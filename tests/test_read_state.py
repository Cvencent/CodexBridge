"""Tests for the cross-device CodexBridge read marker."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codexbridge.read_state import ReadStateStore


class ReadStateStoreTests(unittest.TestCase):
    def test_thread_read_marker_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "read-state.json"
            store = ReadStateStore(path)
            store.mark_thread("thread-1", 1000)
            store.save()

            loaded = ReadStateStore(path)
            loaded.load()

            self.assertTrue(loaded.is_thread_read("thread-1", 1000))
            self.assertFalse(loaded.is_thread_read("thread-1", 1001))

    def test_older_marker_cannot_replace_newer_completion(self) -> None:
        store = ReadStateStore(None)
        store.mark_thread("thread-1", 2000)
        store.mark_thread("thread-1", 1000)

        self.assertEqual(store.thread_read_at("thread-1"), 2000)

    def test_repair_implausible_thread_markers_uses_current_time(self) -> None:
        store = ReadStateStore(None)
        store.mark_thread("thread-1", 4549)
        store.mark_thread("thread-2", 1_792_919_304)

        repaired = store.repair_implausible_thread_markers(now=1_800_000_000)

        self.assertEqual(1, repaired)
        self.assertEqual(1_800_000_000, store.thread_read_at("thread-1"))
        self.assertEqual(1_792_919_304, store.thread_read_at("thread-2"))

    def test_message_read_marker_is_global(self) -> None:
        store = ReadStateStore(None)
        store.mark_message("result:thread-1:1000")

        self.assertTrue(store.is_message_read("result:thread-1:1000"))
        self.assertFalse(store.is_message_read("result:thread-1:1001"))

    def test_listener_is_notified_once_when_thread_becomes_read(self) -> None:
        store = ReadStateStore(None)
        events = []
        store.add_listener(events.append)

        store.mark_thread("thread-1", 2000, source="phone")
        store.mark_thread("thread-1", 2000, source="phone")
        store.mark_thread("thread-1", 1000, source="phone")

        self.assertEqual(
            events,
            [
                {
                    "thread_id": "thread-1",
                    "completed_at": 2000,
                    "message_id": "",
                    "source": "phone",
                }
            ],
        )

    def test_notify_false_marks_read_without_rebroadcasting(self) -> None:
        store = ReadStateStore(None)
        events = []
        store.add_listener(events.append)

        store.mark_message("result:thread-1:2000", source="phone", notify=False)

        self.assertTrue(store.is_message_read("result:thread-1:2000"))
        self.assertEqual(events, [])

    def test_same_bound_listener_is_registered_only_once(self) -> None:
        store = ReadStateStore(None)
        events = []

        class Sink:
            def handle(self, event) -> None:
                events.append(event)

        sink = Sink()
        store.add_listener(sink.handle)
        store.add_listener(sink.handle)

        store.mark_message("result:thread-1:2000", source="phone")

        self.assertEqual(1, len(events))


if __name__ == "__main__":
    unittest.main()
