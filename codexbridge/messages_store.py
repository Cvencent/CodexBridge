from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field

KIND_CAPABILITY: dict[str, str] = {
    "status": "status",
    "task": "tasks",
    "result": "results",
    "approval": "approval",
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
    read_by: set[str] = field(default_factory=set)


class MessageStore:
    def __init__(self, max_messages: int = 200):
        if max_messages <= 0:
            raise ValueError("max_messages must be positive")
        self.max_messages = max_messages
        self._messages: OrderedDict[str, MessageRecord] = OrderedDict()

    def add(self, record: MessageRecord) -> MessageRecord:
        if record.message_id in self._messages:
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
