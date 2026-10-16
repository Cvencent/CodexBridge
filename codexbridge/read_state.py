"""Shared read markers used by the phone companion and legacy devices."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

MAX_MESSAGE_IDS = 2000


class ReadStateStore:
    """Persist one global read watermark per thread and message.

    The desktop is the coordination point for every client. A read marker made
    by a phone, an ESP device, or any future client is therefore visible to all
    other clients through the same store.
    """

    def __init__(self, path: Path | None) -> None:
        self.path = Path(path) if path is not None else None
        self._threads: dict[str, int] = {}
        self._messages: set[str] = set()
        self._lock = threading.RLock()
        # Every client writes through this one store.  Listeners let the hub
        # turn a global read marker into an immediate fan-out to all devices.
        self._listeners: list[Any] = []
        self.load()

    def add_listener(self, listener: Any) -> None:
        self._listeners.append(listener)

    def _notify(
        self,
        thread_id: str,
        completed_at: int,
        message_id: str,
        source: str,
    ) -> None:
        event = {
            "thread_id": thread_id,
            "completed_at": completed_at,
            "message_id": message_id,
            "source": source,
        }
        for listener in list(self._listeners):
            try:
                listener(event)
            except Exception:  # noqa: BLE001 - one bad client must not block reads
                pass

    def load(self) -> None:
        if self.path is None:
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return
        if not isinstance(payload, dict):
            return
        with self._lock:
            threads = payload.get("threads")
            if isinstance(threads, dict):
                for thread_id, value in threads.items():
                    if isinstance(thread_id, str) and isinstance(value, (int, float)):
                        self._threads[thread_id] = max(0, int(value))
            messages = payload.get("messages")
            if isinstance(messages, list):
                self._messages = {value for value in messages if isinstance(value, str)}

    def save(self) -> None:
        if self.path is None:
            return
        with self._lock:
            payload: dict[str, Any] = {
                "version": 1,
                "threads": dict(self._threads),
                "messages": sorted(self._messages)[-MAX_MESSAGE_IDS:],
            }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary.replace(self.path)
        except OSError:
            return

    def thread_read_at(self, thread_id: str) -> int:
        with self._lock:
            return int(self._threads.get(str(thread_id or ""), 0))

    def is_thread_read(self, thread_id: str, completed_at: int) -> bool:
        if not thread_id or completed_at <= 0:
            return False
        return self.thread_read_at(thread_id) >= int(completed_at)

    def mark_thread(
        self,
        thread_id: str,
        completed_at: int,
        source: str = "",
        notify: bool = True,
    ) -> None:
        if not thread_id or completed_at <= 0:
            return
        with self._lock:
            current = self._threads.get(thread_id, 0)
            if completed_at <= current:
                return
            self._threads[thread_id] = int(completed_at)
        self.save()
        if notify:
            self._notify(thread_id, int(completed_at), "", source)

    def is_message_read(self, message_id: str) -> bool:
        with self._lock:
            return str(message_id or "") in self._messages

    def mark_message(
        self,
        message_id: str,
        source: str = "",
        notify: bool = True,
    ) -> None:
        if not message_id:
            return
        with self._lock:
            if message_id in self._messages:
                return
            self._messages.add(message_id)
            if len(self._messages) > MAX_MESSAGE_IDS:
                self._messages = set(sorted(self._messages)[-MAX_MESSAGE_IDS:])
        self.save()
        if not notify:
            return
        parts = message_id.split(":")
        thread_id = parts[1] if len(parts) > 1 else ""
        self._notify(thread_id, 0, message_id, source)
