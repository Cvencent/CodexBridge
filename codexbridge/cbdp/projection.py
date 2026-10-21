from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass

from codexbridge.cbdp.constants import (
    TAG_BODY,
    TAG_CREATED_AT_MS,
    TAG_KIND,
    TAG_MESSAGE_ID,
    TAG_MESSAGE_REF,
    TAG_REVISION,
    TAG_STATUS,
    TAG_THREAD_ID,
    TAG_THREAD_REF,
    TAG_TITLE,
    TAG_TURN_ID,
    TAG_TURN_REF,
    TAG_UPDATED_AT_MS,
)
from codexbridge.cbdp.tlv import (
    decode_bool,
    decode_i64,
    decode_u8,
    decode_u16,
    decode_u64,
    decode_utf8,
)

EVENT_THREAD_CREATED = 0x0101
EVENT_THREAD_UPDATED = 0x0102
EVENT_THREAD_TITLE_CHANGED = 0x0103
EVENT_THREAD_STATUS_CHANGED = 0x0104
EVENT_MESSAGE_APPENDED = 0x0301
EVENT_MESSAGE_UPDATED = 0x0302
EVENT_TURN_COMPLETED = 0x0203
EVENT_TURN_FAILED = 0x0204
EVENT_TURN_INTERRUPTED = 0x0205


@dataclass(frozen=True)
class ProjectedThread:
    thread_id: str
    title: str
    status: int
    revision: int
    created_at_ms: int
    updated_at_ms: int


@dataclass(frozen=True)
class ProjectedMessage:
    message_id: str
    thread_id: str
    turn_id: str
    kind: int
    status: int
    body: str
    revision: int
    created_at_ms: int
    updated_at_ms: int


