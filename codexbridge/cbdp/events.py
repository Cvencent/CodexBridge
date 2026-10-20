from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from codexbridge.cbdp.tlv import decode_tlvs, encode_tlvs


SCOPE_SUMMARY = 0
SCOPE_THREAD = 1
SCOPE_LIVE = 2

# The wire protocol fixes the three scope names but leaves the event-to-scope
# assignment to the Bridge. This table is the single place that decision lives
# so the fan-out and the subscription manager cannot drift apart; system and
# device-wide state stays on the summary scope, a thread's own messages stay on
# its thread scope, and turn/approval/control traffic is live.
_SUMMARY_EVENTS = frozenset(
    {
        0x0101,
        0x0102,
        0x0103,
        0x0104,
        # 0x0302 (message.updated) and 0x0304 (message.deleted) live on the
        # thread topic, so they must share 0x0301's thread scope; otherwise a
        # device subscribed to the thread would silently miss updates.
        0x0303,
        0x0601,
        0x0602,
        0x0603,
        # system.codex_state: aggregate running/unread counters for the home
        # screen. Summary topic, state QoS, so it can be replayed to a device
        # that reconnects with a cursor.
        0x0704,
    }
)
_THREAD_EVENTS = frozenset(
    {
        0x0105,
        0x0106,
        0x0203,
        0x0204,
        0x0205,
        0x0301,
        0x0302,
        0x0304,
        0x0401,
        0x0402,
        0x0403,
    }
)


def event_scope(event_type):
    """Return the delivery scope for one stable event number."""

    value = int(event_type)
    if value in _SUMMARY_EVENTS:
        return SCOPE_SUMMARY
    if value in _THREAD_EVENTS:
        return SCOPE_THREAD
    return SCOPE_LIVE


@dataclass(frozen=True)
class Event:
    event_id: bytes
    global_seq: int
    topic_id: int
    topic_seq: int
    revision: int
    event_type: int
    qos: int
    payload: dict[int, bytes]
    created_at_ms: int


