from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Fields the collector refreshes on every poll that no device renders. Keeping
# them out of the fingerprint is what stops a stable task from looking new.
VOLATILE_FIELDS: frozenset[str] = frozenset(
    {
        "generated_at_epoch",
        "started_at_epoch",
        "last_updated_epoch",
        "completed_at_epoch",
        "duration_seconds",
        "token_count",
        "token_count_available",
    }
)


def _without_volatile(value: Any) -> Any:
    """Drop timing/counter noise so an unchanged task looks unchanged."""
    if isinstance(value, dict):
        return {
            key: _without_volatile(item)
            for key, item in sorted(value.items())
            if key not in VOLATILE_FIELDS
        }
    if isinstance(value, list):
        return [_without_volatile(item) for item in value]
    return value


# A task that is still moving is not a notification the user has consumed.
# These records describe live state, so the host must keep them reachable after
# any client has marked them read, and a pull must always be able to answer
# with the current one.
IN_FLIGHT_KINDS: frozenset[str] = frozenset({"status", "task"})
IN_FLIGHT_STATUSES: frozenset[str] = frozenset(
    {"working", "waiting_user", "queued", "starting"}
)

# Device-facing names are intentionally generic. ``waiting_user`` is the
# internal Codex/Companion spelling; CBDP clients use ``waiting`` in both
# message payloads and status snapshots.
DEVICE_STATUS_NAMES: dict[str, str] = {"waiting_user": "waiting"}


def device_status(status: str) -> str:
    value = str(status or "")
    return DEVICE_STATUS_NAMES.get(value, value)


def is_in_flight(record: "MessageRecord") -> bool:
    return record.kind in IN_FLIGHT_KINDS and record.status in IN_FLIGHT_STATUSES


# A live record only proves the host is still refreshing it. If the collector
# stops republishing a working thread, the old record must not keep the running
# badge lit forever.
IN_FLIGHT_STALE_AFTER_SECONDS: float = 90.0


def is_fresh(
    record: "MessageRecord",
    now: float | None = None,
    max_age_seconds: float = IN_FLIGHT_STALE_AFTER_SECONDS,
) -> bool:
    """Return whether a live record has been republished recently enough."""
    if max_age_seconds <= 0:
        return True
    stamp = record.seen_at or record.created_at
    if stamp <= 0:
        return True
    reference = time.time() if now is None else float(now)
    return reference - stamp <= float(max_age_seconds)


KIND_CAPABILITY: dict[str, str] = {
    "status": "status",
    "task": "tasks",
    "result": "results",
    "approval": "approval",
    "codex_status": "quota",
}


