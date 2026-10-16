from __future__ import annotations

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from dataclasses import dataclass, field
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
    def __init__(self, max_messages: int = 200):
        if max_messages <= 0:
            raise ValueError("max_messages must be positive")
        self.max_messages = max_messages
        self._messages: OrderedDict[str, MessageRecord] = OrderedDict()

    def add(self, record: MessageRecord) -> MessageRecord:
        existing = self._messages.get(record.message_id)
        if existing is not None:
            record.read_by.update(existing.read_by)
            if record.data is None:
                record.data = existing.data
            del self._messages[record.message_id]
        self._messages[record.message_id] = record
        while len(self._messages) > self.max_messages:
            self._messages.popitem(last=False)
        return record

    def get(self, message_id: str) -> MessageRecord | None:
        return self._messages.get(message_id)

    def mark_read(self, message_id: str, device_id: str) -> MessageRecord | None:
        record = self._messages.get(message_id)
        if record is None:
            return None
        record.read_by.add(device_id)
        return record

    def read_by(self, message_id: str) -> set[str]:
        record = self._messages.get(message_id)
        return set(record.read_by) if record is not None else set()

    def records(self) -> list[MessageRecord]:
        """Snapshot of stored records in insertion order.

        Exposed so the host can derive aggregate counts (``codex_state``)
        from the exact same set of records it hands to devices, instead of
        maintaining a second, drift-prone tally.
        """
        return list(self._messages.values())

    def pending_for(
        self,
        device_id: str,
        capabilities: set[str],
        unread_only: bool = True,
    ) -> list[MessageRecord]:
        pending: list[MessageRecord] = []
        for record in self._messages.values():
            capability = KIND_CAPABILITY.get(record.kind)
            if capability is None or capability not in capabilities:
                continue
            if unread_only and device_id in record.read_by:
                continue
            pending.append(record)
        return pending
