"""Desktop Codex reads must join the shared cross-client read marker."""

import json
import os
import tempfile
import unittest
from pathlib import Path

from codexbridge.desktop_read import sync_desktop_read_state
from codexbridge.devices.registry import DeviceRegistry
from codexbridge.hub import BridgeHub
from codexbridge.messages_store import MessageRecord
from codexbridge.read_state import ReadStateStore


class FakeSession:
    is_cbdp = False

    def __init__(self):
        self.sent = []

    def send(self, message):
        self.sent.append(message)


class FakeCompanion:
    def __init__(self):
        self.events = []

    def publish(self, payload):
        self.events.append(payload)


class DesktopReadSyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = ReadStateStore(None)
        self.hub = BridgeHub(DeviceRegistry(None), read_state=self.store)
        self.companion = FakeCompanion()
        self.hub.companion_events = self.companion
        self.hub.registry.upsert(
            "esp",
            model="dotii",
            supported=["status", "tasks", "results", "read_ack"],
        )
        self.session = FakeSession()
        self.hub.attach("esp", self.session)

    def write_desktop(self, activity, unread=None, filename="desktop.json"):
        path = self.root / filename
        payload = {
            "electron-persisted-atom-state": {
                "thread-user-activity-times-v1": activity,
            },
            "electron-thread-read-state-v1": {
                "version": 1,
                "unreadByIdentity": {
                    "identity": {"local:profile": sorted(unread or [])}
                },
            },
        }
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def write_desktop_without_activity(self, known_thread="", filename="desktop.json"):
        path = self.root / filename
        atom = {"thread-user-activity-times-v1": {}}
        if known_thread:
            atom[f"thread-client-id-v1:local%3A{known_thread}"] = "client-thread"
        payload = {
            "electron-persisted-atom-state": atom,
            "electron-thread-read-state-v1": {
                "version": 1,
                "unreadByIdentity": {"identity": {"local:profile": []}},
            },
        }
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def publish_result(self, thread="t1", stamp=100):
        self.hub.publish(
            MessageRecord(
                f"result:{thread}:{int(stamp)}",
                thread,
                "result",
                "completed",
                "结果",
                "完成了",
                float(stamp),
            )
        )

    def test_desktop_read_advances_global_read_and_retracts_badges(self):
        self.publish_result(stamp=100)
        self.assertEqual(1, self.hub.counts()["completed_unread"])
        path = self.write_desktop(
            {'["local","t1"]': 105_000},
        )

        changed = sync_desktop_read_state(self.store, path, store=self.hub.store)

        self.assertEqual(1, changed)
        self.assertTrue(self.store.is_message_read("result:t1:100"))
        self.assertEqual(0, self.hub.counts()["completed_unread"])
        self.assertTrue(any(
            message.get("type") == "read_state"
            and message.get("message_id") == "result:t1:100"
            for message in self.session.sent
        ))
        self.assertEqual(
            ("thread_status", "idle"),
            (self.companion.events[-1]["type"], self.companion.events[-1]["status"]),
        )

    def test_desktop_still_unread_does_not_advance_the_marker(self):
        self.publish_result(stamp=100)
        path = self.write_desktop(
            {'["local","t1"]': 105_000},
            unread={"t1"},
        )

        self.assertEqual(
            0, sync_desktop_read_state(self.store, path, store=self.hub.store)
        )
        self.assertFalse(self.store.is_message_read("result:t1:100"))
        self.assertEqual(1, self.hub.counts()["completed_unread"])

    def test_desktop_read_decision_is_not_guessed_from_an_unrelated_write(self):
        self.publish_result(stamp=100)
        path = self.write_desktop(
            {'["local","t1"]': 50_000},
        )
        # Pretend the desktop snapshot predates the result: the desktop has
        # not classified it yet, so Bridge must not clear it early.
        old = self.root / "desktop-old.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        old.write_text(json.dumps(payload), encoding="utf-8")
        os.utime(old, (50.0, 50.0))

        self.assertEqual(0, sync_desktop_read_state(self.store, old, store=self.hub.store))
        self.assertFalse(self.store.is_message_read("result:t1:100"))
        self.assertEqual(1, self.hub.counts()["completed_unread"])

    def test_activity_before_completion_still_reads_after_desktop_removes_it_from_unread(self):
        self.publish_result(stamp=100)
        path = self.write_desktop(
            {'["local","t1"]': 50_000},
        )

        self.assertEqual(1, sync_desktop_read_state(self.store, path, store=self.hub.store))
        self.assertTrue(self.store.is_message_read("result:t1:100"))
        self.assertEqual(0, self.hub.counts()["completed_unread"])

    def test_desktop_known_thread_without_activity_follows_blue_dot_state(self):
        self.publish_result(stamp=100)
        path = self.write_desktop_without_activity(known_thread="t1")

        changed = sync_desktop_read_state(self.store, path, store=self.hub.store)

        self.assertEqual(1, changed)
        self.assertTrue(self.store.is_message_read("result:t1:100"))
        self.assertEqual(0, self.hub.counts()["completed_unread"])

    def test_phone_only_thread_without_desktop_tracking_stays_unread(self):
        self.publish_result(stamp=100)
        path = self.write_desktop_without_activity()

        changed = sync_desktop_read_state(self.store, path, store=self.hub.store)

        self.assertEqual(0, changed)
        self.assertFalse(self.store.is_message_read("result:t1:100"))
        self.assertEqual(1, self.hub.counts()["completed_unread"])

    def test_remote_host_activity_is_not_treated_as_a_local_read(self):
        self.publish_result(stamp=100)
        path = self.write_desktop(
            {'["remote","t1"]': 105_000},
        )

        self.assertEqual(
            0, sync_desktop_read_state(self.store, path, store=self.hub.store)
        )
        self.assertFalse(self.store.is_message_read("result:t1:100"))
        self.assertEqual(1, self.hub.counts()["completed_unread"])

    def test_malformed_desktop_state_is_ignored(self):
        self.publish_result(stamp=100)
        path = self.root / "broken.json"
        path.write_text("{not json", encoding="utf-8")

        self.assertEqual(
            0, sync_desktop_read_state(self.store, path, store=self.hub.store)
        )
        self.assertEqual(1, self.hub.counts()["completed_unread"])


if __name__ == "__main__":
    unittest.main()
