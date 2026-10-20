"""Persistent content store and upload transactions for Companion clients."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MAX_ATTACHMENT_BYTES = 32 * 1024 * 1024
MAX_ATTACHMENTS_PER_THREAD = 64
MAX_UPLOAD_CHUNK_BYTES = 2 * 1024 * 1024
UPLOAD_TTL_SECONDS = 60 * 60
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
_KINDS = {"file", "image", "audio", "other"}


class AttachmentError(ValueError):
    """Typed failure that maps onto a stable HTTP/CBDP error code."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.message = message or code


def _now() -> float:
    return time.time()


def _safe_filename(value: str, kind: str) -> str:
    name = Path(str(value or "")).name.strip()
    cleaned = _SAFE_NAME.sub("_", name)[:120].strip("._")
    if not cleaned:
        cleaned = f"attachment.{'png' if kind == 'image' else 'bin'}"
    return cleaned


def _kind_for_mime(mime_type: str) -> str:
    mime = str(mime_type or "").lower()
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("audio/"):
        return "audio"
    if mime.startswith(("text/", "application/")) or mime:
        return "file"
    return "other"


def _assert_kind_matches(kind: str, mime_type: str) -> None:
    expected = _kind_for_mime(mime_type)
    if kind == "other":
        return
    if kind == "file" and expected in {"file", "other"}:
        return
    if kind == "image" and expected == "image":
        return
    if kind == "audio" and expected == "audio":
        return
    raise AttachmentError("ATTACHMENT_KIND_MISMATCH")


@dataclass
class AttachmentRecord:
    attachment_id: str
    thread_id: str
    kind: str
    filename: str
    mime_type: str
    byte_size: int
    content_sha256: str
    content_ref: str
    created_at: float = field(default_factory=_now)
    alt_text: str = ""
    source: str = ""

    def public(self) -> dict[str, Any]:
        return {
            "attachment_id": self.attachment_id,
            "attachment_ref": self.attachment_id,
            "thread_id": self.thread_id,
            "kind": self.kind,
            "filename": self.filename,
            "mime_type": self.mime_type,
            "byte_size": self.byte_size,
            "content_sha256": self.content_sha256,
            "content_ref": self.content_ref,
            "created_at": self.created_at,
            "alt_text": self.alt_text,
        }


@dataclass
class UploadTransaction:
    upload_id: str
    thread_id: str
    kind: str
    filename: str
    mime_type: str
    byte_size: int
    content_sha256: str
    path: Path
    created_at: float = field(default_factory=_now)
    offset: int = 0
    alt_text: str = ""
    source: str = ""


