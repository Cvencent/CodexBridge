from __future__ import annotations

from __future__ import annotations

import hashlib
import json
import time
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


# A task that is still moving is not a notification the user has consumed.
# These records describe live state, so the host must keep them reachable after
# any client has marked them read, and a pull must always be able to answer
# with the current one.
IN_FLIGHT_KINDS: frozenset[str] = frozenset({"status", "task"})
IN_FLIGHT_STATUSES: frozenset[str] = frozenset(
    {"working", "waiting_user", "queued", "starting"}
)


# An in-flight record only proves the host is alive while the collector keeps
# republishing it. A Codex thread that died, or a collector that stopped, leaves
# the same ``working`` line sitting in the store forever, and a device polling
# for state would then read "4 tasks running" long after nothing is running.
# Freshness is measured from ``seen_at`` -- the wall-clock instant the host last
# received this exact record -- so it needs no per-device knowledge.
IN_FLIGHT_STALE_AFTER_SECONDS: float = 90.0


def is_in_flight(record: "MessageRecord") -> bool:
    return record.kind in IN_FLIGHT_KINDS and record.status in IN_FLIGHT_STATUSES


def is_fresh(
    record: "MessageRecord",
    now: float | None = None,
    max_age_seconds: float = IN_FLIGHT_STALE_AFTER_SECONDS,
) -> bool:
    """Has the host seen this record recently enough to still call it live?

    ``max_age_seconds <= 0`` disables the rule, and a record with no usable
    timestamp is treated as fresh so a client that cannot supply clocks is
    never penalised.
    """
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
    # Set by the store on every arrival; never part of the fingerprint,
    # because it is transport metadata rather than renderable content.
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
        if record.kind in IN_FLIGHT_KINDS:
            # A collector cycle that re-reports the same live task is the proof
            # of life; record the arrival so staleness can be judged later.
            record.seen_at = time.time()
        if existing is not None:
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

    def live_status(self, thread_id: str = "") -> MessageRecord | None:
        """Most recent in-flight status record, or None.

        The device asks for this when its screen stops making progress, so the
        answer has to be the record the host is currently sitting on rather than
        the last one that happened to look new. ``thread_id`` narrows the answer
        to the conversation a client is actually watching.
        """
        wanted = str(thread_id or "")
        fresh = [record for record in self._messages.values()
                 if is_in_flight(record) and is_fresh(record)
                 and (not wanted or str(record.thread_id or "") == wanted)]
        status_records = [record for record in fresh if record.kind == "status"]
        # The task record carries the same live thread when the collector has
        # only republished one of the pair, so a waiting device is answered
        # rather than left in silence.
        candidates = status_records or [record for record in fresh if record.kind == "task"]
        if not candidates:
            return None
        return max(candidates, key=lambda item: (item.seen_at or item.created_at,
                                                 item.message_id))

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
