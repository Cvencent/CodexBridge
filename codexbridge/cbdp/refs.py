from __future__ import annotations

import sqlite3
import threading


REF_MAX = 0x0FFFFFFF
THREAD_TOPIC_BASE = 0x10000000
THREAD_TOPIC_MAX = 0x1FFFFFFF

SYSTEM_BRIDGE_TOPIC = 0x50000000
SYSTEM_DEVICES_TOPIC = 0x50000001
SYSTEM_RESYNC_TOPIC = 0x50000002
SYSTEM_THREAD_SUMMARIES_TOPIC = 0x50000003


class ReferenceRegistry:
    """Persistent string-ID to CBDP reference mapping.

    This is addressing metadata, not a second business-state store. The same
    mapping is required on every replay so a gateway translation cannot create
    a new object merely because the same legacy event arrived twice.
    """

    def __init__(self, path=None):
        self._lock = threading.RLock()
        self._connection = None
        self._next_ref = 1
        if path is not None:
            self._connection = sqlite3.connect(str(path), check_same_thread=False)
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS refs(
                    kind TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    ref INTEGER NOT NULL,
                    PRIMARY KEY(kind, external_id),
                    UNIQUE(kind, ref)
                )
                """
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS thread_topics(
                    thread_id TEXT PRIMARY KEY,
                    topic_id INTEGER NOT NULL UNIQUE
                )
                """
            )
            self._connection.commit()
            row = self._connection.execute(
                "SELECT COALESCE(MAX(ref), 0) FROM refs"
            ).fetchone()
            self._next_ref = max(1, int(row[0] or 0) + 1)
            row = self._connection.execute(
                "SELECT COALESCE(MAX(topic_id), 0) FROM thread_topics"
            ).fetchone()
            self._next_topic = max(THREAD_TOPIC_BASE, int(row[0] or 0) + 1)
        else:
            self._next_topic = THREAD_TOPIC_BASE

    @staticmethod
    def _kind(kind: str) -> str:
        value = str(kind or "").strip().lower()
        if value not in {"thread", "turn", "message", "approval", "device"}:
            raise ValueError("unsupported reference kind")
        return value

    def ref_for(self, kind: str, external_id: str) -> int:
        kind = self._kind(kind)
        external_id = str(external_id or "")
        if not external_id:
            return 0
        with self._lock:
            if self._connection is not None:
                row = self._connection.execute(
                    "SELECT ref FROM refs WHERE kind=? AND external_id=?",
                    (kind, external_id),
                ).fetchone()
                if row is not None:
                    return int(row[0])
            if self._next_ref > REF_MAX:
                raise OverflowError("reference space exhausted")
            ref = self._next_ref
            self._next_ref += 1
            if self._connection is not None:
                self._connection.execute(
                    "INSERT INTO refs(kind, external_id, ref) VALUES(?,?,?)",
                    (kind, external_id, ref),
                )
                self._connection.commit()
            return ref

    def external_id(self, kind: str, ref: int) -> str:
        kind = self._kind(kind)
        with self._lock:
            if self._connection is None:
                return ""
            row = self._connection.execute(
                "SELECT external_id FROM refs WHERE kind=? AND ref=?",
                (kind, int(ref)),
            ).fetchone()
        return "" if row is None else str(row[0])

    def topic_for(self, thread_id: str) -> int:
        thread_id = str(thread_id or "")
        if not thread_id:
            return 0
        with self._lock:
            if self._connection is not None:
                row = self._connection.execute(
                    "SELECT topic_id FROM thread_topics WHERE thread_id=?",
                    (thread_id,),
                ).fetchone()
                if row is not None:
                    return int(row[0])
            if self._next_topic > THREAD_TOPIC_MAX:
                raise OverflowError("thread topic space exhausted")
            topic_id = self._next_topic
            self._next_topic += 1
            if self._connection is not None:
                self._connection.execute(
                    "INSERT INTO thread_topics(thread_id, topic_id) VALUES(?,?)",
                    (thread_id, topic_id),
                )
                self._connection.commit()
            return topic_id

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None