class AttachmentStore:
    """Bounded on-disk content store plus resumable upload sessions."""

    def __init__(self, data_dir: Path) -> None:
        self.root = Path(data_dir) / "attachments"
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_path = self.root / "index.json"
        self._lock = threading.RLock()
        self._records: dict[str, AttachmentRecord] = {}
        self._uploads: dict[str, UploadTransaction] = {}
        self._load()

    def _load(self) -> None:
        try:
            payload = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return
        rows = payload.get("attachments") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            return
        with self._lock:
            for row in rows:
                if not isinstance(row, dict):
                    continue
                try:
                    record = AttachmentRecord(
                        attachment_id=str(row["attachment_id"]),
                        thread_id=str(row.get("thread_id") or ""),
                        kind=str(row.get("kind") or "file"),
                        filename=str(row.get("filename") or ""),
                        mime_type=str(row.get("mime_type") or ""),
                        byte_size=int(row.get("byte_size") or 0),
                        content_sha256=str(row.get("content_sha256") or ""),
                        content_ref=str(row.get("content_ref") or ""),
                        created_at=float(row.get("created_at") or 0.0),
                        alt_text=str(row.get("alt_text") or ""),
                        source=str(row.get("source") or ""),
                    )
                except (KeyError, TypeError, ValueError):
                    continue
                if record.attachment_id and self._path(record.attachment_id).exists():
                    self._records[record.attachment_id] = record

    def _save(self) -> None:
        with self._lock:
            payload = {
                "version": 1,
                "attachments": [
                    {
                        **record.public(),
                        "content_ref": record.content_ref,
                        "source": record.source,
                    }
                    for record in self._records.values()
                ],
            }
        _replace_atomic(self.index_path, payload)

    def _path(self, attachment_id: str) -> Path:
        return self.root / f"{attachment_id}.bin"

    def begin_upload(
        self,
        *,
        thread_id: str,
        kind: str,
        byte_size: int,
        content_sha256: str = "",
        filename: str = "",
        mime_type: str = "",
        alt_text: str = "",
        source: str = "",
    ) -> dict[str, Any]:
        kind = str(kind or "").lower()
        if kind not in _KINDS:
            raise AttachmentError("INVALID_PARAM")
        if not thread_id:
            raise AttachmentError("INVALID_PARAM")
        if not isinstance(byte_size, int) or isinstance(byte_size, bool):
            raise AttachmentError("INVALID_PARAM")
        if byte_size <= 0 or byte_size > MAX_ATTACHMENT_BYTES:
            raise AttachmentError("PAYLOAD_TOO_LARGE")
        mime_type = str(mime_type or "application/octet-stream").lower()[:120]
        _assert_kind_matches(kind, mime_type)
        filename = _safe_filename(filename, kind)
        upload_id = "u_" + secrets.token_hex(16)
        path = self.root / f".upload-{upload_id}"
        with self._lock:
            self._prune_uploads_locked()
            transaction = UploadTransaction(
                upload_id=upload_id,
                thread_id=str(thread_id),
                kind=kind,
                filename=filename,
                mime_type=mime_type,
                byte_size=int(byte_size),
                content_sha256=str(content_sha256 or "").lower(),
                path=path,
                alt_text=str(alt_text or "")[:280],
                source=str(source or "")[:120],
            )
            self._uploads[upload_id] = transaction
            path.write_bytes(b"")
        return self._upload_public(transaction)

    @staticmethod
    def _upload_public(transaction: UploadTransaction) -> dict[str, Any]:
        return {
            "upload_id": transaction.upload_id,
            "thread_id": transaction.thread_id,
            "upload_state": "uploading" if transaction.offset else "pending",
            "upload_offset": transaction.offset,
            "byte_size": transaction.byte_size,
        }

    def upload_chunk(self, upload_id: str, offset: int, data: bytes) -> dict[str, Any]:
        if not isinstance(data, (bytes, bytearray)) or not data:
            raise AttachmentError("INVALID_PARAM")
        if len(data) > MAX_UPLOAD_CHUNK_BYTES:
            raise AttachmentError("PAYLOAD_TOO_LARGE")
        with self._lock:
            transaction = self._uploads.get(str(upload_id or ""))
            if transaction is None:
                raise AttachmentError("NOT_FOUND")
            if offset != transaction.offset:
                raise AttachmentError("UPLOAD_OFFSET_MISMATCH")
            if transaction.offset + len(data) > transaction.byte_size:
                raise AttachmentError("INVALID_PARAM")
            with transaction.path.open("ab") as handle:
                handle.write(bytes(data))
            transaction.offset += len(data)
            return self._upload_public(transaction)

    def end_upload(
        self,
        upload_id: str,
        *,
        byte_size: int | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            transaction = self._uploads.get(str(upload_id or ""))
            if transaction is None:
                raise AttachmentError("NOT_FOUND")
            if byte_size is not None and int(byte_size) != transaction.byte_size:
                raise AttachmentError("INVALID_PARAM")
            if transaction.offset != transaction.byte_size:
                raise AttachmentError("UPLOAD_INCOMPLETE")
            data = transaction.path.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            if transaction.content_sha256 and digest != transaction.content_sha256:
                transaction.path.unlink(missing_ok=True)
                self._uploads.pop(transaction.upload_id, None)
                raise AttachmentError("CONTENT_HASH_MISMATCH")
            attachment_id = "att_" + digest[:24]
            target = self._path(attachment_id)
            os.replace(transaction.path, target)
            record = AttachmentRecord(
                attachment_id=attachment_id,
                thread_id=transaction.thread_id,
                kind=transaction.kind,
                filename=transaction.filename,
                mime_type=transaction.mime_type,
                byte_size=transaction.byte_size,
                content_sha256=digest,
                content_ref=f"content:{attachment_id}",
                alt_text=transaction.alt_text,
                source=transaction.source,
            )
            self._records[attachment_id] = record
            self._uploads.pop(transaction.upload_id, None)
            self._prune_thread_locked(record.thread_id)
            self._save()
        return {**record.public(), "upload_state": "complete"}

    def cancel_upload(self, upload_id: str) -> dict[str, Any]:
        with self._lock:
            transaction = self._uploads.pop(str(upload_id or ""), None)
            if transaction is None:
                raise AttachmentError("NOT_FOUND")
            transaction.path.unlink(missing_ok=True)
        return {"upload_id": transaction.upload_id, "upload_state": "cancelled"}

    def _prune_uploads_locked(self) -> None:
        cutoff = _now() - UPLOAD_TTL_SECONDS
        for upload_id, transaction in list(self._uploads.items()):
            if transaction.created_at < cutoff:
                transaction.path.unlink(missing_ok=True)
                self._uploads.pop(upload_id, None)

    def _prune_thread_locked(self, thread_id: str) -> None:
        records = [record for record in self._records.values() if record.thread_id == thread_id]
        records.sort(key=lambda record: record.created_at)
        for record in records[:-MAX_ATTACHMENTS_PER_THREAD]:
            self._records.pop(record.attachment_id, None)
            self._path(record.attachment_id).unlink(missing_ok=True)

    def get(self, attachment_id: str) -> AttachmentRecord | None:
        with self._lock:
            return self._records.get(str(attachment_id or ""))

    def get_by_ref(self, content_ref: str) -> AttachmentRecord | None:
        value = str(content_ref or "")
        if value.startswith("content:"):
            value = value[len("content:"):]
        return self.get(value)

    def list(self, thread_id: str) -> dict[str, Any]:
        with self._lock:
            rows = [
                record.public()
                for record in sorted(self._records.values(), key=lambda item: item.created_at)
                if not thread_id or record.thread_id == thread_id
            ]
        return {"attachments": rows, "object_count": len(rows)}

    def remove(self, attachment_id: str) -> bool:
        with self._lock:
            record = self._records.pop(str(attachment_id or ""), None)
            if record is None:
                return False
            self._path(record.attachment_id).unlink(missing_ok=True)
            self._save()
            return True

    def set_alt_text(self, attachment_id: str, alt_text: str) -> AttachmentRecord | None:
        with self._lock:
            record = self._records.get(str(attachment_id or ""))
            if record is None:
                return None
            record.alt_text = str(alt_text or "")[:280]
            self._save()
            return record

    def read_bytes(self, attachment_id: str, offset: int = 0, max_bytes: int = 256 * 1024) -> dict[str, Any]:
        record = self.get(attachment_id)
        if record is None:
            raise AttachmentError("NOT_FOUND")
        data = self._path(record.attachment_id).read_bytes()
        offset = max(0, int(offset))
        limit = max(1, min(int(max_bytes), MAX_UPLOAD_CHUNK_BYTES))
        chunk = data[offset:offset + limit]
        return {
            **record.public(),
            "offset": offset,
            "next_offset": offset + len(chunk),
            "total_bytes": len(data),
            "data": chunk,
        }

    def data_url(self, attachment_id: str) -> str:
        record = self.get(attachment_id)
        if record is None:
            raise AttachmentError("NOT_FOUND")
        import base64

        payload = base64.b64encode(self._path(record.attachment_id).read_bytes()).decode("ascii")
        return f"data:{record.mime_type};base64,{payload}"


def _replace_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            name = temporary.name
            json.dump(payload, temporary, ensure_ascii=False, separators=(",", ":"))
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(name, path)
    finally:
        if name and os.path.exists(name):
            try:
                os.unlink(name)
            except OSError:
                pass
