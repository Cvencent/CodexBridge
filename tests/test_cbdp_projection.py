import tempfile
import unittest
from pathlib import Path

from codexbridge.cbdp.constants import (
    QOS_CRITICAL,
    QOS_STATE,
    TAG_BODY,
    TAG_CREATED_AT_MS,
    TAG_CURRENT_ACTION,
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
from codexbridge.cbdp.events import EventSpine
from codexbridge.cbdp.projection import ProjectionStore
from codexbridge.cbdp.tlv import i64, u8, u16, u32, u64, utf8

THREAD_CREATED = 0x0101
THREAD_TITLE_CHANGED = 0x0103
MESSAGE_APPENDED = 0x0301
THREAD_TOPIC = 0x10000001


def thread_payload(title="First", status=1, revision=1):
    return {
        TAG_THREAD_REF: u32(1),
        TAG_THREAD_ID: utf8("thread-1"),
        TAG_TITLE: utf8(title),
        TAG_STATUS: u16(status),
        TAG_REVISION: u64(revision),
        TAG_CREATED_AT_MS: i64(1000),
        TAG_UPDATED_AT_MS: i64(1000 + revision),
    }


def message_payload(body="hello", revision=1):
    return {
        TAG_THREAD_REF: u32(1),
        TAG_THREAD_ID: utf8("thread-1"),
        TAG_TURN_REF: u32(2),
        TAG_TURN_ID: utf8("turn-1"),
        TAG_MESSAGE_REF: u32(3),
        TAG_MESSAGE_ID: utf8("message-1"),
        TAG_KIND: u8(1),
        TAG_STATUS: u16(2),
        TAG_BODY: utf8(body),
        TAG_REVISION: u64(revision),
        TAG_CREATED_AT_MS: i64(2000),
        TAG_UPDATED_AT_MS: i64(2000 + revision),
    }


class ProjectionStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.events = EventSpine(self.root / "events.sqlite3")
        self.addCleanup(self.events.close)
        self.projection = ProjectionStore(self.root / "projection.sqlite3")
        self.addCleanup(self.projection.close)

    def append_and_apply(self, event_type, payload):
        event = self.events.append(THREAD_TOPIC, event_type, QOS_STATE, payload)
        self.projection.apply(event)
        return event

    def test_thread_and_message_are_projected(self):
        self.append_and_apply(THREAD_CREATED, thread_payload())
        self.append_and_apply(MESSAGE_APPENDED, message_payload())

        thread = self.projection.get_thread("thread-1")
        self.assertEqual(thread.title, "First")
        self.assertEqual(thread.status, 1)
        self.assertEqual(thread.revision, 1)
        message = self.projection.get_message("message-1")
        self.assertEqual(message.thread_id, "thread-1")
        self.assertEqual(message.body, "hello")
        self.assertEqual(message.kind, 1)
        self.assertEqual(message.status, 2)

    def test_message_event_creates_the_thread_row_when_no_thread_event_arrived(self):
        # Reproduces the real corruption where ``messages`` had rows but
        # ``threads`` stayed empty because the host never emitted 0x0101.
        self.append_and_apply(MESSAGE_APPENDED, message_payload())

        thread = self.projection.get_thread("thread-1")
        self.assertIsNotNone(thread)
        self.assertEqual(thread.status, 0)
        self.assertEqual(thread.updated_at_ms, 2001)

    def test_thread_title_revision_is_monotonic(self):
        self.append_and_apply(THREAD_CREATED, thread_payload())
        event = self.events.append(
            THREAD_TOPIC,
            THREAD_TITLE_CHANGED,
            QOS_STATE,
            thread_payload(title="Renamed", revision=2),
        )
        self.projection.apply(event)
        self.assertEqual(self.projection.get_thread("thread-1").title, "Renamed")
        self.assertGreater(event.topic_seq, 1)
        self.assertGreater(event.revision, 1)

    def test_rebuild_from_event_log_restores_projection(self):
        self.append_and_apply(THREAD_CREATED, thread_payload())
        self.append_and_apply(MESSAGE_APPENDED, message_payload())

        rebuilt = ProjectionStore(self.root / "rebuilt.sqlite3")
        self.addCleanup(rebuilt.close)
        rebuilt.rebuild(self.events.events_since(0))

        self.assertEqual(rebuilt.get_thread("thread-1").title, "First")
        self.assertEqual(rebuilt.get_message("message-1").body, "hello")

    def test_projection_survives_process_style_reopen(self):
        self.append_and_apply(THREAD_CREATED, thread_payload())
        self.projection.close()

        reopened = ProjectionStore(self.root / "projection.sqlite3")
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.get_thread("thread-1").title, "First")

    def test_snapshot_payload_has_no_second_state_store(self):
        self.append_and_apply(THREAD_CREATED, thread_payload())
        snapshot = self.projection.thread_object("thread-1")
        self.assertEqual(snapshot[TAG_THREAD_ID], "thread-1")
        self.assertEqual(snapshot[TAG_TITLE], "First")
        self.assertNotIn("status_text", snapshot)


if __name__ == "__main__":
    unittest.main()
