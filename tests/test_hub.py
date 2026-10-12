import unittest

from codexbridge.devices.registry import DeviceRegistry
from codexbridge.hub import BridgeHub
from codexbridge.messages_store import MessageRecord


class FakeSession:
    def __init__(self):
        self.sent = []

    def send(self, message):
        self.sent.append(message)


class HubTest(unittest.TestCase):
    def setUp(self):
        self.hub = BridgeHub(DeviceRegistry(None))
        self.hub.registry.upsert(
            "d1",
            model="dotii",
            supported=["status", "text", "tasks", "results", "read_ack"],
        )
        self.hub.registry.upsert(
            "d2",
            model="FoloOS-AI-Passport",
            supported=["status", "text", "read_ack"],
        )
        self.hub.registry.upsert(
            "d3",
            model="dotii",
            supported=["status"],
        )
        self.s1, self.s2, self.s3 = FakeSession(), FakeSession(), FakeSession()
        self.hub.attach("d1", self.s1)
        self.hub.attach("d2", self.s2)
        self.hub.attach("d3", self.s3)

    def test_publish_only_reaches_capable_devices(self):
        self.hub.publish(
            MessageRecord("m1", "t1", "result", "completed", "标题", "正文", 0.0)
        )
        self.assertEqual([message["type"] for message in self.s1.sent], ["message"])
        self.assertEqual(self.s2.sent, [])

    def test_read_ack_broadcasts_read_state(self):
        self.hub.publish(
            MessageRecord("m1", "t1", "result", "completed", "标题", "正文", 0.0)
        )
        self.s1.sent.clear()
        self.hub.handle_device_event("d1", {"type": "read_ack", "message_id": "m1"})
        self.assertEqual(self.s2.sent[-1]["type"], "read_state")
        self.assertEqual(self.s2.sent[-1]["read_by"], ["d1"])

    def test_read_state_is_filtered_by_read_ack_capability(self):
        self.hub.publish(
            MessageRecord("m1", "t1", "result", "completed", "标题", "正文", 0.0)
        )
        self.s2.sent.clear()
        self.s3.sent.clear()
        self.hub.handle_device_event("d1", {"type": "read_ack", "message_id": "m1"})
        self.assertEqual(self.s2.sent[-1]["type"], "read_state")
        self.assertEqual(self.s3.sent, [])

    def test_capability_less_event_is_rejected(self):
        self.hub.handle_device_event("d2", {"type": "voice_command", "text": "hi"})
        self.assertEqual(self.s2.sent[-1]["type"], "error")

    def test_reconnect_state_includes_unread_messages(self):
        self.hub.publish(
            MessageRecord("m1", "t1", "result", "completed", "标题", "正文", 0.0)
        )
        state = self.hub.state_for("d1")
        self.assertEqual([message["message_id"] for message in state["messages"]], ["m1"])

    def test_structured_data_is_sent_only_to_capable_devices(self):
        record = MessageRecord(
            "m-data",
            "t1",
            "result",
            "completed",
            "标题",
            "正文",
            1.0,
            data={"codex": {"task": {"status": "working"}}},
        )
        self.hub.publish(record)
        self.assertEqual(self.s1.sent[-1]["data"], record.data)
        self.assertEqual(self.s2.sent, [])