class ProjectionStore:
    def __init__(self, path=None):
        self.path = path
        self._lock = threading.RLock()
        self._connection = None
        if path is not None:
            self._connection = sqlite3.connect(str(path), check_same_thread=False)
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS threads(
                    thread_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    status INTEGER NOT NULL,
                    revision INTEGER NOT NULL,
                    created_at_ms INTEGER NOT NULL,
                    updated_at_ms INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages(
                    message_id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    kind INTEGER NOT NULL,
                    status INTEGER NOT NULL,
                    body TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    created_at_ms INTEGER NOT NULL,
                    updated_at_ms INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(thread_id, created_at_ms);
                """
            )
            self._connection.commit()

    @staticmethod
    def _text(payload, tag, default=""):
        value = payload.get(tag)
        return default if value is None else decode_utf8(value)

    @staticmethod
    def _number(payload, tag, decode, default=0):
        value = payload.get(tag)
        return default if value is None else decode(value)

    def _upsert_thread(
        self,
        thread_id: str,
        *,
        title: str = "",
        status: int | None = None,
        revision: int | None = None,
        created_at_ms: int | None = None,
        updated_at_ms: int | None = None,
    ) -> None:
        """Create or refresh one thread row from any thread-bearing event.

        Message and turn events carry the thread id as well. Projecting only
        the dedicated thread events left ``threads`` empty whenever the host
        had not emitted one, which made every consumer that reads that table
        see zero conversations.
        """

        if not thread_id or self._connection is None:
            return
        previous = self._thread_row(thread_id)
        if previous is None:
            row = (
                thread_id,
                str(title or ""),
                max(0, int(status or 0)),
                max(0, int(revision or 0)),
                max(0, int(created_at_ms or 0)),
                max(0, int(updated_at_ms or 0)),
            )
        else:
            row = (
                thread_id,
                str(title) if str(title or "") else previous[1],
                max(0, int(status)) if status is not None else previous[2],
                max(int(revision or 0), previous[3]),
                max(0, int(created_at_ms or 0)) or previous[4],
                max(0, int(updated_at_ms or 0)) or previous[5],
            )
        self._connection.execute(
            "INSERT OR REPLACE INTO threads VALUES(?,?,?,?,?,?)",
            row,
        )
        self._connection.commit()

    def apply(self, event) -> None:
        payload = event.payload
        with self._lock:
            if event.event_type in {
                EVENT_THREAD_CREATED,
                EVENT_THREAD_UPDATED,
                EVENT_THREAD_TITLE_CHANGED,
                EVENT_THREAD_STATUS_CHANGED,
            }:
                thread_id = self._text(payload, TAG_THREAD_ID)
                if not thread_id:
                    return
                previous = self._thread_row(thread_id)
                title = self._text(payload, TAG_TITLE, previous[1] if previous else "")
                status = self._number(
                    payload, TAG_STATUS, decode_u16, previous[2] if previous else 0
                )
                revision = self._number(
                    payload, TAG_REVISION, decode_u64, previous[3] if previous else event.revision
                )
                created = self._number(
                    payload, TAG_CREATED_AT_MS, decode_i64, previous[4] if previous else event.created_at_ms
                )
                updated = self._number(
                    payload, TAG_UPDATED_AT_MS, decode_i64, previous[5] if previous else event.created_at_ms
                )
                if self._connection is not None:
                    self._upsert_thread(
                        thread_id,
                        title=title,
                        status=status,
                        revision=revision,
                        created_at_ms=created,
                        updated_at_ms=updated,
                    )
                return
            if event.event_type in {EVENT_MESSAGE_APPENDED, EVENT_MESSAGE_UPDATED}:
                message_id = self._text(payload, TAG_MESSAGE_ID)
                if not message_id:
                    return
                previous = self._message_row(message_id)
                thread_id = self._text(payload, TAG_THREAD_ID, previous[1] if previous else "")
                turn_id = self._text(payload, TAG_TURN_ID, previous[2] if previous else "")
                kind = self._number(payload, TAG_KIND, decode_u8, previous[3] if previous else 0)
                status = self._number(payload, TAG_STATUS, decode_u16, previous[4] if previous else 0)
                body = self._text(payload, TAG_BODY, previous[5] if previous else "")
                revision = self._number(
                    payload, TAG_REVISION, decode_u64, previous[6] if previous else event.revision
                )
                created = self._number(
                    payload, TAG_CREATED_AT_MS, decode_i64, previous[7] if previous else event.created_at_ms
                )
                updated = self._number(
                    payload, TAG_UPDATED_AT_MS, decode_i64, previous[8] if previous else event.created_at_ms
                )
                if self._connection is not None:
                    self._connection.execute(
                        "INSERT OR REPLACE INTO messages VALUES(?,?,?,?,?,?,?,?,?)",
                        (message_id, thread_id, turn_id, kind, status, body, revision, created, updated),
                    )
                    self._connection.commit()
                    self._upsert_thread(
                        thread_id,
                        title=self._text(payload, TAG_TITLE),
                        # A message's status describes the message, not the
                        # thread, so leave the thread status untouched.
                        revision=revision,
                        created_at_ms=created,
                        updated_at_ms=updated,
                    )
                return
            if event.event_type in {
                EVENT_TURN_COMPLETED,
                EVENT_TURN_FAILED,
                EVENT_TURN_INTERRUPTED,
            }:
                thread_id = self._text(payload, TAG_THREAD_ID)
                if not thread_id:
                    return
                completed = self._number(
                    payload,
                    TAG_UPDATED_AT_MS,
                    decode_i64,
                    event.created_at_ms,
                )
                self._upsert_thread(
                    thread_id,
                    title=self._text(payload, TAG_TITLE)
                    or self._text(payload, TAG_SUMMARY),
                    status=self._number(payload, TAG_STATUS, decode_u16, 0),
                    revision=self._number(
                        payload, TAG_REVISION, decode_u64, event.revision
                    ),
                    updated_at_ms=completed,
                )

    def _thread_row(self, thread_id):
        if self._connection is None:
            return None
        return self._connection.execute(
            "SELECT * FROM threads WHERE thread_id=?", (thread_id,)
        ).fetchone()

    def _message_row(self, message_id):
        if self._connection is None:
            return None
        return self._connection.execute(
            "SELECT * FROM messages WHERE message_id=?", (message_id,)
        ).fetchone()

    def get_thread(self, thread_id):
        with self._lock:
            row = self._thread_row(thread_id)
        return None if row is None else ProjectedThread(*row)

    def get_message(self, message_id):
        with self._lock:
            row = self._message_row(message_id)
        return None if row is None else ProjectedMessage(*row)

    def thread_object(self, thread_id):
        thread = self.get_thread(thread_id)
        if thread is None:
            return None
        return {
            TAG_THREAD_ID: thread.thread_id,
            TAG_TITLE: thread.title,
            TAG_STATUS: thread.status,
            TAG_REVISION: thread.revision,
            TAG_CREATED_AT_MS: thread.created_at_ms,
            TAG_UPDATED_AT_MS: thread.updated_at_ms,
        }

    def rebuild(self, events) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.execute("DELETE FROM threads")
                self._connection.execute("DELETE FROM messages")
                self._connection.commit()
            for event in events:
                self.apply(event)

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None


Projection = ProjectionStore