class EventSpine:
    def __init__(self, path, ring_capacity=1000):
        if ring_capacity <= 0:
            raise ValueError("ring_capacity must be positive")
        self.path = Path(path) if path is not None else None
        self.ring = deque(maxlen=ring_capacity)
        self._lock = threading.RLock()
        self._connection = None
        self._global_seq = 0
        self._topic_seq = {}
        self._topic_revision = {}
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._connection = sqlite3.connect(str(self.path), check_same_thread=False)
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    global_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    topic_id INTEGER NOT NULL,
                    topic_seq INTEGER NOT NULL,
                    revision INTEGER NOT NULL,
                    event_type INTEGER NOT NULL,
                    qos INTEGER NOT NULL,
                    payload BLOB NOT NULL,
                    created_at_ms INTEGER NOT NULL
                )
                """
            )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_topic_seq ON events(topic_id, topic_seq)"
            )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at_ms)"
            )
            self._connection.commit()
            row = self._connection.execute(
                "SELECT COALESCE(MAX(global_seq), 0) FROM events"
            ).fetchone()
            self._global_seq = int(row[0] or 0)
            rows = self._connection.execute(
                "SELECT topic_id, topic_seq, revision FROM events "
                "WHERE global_seq IN (SELECT MAX(global_seq) FROM events GROUP BY topic_id)"
            ).fetchall()
            for topic_id, topic_seq, revision in rows:
                self._topic_seq[int(topic_id)] = int(topic_seq)
                self._topic_revision[int(topic_id)] = int(revision)

    def append(self, topic_id, event_type, qos, payload):
        with self._lock:
            topic_id = int(topic_id)
            global_seq = self._global_seq + 1
            topic_seq = self._topic_seq.get(topic_id, 0) + 1
            revision = self._topic_revision.get(topic_id, 0) + 1
            if callable(payload):
                payload = payload(revision)
            event = Event(
                event_id=uuid.uuid4().bytes,
                global_seq=global_seq,
                topic_id=topic_id,
                topic_seq=topic_seq,
                revision=revision,
                event_type=int(event_type),
                qos=int(qos),
                payload=dict(payload),
                created_at_ms=int(time.time() * 1000),
            )
            encoded = encode_tlvs(event.payload.items())
            if self._connection is not None:
                self._connection.execute(
                    "INSERT INTO events(event_id, topic_id, topic_seq, revision, event_type, qos, payload, created_at_ms) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        event.event_id.hex(),
                        event.topic_id,
                        event.topic_seq,
                        event.revision,
                        event.event_type,
                        event.qos,
                        encoded,
                        event.created_at_ms,
                    ),
                )
                self._connection.commit()
            self._global_seq = global_seq
            self._topic_seq[topic_id] = topic_seq
            self._topic_revision[topic_id] = revision
            self.ring.append(event)
            return event

    def latest_event(self, predicate, limit=4096):
        """Return the newest committed event satisfying ``predicate``.

        Used to re-deliver an event that is already represented in the journal
        (for example the connect-time replay of unread messages). Looking the
        event up instead of appending a new one keeps ``global_seq`` /
        ``topic_seq`` authoritative and avoids creating gaps for other
        subscribers. The scan is bounded; a miss simply means "not retained".
        """

        limit = max(1, min(int(limit), 65536))
        with self._lock:
            if self._connection is None:
                for event in reversed(self.ring):
                    try:
                        if predicate(event):
                            return event
                    except Exception:  # noqa: BLE001
                        continue
                return None
            rows = self._connection.execute(
                "SELECT event_id, global_seq, topic_id, topic_seq, revision, "
                "event_type, qos, payload, created_at_ms FROM events "
                "ORDER BY global_seq DESC LIMIT ?",
                (limit,),
            ).fetchall()
        for row in rows:
            event = Event(
                event_id=bytes.fromhex(row[0]),
                global_seq=int(row[1]),
                topic_id=int(row[2]),
                topic_seq=int(row[3]),
                revision=int(row[4]),
                event_type=int(row[5]),
                qos=int(row[6]),
                payload=decode_tlvs(row[7] or b""),
                created_at_ms=int(row[8]),
            )
            try:
                if predicate(event):
                    return event
            except Exception:  # noqa: BLE001 - a bad predicate matches nothing
                continue
        return None

    def events_since(self, cursor, topic_id=0, limit=256):
        cursor = max(0, int(cursor))
        limit = max(1, min(int(limit), 4096))
        with self._lock:
            if self._connection is None:
                return [
                    event
                    for event in list(self.ring)
                    if event.global_seq > cursor
                    and (not topic_id or event.topic_id == int(topic_id))
                ][:limit]
            rows = self._connection.execute(
                "SELECT event_id, global_seq, topic_id, topic_seq, revision, event_type, qos, payload, created_at_ms "
                "FROM events WHERE global_seq > ? AND (? = 0 OR topic_id = ?) ORDER BY global_seq LIMIT ?",
                (cursor, int(topic_id), int(topic_id), limit),
            ).fetchall()
        return [
            Event(
                event_id=bytes.fromhex(row[0]),
                global_seq=int(row[1]),
                topic_id=int(row[2]),
                topic_seq=int(row[3]),
                revision=int(row[4]),
                event_type=int(row[5]),
                qos=int(row[6]),
                payload=decode_tlvs(row[7] or b""),
                created_at_ms=int(row[8]),
            )
            for row in rows
        ]

    def events_latest(self, topic_id=0, limit=256):
        """Return the newest committed events, still in ascending seq order.

        A fresh subscriber has no cursor, so replaying from ``0`` would walk
        the oldest history and never reach current state. Bounded queues are
        the real limit: give the client the most recent window instead.
        """

        limit = max(1, min(int(limit), 4096))
        with self._lock:
            if self._connection is None:
                rows = [
                    event for event in list(self.ring)
                    if not topic_id or event.topic_id == int(topic_id)
                ][-limit:]
                return rows
            query = (
                "SELECT event_id, global_seq, topic_id, topic_seq, revision, "
                "event_type, qos, payload, created_at_ms FROM events "
            )
            params: tuple = ()
            if topic_id:
                query += "WHERE topic_id = ? ORDER BY global_seq DESC LIMIT ?"
                params = (int(topic_id), limit)
            else:
                query += "ORDER BY global_seq DESC LIMIT ?"
                params = (limit,)
            fetched = self._connection.execute(query, params).fetchall()
        events = [
            Event(
                event_id=bytes.fromhex(row[0]),
                global_seq=int(row[1]),
                topic_id=int(row[2]),
                topic_seq=int(row[3]),
                revision=int(row[4]),
                event_type=int(row[5]),
                qos=int(row[6]),
                payload=decode_tlvs(row[7] or b""),
                created_at_ms=int(row[8]),
            )
            for row in fetched
        ]
        events.reverse()
        return events

    def cursor_is_recoverable(self, cursor):
        cursor = max(0, int(cursor))
        if cursor == 0:
            return True
        with self._lock:
            if self._connection is None:
                if not self.ring:
                    return cursor == 0
                return cursor >= self.ring[0].global_seq - 1
            row = self._connection.execute(
                "SELECT MIN(global_seq), MAX(global_seq) FROM events"
            ).fetchone()
            first = int(row[0] or 0)
            last = int(row[1] or 0)
        if last == 0:
            return cursor == 0
        return cursor >= first - 1

    def close(self):
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None