@dataclass
class MessageRecord:
    message_id: str
    thread_id: str
    kind: str
    status: str
    title: str
    body: str
    created_at: float
    data: dict[str, Any] | None = None
    read_by: set[str] = field(default_factory=set)
    # Set by the store on each live arrival; transport metadata, not content.
    seen_at: float = 0.0

    @property
    def fingerprint(self) -> str:
        """Stable hash of the *renderable* content of this record.

        The collector re-reads the same Codex snapshot every few seconds, so
        records often republish byte-for-byte identical text with a fresher
        timestamp. Devices must not be woken for that, so the host compares a
        fingerprint that ignores volatile timing fields.
        """
        return hashlib.sha256(
            json.dumps(
                {
                    "kind": self.kind,
                    "status": self.status,
                    "title": self.title,
                    "body": self.body,
                    "data": _without_volatile(self.data),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest()


class MessageStore:
    def __init__(
        self,
        max_messages: int = 200,
        path: Path | str | None = None,
    ):
        if isinstance(max_messages, (str, Path)):
            raise TypeError("max_messages must be an integer; use path= for persistence")
        if max_messages <= 0:
            raise ValueError("max_messages must be positive")
        self.max_messages = max_messages
        self.path = Path(path) if path is not None else None
        self._lock = threading.RLock()
        self._messages: OrderedDict[str, MessageRecord] = OrderedDict()
        # Hash of the last payload written to disk. save() used to rewrite the
        # whole results file on every collector cycle even when nothing had
        # changed, which showed up as ~8 MB/min of needless writes.
        self._saved_digest: bytes | None = None
        self.load()

    def load(self) -> None:
        """Restore terminal results needed for unread counts after restart.

        Live task/status records describe a transient Codex collection cycle,
        so the collector refreshes them. Terminal results are the notification
        source for global unread counters and must survive Bridge restarts;
        otherwise every upgrade silently clears the user's completion alerts.
        """

        if self.path is None:
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return
        if not isinstance(payload, dict):
            return
        rows = payload.get("messages")
        if not isinstance(rows, list):
            return
        with self._lock:
            for item in rows[-self.max_messages:]:
                if not isinstance(item, dict):
                    continue
                message_id = str(item.get("message_id") or "")
                thread_id = str(item.get("thread_id") or "")
                status = str(item.get("status") or "")
                if not message_id or not thread_id or status not in {
                    "completed",
                    "failed",
                }:
                    continue
                try:
                    created_at = float(item.get("created_at") or 0)
                except (TypeError, ValueError):
                    created_at = 0.0
                data = item.get("data")
                self._messages[message_id] = MessageRecord(
                    message_id=message_id,
                    thread_id=thread_id,
                    kind="result",
                    status=status,
                    title=str(item.get("title") or ""),
                    body=str(item.get("body") or ""),
                    created_at=created_at,
                    data=data if isinstance(data, dict) else None,
                )

    def save(self) -> None:
        if self.path is None:
            return
        with self._lock:
            rows = [
                {
                    "message_id": record.message_id,
                    "thread_id": record.thread_id,
                    "status": record.status,
                    "title": record.title,
                    "body": record.body,
                    "created_at": record.created_at,
                    "data": record.data,
                }
                for record in self._messages.values()
                if record.kind == "result"
                and record.status in {"completed", "failed"}
            ]
            payload = {"version": 1, "messages": rows}
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        # The collector refreshes timing/counter fields on every poll. Hashing
        # the full payload made each cycle look like a change, so the file was
        # rewritten even when every rendered value was identical. Compare the
        # stable projection instead; the bytes written still carry everything.
        stable = json.dumps(
            _without_volatile(payload), ensure_ascii=False, sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(stable.encode("utf-8")).digest()
        with self._lock:
            if digest == self._saved_digest:
                return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            temporary.write_text(text, encoding="utf-8")
            temporary.replace(self.path)
        except OSError:
            return
        with self._lock:
            self._saved_digest = digest

    def add(self, record: MessageRecord) -> MessageRecord:
        with self._lock:
            existing = self._messages.get(record.message_id)
            if existing is not None:
                record.read_by.update(existing.read_by)
                if record.data is None:
                    record.data = existing.data
            if record.kind in IN_FLIGHT_KINDS:
                record.seen_at = time.time()
            if existing is not None:
                del self._messages[record.message_id]
            self._messages[record.message_id] = record
            while len(self._messages) > self.max_messages:
                self._messages.popitem(last=False)
            persist = record.kind == "result" and record.status in {
                "completed",
                "failed",
            }
        if persist:
            self.save()
        return record

    def get(self, message_id: str) -> MessageRecord | None:
        with self._lock:
            return self._messages.get(message_id)

    def mark_read(self, message_id: str, device_id: str) -> MessageRecord | None:
        with self._lock:
            record = self._messages.get(message_id)
            if record is None:
                return None
            record.read_by.add(device_id)
            return record

    def read_by(self, message_id: str) -> set[str]:
        with self._lock:
            record = self._messages.get(message_id)
            return set(record.read_by) if record is not None else set()

    def live_status(self, thread_id: str = "") -> MessageRecord | None:
        """Most recent in-flight status record, or None.

        The device asks for this when its screen stops making progress, so the
        answer has to be the record the host is currently sitting on rather than
        the last one that happened to look new. ``thread_id`` narrows the answer
        to the conversation a client is actually watching.
        """
        wanted = str(thread_id or "")
        with self._lock:
            fresh = [
                record
                for record in self._messages.values()
                if is_in_flight(record) and is_fresh(record)
                and (not wanted or str(record.thread_id or "") == wanted)
            ]
        status_records = [record for record in fresh if record.kind == "status"]
        # A task record carries the same live thread when only one half of the
        # pair was republished, so it is still a valid answer. Keep status
        # records authoritative for renderable text; status transitions use a
        # new message id, so a fresh task record is not needed to replace it.
        candidates = status_records or [
            record for record in fresh if record.kind == "task"
        ]
        if not candidates:
            return None
        return max(
            candidates,
            key=lambda item: (item.seen_at or item.created_at, item.message_id),
        )

    def records(self) -> list[MessageRecord]:
        """Snapshot of stored records in insertion order.

        Exposed so the host can derive aggregate counts (``codex_state``)
        from the exact same set of records it hands to devices, instead of
        maintaining a second, drift-prone tally.
        """
        with self._lock:
            return list(self._messages.values())

    def pending_for(
        self,
        device_id: str,
        capabilities: set[str],
        unread_only: bool = True,
    ) -> list[MessageRecord]:
        pending: list[MessageRecord] = []
        with self._lock:
            for record in self._messages.values():
                capability = KIND_CAPABILITY.get(record.kind)
                if capability is None or capability not in capabilities:
                    continue
                if unread_only and device_id in record.read_by:
                    continue
                pending.append(record)
        return pending
