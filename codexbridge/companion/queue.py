"""Bridge-owned persistent queue for messages waiting on a Codex thread."""

from __future__ import annotations

import json
import os
import secrets
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable

from codexbridge.protocol.commands import CommandError

MAX_ITEMS_PER_THREAD = 100
MAX_TEXT_CHARS = 10_000
_TERMINAL = {"dispatched", "steered", "deleted"}


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


class QueueService:
    def __init__(
        self,
        path: Path | None,
        *,
        start_turn: Callable[..., dict[str, Any]],
        steer_turn: Callable[[str, str], dict[str, Any]],
        thread_exists: Callable[[str], bool],
        is_active: Callable[[str], bool],
        publish: Callable[[dict[str, Any]], None],
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = Path(path) if path is not None else None
        self._start_turn = start_turn
        self._steer_turn = steer_turn
        self._thread_exists = thread_exists
        self._is_active = is_active
        self._publish = publish
        self._clock = clock
        self._lock = threading.RLock()
        self._items: dict[str, dict[str, dict[str, Any]]] = {}
        self._paused: set[str] = set()
        self._revision = 0
        self._stopped = threading.Event()
        self._wake = threading.Event()
        self._worker: threading.Thread | None = None
        self._load()

    def _load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return
        revision = payload.get("revision") if isinstance(payload, dict) else None
        if isinstance(revision, int) and not isinstance(revision, bool) and revision >= 0:
            self._revision = revision
        raw_threads = payload.get("queues") if isinstance(payload, dict) else None
        if not isinstance(raw_threads, dict):
            return
        for thread_id, raw_items in raw_threads.items():
            if not isinstance(thread_id, str) or not isinstance(raw_items, list):
                continue
            items: dict[str, dict[str, Any]] = {}
            for raw in raw_items:
                item = self._normalize_item(raw, thread_id)
                if item is not None and item["status"] not in _TERMINAL:
                    items[item["queue_id"]] = item
            if items:
                self._items[thread_id] = items

    def _save(self) -> None:
        if self.path is None:
            return
        payload = {
            "version": 1,
            "revision": self._revision,
            "queues": {
                thread_id: [items[key] for key in sorted(items)]
                for thread_id, items in sorted(self._items.items())
                if items
            },
        }
        _replace_atomic(self.path, payload)

    @staticmethod
    def _normalize_item(raw: object, thread_id: str) -> dict[str, Any] | None:
        if not isinstance(raw, dict):
            return None
        queue_id = raw.get("queue_id")
        text = raw.get("text")
        if not isinstance(queue_id, str) or not queue_id:
            return None
        if not isinstance(text, str) or not text.strip():
            return None
        status = str(raw.get("status") or "pending")
        if status not in {"pending", "dispatching", "failed"} | _TERMINAL:
            status = "pending"
        return {
            "queue_id": queue_id,
            "thread_id": thread_id,
            "text": text[:MAX_TEXT_CHARS],
            "model": str(raw.get("model") or ""),
            "effort": str(raw.get("effort") or ""),
            "status": status,
            "revision": int(raw.get("revision") or 0),
            "created_at": float(raw.get("created_at") or 0.0),
            "updated_at": float(raw.get("updated_at") or 0.0),
            "created_by": str(raw.get("created_by") or ""),
            "created_by_kind": str(raw.get("created_by_kind") or ""),
            "error": str(raw.get("error") or ""),
        }

    def _next_revision(self) -> int:
        self._revision += 1
        return self._revision

    @staticmethod
    def _public(item: dict[str, Any]) -> dict[str, Any]:
        return dict(item)

    def _snapshot(self, thread_id: str) -> dict[str, Any]:
        items = [
            self._public(item)
            for item in self._items.get(thread_id, {}).values()
            if item["status"] not in _TERMINAL
        ]
        items.sort(key=lambda item: (item["created_at"], item["queue_id"]))
        return {
            "thread_id": thread_id,
            "revision": self._revision,
            "paused": thread_id in self._paused,
            "items": items,
        }

    def list(self, thread_id: str) -> dict[str, Any]:
        if not isinstance(thread_id, str) or not thread_id:
            raise CommandError("invalid_params")
        with self._lock:
            return self._snapshot(thread_id)

    def snapshots(self) -> list[dict[str, Any]]:
        with self._lock:
            return [self._snapshot(thread_id) for thread_id in sorted(self._items)]

    def _emit(self, event_type: str, thread_id: str, item: dict[str, Any] | None = None,
              **extra: Any) -> None:
        event = {
            "type": event_type,
            "thread_id": thread_id,
            "revision": self._revision,
            "server_time": self._clock(),
        }
        if item is not None:
            event["queue_id"] = item["queue_id"]
            event["item"] = self._public(item)
        event.update(extra)
        try:
            self._publish(event)
        except Exception:
            pass

    def _get_item(self, queue_id: str) -> tuple[str, dict[str, Any]]:
        for thread_id, items in self._items.items():
            item = items.get(queue_id)
            if item is not None and item["status"] not in _TERMINAL:
                return thread_id, item
        raise CommandError("queue_item_not_found")

    def _check_revision(self, item: dict[str, Any], expected_revision: int) -> None:
        if not isinstance(expected_revision, int) or isinstance(expected_revision, bool):
            raise CommandError("invalid_params")
        if expected_revision != item["revision"]:
            raise CommandError("queue_conflict")

    def _resume_locked(self, thread_id: str) -> bool:
        """Clear a dispatch pause for one thread. True when it was paused.

        ``_paused`` only exists to stop a hot retry loop after a dispatch
        failure. Any explicit user action that shows the thread is usable
        again (adding a message, deleting the failed item, or a turn that
        actually completed) must lift it, otherwise the queue stays stuck
        until someone edits the exact failed row.
        """

        if thread_id in self._paused:
            self._paused.discard(thread_id)
            return True
        return False

    def enqueue(self, thread_id: str, text: str, model: str = "", effort: str = "",
                created_by: str = "", created_by_kind: str = "") -> dict[str, Any]:
        if not isinstance(thread_id, str) or not thread_id or not self._thread_exists(thread_id):
            raise CommandError("thread_not_found")
        content = text.strip() if isinstance(text, str) else ""
        if not content:
            raise CommandError("invalid_params")
        if len(content) > MAX_TEXT_CHARS:
            raise CommandError("queue_text_too_long")
        with self._lock:
            items = self._items.setdefault(thread_id, {})
            visible = [item for item in items.values() if item["status"] not in _TERMINAL]
            if len(visible) >= MAX_ITEMS_PER_THREAD:
                raise CommandError("queue_full")
            now = self._clock()
            queue_id = "q_" + secrets.token_hex(12)
            item = {
                "queue_id": queue_id,
                "thread_id": thread_id,
                "text": content,
                "model": str(model or ""),
                "effort": str(effort or ""),
                "status": "pending",
                "revision": 1,
                "created_at": now,
                "updated_at": now,
                "created_by": str(created_by or ""),
                "created_by_kind": str(created_by_kind or ""),
                "error": "",
            }
            items[queue_id] = item
            # A new message is an explicit retry: let the queue move again.
            self._resume_locked(thread_id)
            self._next_revision()
            self._save()
            self._emit("queue_item_added", thread_id, item)
        self._wake.set()
        return {"item": self._public(item), **self._snapshot(thread_id)}

    def edit(self, queue_id: str, text: str, model: str = "", effort: str = "",
             expected_revision: int = 0) -> dict[str, Any]:
        content = text.strip() if isinstance(text, str) else ""
        if not content:
            raise CommandError("invalid_params")
        if len(content) > MAX_TEXT_CHARS:
            raise CommandError("queue_text_too_long")
        with self._lock:
            thread_id, item = self._get_item(queue_id)
            self._check_revision(item, expected_revision)
            if item["status"] == "dispatching":
                raise CommandError("queue_conflict")
            item["text"] = content
            if model:
                item["model"] = str(model)
            if effort:
                item["effort"] = str(effort)
            item["status"] = "pending"
            item["error"] = ""
            item["revision"] += 1
            item["updated_at"] = self._clock()
            self._paused.discard(thread_id)
            self._next_revision()
            self._save()
            self._emit("queue_item_updated", thread_id, item)
        self._wake.set()
        return {"item": self._public(item), **self._snapshot(thread_id)}

    def delete(self, queue_id: str, expected_revision: int = 0) -> dict[str, Any]:
        with self._lock:
            thread_id, item = self._get_item(queue_id)
            self._check_revision(item, expected_revision)
            if item["status"] == "dispatching":
                raise CommandError("queue_conflict")
            item["status"] = "deleted"
            item["revision"] += 1
            item["updated_at"] = self._clock()
            self._items.get(thread_id, {}).pop(queue_id, None)
            if not self._items.get(thread_id):
                self._items.pop(thread_id, None)
            remaining = self._items.get(thread_id, {}).values()
            still_failed = any(value["status"] == "failed" for value in remaining)
            if item["status"] == "failed" or not still_failed:
                self._resume_locked(thread_id)
            self._next_revision()
            self._save()
            self._emit("queue_item_removed", thread_id, item, reason="deleted")
        return {"removed": True, **self._snapshot(thread_id)}

    def steer(self, queue_id: str, expected_revision: int = 0) -> dict[str, Any]:
        with self._lock:
            thread_id, item = self._get_item(queue_id)
            self._check_revision(item, expected_revision)
        if not self._is_active(thread_id):
            raise CommandError("no_active_turn")
        try:
            self._steer_turn(thread_id, item["text"])
        except Exception:
            raise CommandError("no_active_turn")
        with self._lock:
            item["status"] = "steered"
            item["revision"] += 1
            item["updated_at"] = self._clock()
            self._items.get(thread_id, {}).pop(queue_id, None)
            if not self._items.get(thread_id):
                self._items.pop(thread_id, None)
            self._next_revision()
            self._save()
            self._emit("queue_item_removed", thread_id, item, reason="steered")
        return {"item": self._public(item), **self._snapshot(thread_id)}

    def start(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        self._stopped.clear()
        self._worker = threading.Thread(
            target=self._dispatch_loop,
            name="bridge-queue-dispatch",
            daemon=True,
        )
        self._worker.start()
        self._wake.set()

    def stop(self) -> None:
        self._stopped.set()
        self._wake.set()
        worker = self._worker
        self._worker = None
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=2.0)

    def on_turn_finished(self, thread_id: str) -> None:
        with self._lock:
            # The thread just ran a turn, so a stale dispatch pause no longer
            # describes reality.
            self._resume_locked(thread_id)
            item = next(
                (
                    value for value in self._items.get(thread_id, {}).values()
                    if value["status"] == "dispatching"
                ),
                None,
            )
            if item is not None:
                item["status"] = "pending"
                item["updated_at"] = self._clock()
                item["revision"] += 1
                self._next_revision()
                self._save()
                self._emit("queue_item_updated", thread_id, item)
        self._wake.set()

    def _dispatch_loop(self) -> None:
        while not self._stopped.is_set():
            self._wake.wait(0.25)
            self._wake.clear()
            self.dispatch_ready()

    def _ready_thread(self) -> tuple[str, dict[str, Any]] | None:
        with self._lock:
            for thread_id in sorted(self._items):
                if thread_id in self._paused or self._is_active(thread_id):
                    continue
                items = [
                    item for item in self._items[thread_id].values()
                    if item["status"] == "pending"
                ]
                if not items:
                    continue
                items.sort(key=lambda item: (item["created_at"], item["queue_id"]))
                return thread_id, items[0]
        return None

    def dispatch_ready(self) -> int:
        dispatched = 0
        while not self._stopped.is_set():
            ready = self._ready_thread()
            if ready is None:
                break
            thread_id, item = ready
            with self._lock:
                if item["status"] != "pending":
                    continue
                item["status"] = "dispatching"
                item["revision"] += 1
                item["updated_at"] = self._clock()
                self._next_revision()
                self._save()
                self._emit("queue_item_updated", thread_id, item)
            try:
                self._start_turn(
                    thread_id,
                    item["text"],
                    model=item["model"],
                    effort=item["effort"],
                    client_message_id=item["queue_id"],
                )
            except Exception as error:
                with self._lock:
                    item["status"] = "failed"
                    item["error"] = str(error)[:500]
                    item["revision"] += 1
                    item["updated_at"] = self._clock()
                    self._paused.add(thread_id)
                    self._next_revision()
                    self._save()
                    self._emit("queue_item_updated", thread_id, item)
                    self._emit("queue_paused", thread_id, item)
                break
            with self._lock:
                item["status"] = "dispatched"
                item["revision"] += 1
                item["updated_at"] = self._clock()
                self._items.get(thread_id, {}).pop(item["queue_id"], None)
                if not self._items.get(thread_id):
                    self._items.pop(thread_id, None)
                self._next_revision()
                self._save()
                self._emit(
                    "queue_item_removed",
                    thread_id,
                    item,
                    reason="dispatched",
                )
            dispatched += 1
            # A real turn owns the thread now; do not dispatch the next item
            # until the completion hook releases it.
            break
        return dispatched
