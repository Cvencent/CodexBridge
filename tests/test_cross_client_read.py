"""One read marker, every client.

The user's complaint was that a task read on one client still nagged the other.
The phone companion and the hardware sessions share one ``ReadStateStore``, so a
read has to fan out live in both directions.
"""
import unittest

from codexbridge.devices.registry import DeviceRegistry
from codexbridge.hub import BridgeHub
from codexbridge.messages_store import MessageRecord
from codexbridge.read_state import ReadStateStore


class FakeSession:
    def __init__(self):
        self.sent = []

    def send(self, message):
        self.sent.append(message)


class FakeCompanion:
    def __init__(self):
        self.events = []

    def publish(self, payload):
        self.events.append(payload)


def result(thread="t1", stamp=5):
    return MessageRecord(f"result:{thread}:{stamp}", thread, "result",
                         "completed", "结果", "完成了", float(stamp))


def live(thread="t1", status="working"):
    return MessageRecord(f"status:{thread}:{status}", thread, "status",
                         status, "状态", "Running tests", 1.0)


class CrossClientReadTest(unittest.TestCase):
    def setUp(self):
        self.store = ReadStateStore(None)
        self.hub = BridgeHub(DeviceRegistry(None), read_state=self.store)
        self.companion = FakeCompanion()
        self.hub.companion_events = self.companion
        for device_id in ("d1", "d2"):
            self.hub.registry.upsert(
                device_id,
                model="FoloOS-AI-Passport",
                supported=["status", "tasks", "results", "read_ack"],
            )
        self.a = FakeSession()
        self.b = FakeSession()
        self.hub.attach("d1", self.a)
        self.hub.attach("d2", self.b)

    def read_states(self, session):
        return [m for m in session.sent if m.get("type") == "read_state"]

    def test_device_read_reaches_other_devices_and_the_phone(self):
        self.hub.publish(result())
        self.hub.handle_device_event("d1", {"type": "read_ack",
                                            "message_id": "result:t1:5"})
        self.assertTrue(self.hub.wait_idle(2))
        states = self.read_states(self.b)
        self.assertEqual(1, len(states))
        self.assertEqual("result:t1:5", states[0]["message_id"])
        self.assertIn("d1", states[0]["read_by"])
        # the sender is never told about its own read
        self.assertEqual([], self.read_states(self.a))
        # and the phone badge is retracted
        self.assertEqual(
            ["thread_status"], [e["type"] for e in self.companion.events])
        self.assertEqual("idle", self.companion.events[0]["status"])
        self.assertEqual("t1", self.companion.events[0]["thread_id"])

    def test_phone_read_clears_the_device_badge(self):
        self.hub.publish(result())
        self.hub.publish(live())
        # This is exactly what the companion does when a phone opens a thread.
        self.store.mark_thread("t1", 5, source="phone-1")
        states = {m["message_id"] for m in self.read_states(self.a)}
        states |= {m["message_id"] for m in self.read_states(self.b)}
        self.assertIn("result:t1:5", states,
                      "the device must retire a badge the phone already opened")

    def test_phone_read_marks_the_message_globally(self):
        self.hub.publish(result())
        self.store.mark_thread("t1", 5, source="phone-1")
        self.assertTrue(self.store.is_message_read("result:t1:5"))

    def test_live_state_is_not_swallowed_by_a_phone_read(self):
        self.hub.publish(live())
        self.store.mark_thread("t1", 99, source="phone-1")
        other = FakeSession()
        self.hub.attach("d3", other)
        self.hub.registry.upsert(
            "d3", model="FoloOS-AI-Passport",
            supported=["status", "tasks", "read_ack"])
        self.hub.replay_state("d3")
        kinds = [m.get("kind") for m in other.sent if m.get("type") == "message"]
        self.assertIn("status", kinds)

    def test_listener_exception_does_not_lose_the_read(self):
        def boom(event):
            raise RuntimeError("client gone")

        self.store.add_listener(boom)
        self.hub.publish(result())
        self.store.mark_thread("t1", 5, source="phone-1")
        self.assertTrue(self.store.is_message_read("result:t1:5"))

    def test_only_devices_with_read_ack_are_told(self):
        self.hub.registry.upsert("d4", model="dotii", supported=["status"])
        quiet = FakeSession()
        self.hub.attach("d4", quiet)
        self.hub.publish(result())
        self.hub.handle_device_event("d1", {"type": "read_ack",
                                            "message_id": "result:t1:5"})
        self.assertTrue(self.hub.wait_idle(2))
        self.assertEqual([], self.read_states(quiet))

    def test_thread_read_without_a_known_thread_only_marks_the_thread(self):
        self.hub.publish(live())
        self.store.mark_thread("unknown-thread", 7, source="phone-1")
        self.assertEqual([], self.read_states(self.a))


if __name__ == "__main__":
    unittest.main()
