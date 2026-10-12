import tempfile
import time
import unittest
from pathlib import Path

from codexbridge.devices.discovery import DiscoveryResult
from codexbridge.devices.manager import DeviceManager
from codexbridge.devices.registry import DeviceRegistry
from codexbridge.hub import BridgeHub
from codexbridge.messages_store import MessageRecord
from tests.fake_device import FakeDevice


class UnifiedProtocolTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.registry = DeviceRegistry(Path(self.tmp.name) / "devices.json")
        self.hub = BridgeHub(self.registry)
        self.hub.publish(
            MessageRecord("m1", "t1", "status", "working", "Task", "Body", 0.0)
        )
        self.manager = DeviceManager(self.registry, self.hub)
        self.addCleanup(self.manager.stop)

    def test_unified_device_receives_replay_and_read_state(self):
        self.registry.upsert(
            "fake-device",
            model="dotii",
            token="abcdefghijklmnop",
            supported=["status", "text", "read_ack"],
        )
        with FakeDevice() as device:
            device.expect_handshake("abcdefghijklmnop")
            device.stall()
            self.manager.sync([self._discovery_for(device)])

            self.assertTrue(
                self._wait_for(
                    lambda: any(
                        item.get("type") == "message"
                        and item.get("message_id") == "m1"
                        for item in device.received
                    )
                )
            )
            self.assertEqual(device.received[0]["type"], "bridge_auth")
            self.assertEqual(device.received[1]["type"], "bridge_ready")
            self.assertEqual(
                device.received[1]["capabilities"],
                ["read_ack", "status", "text"],
            )
            self.assertFalse(
                any(
                    item.get("type") in {"hello", "hello_ok"}
                    for item in device.received
                )
            )

    def test_read_ack_broadcasts_to_other_unified_session(self):
        with FakeDevice(device_id="device-a") as first, FakeDevice(
            device_id="device-b"
        ) as second:
            for device in (first, second):
                device.expect_handshake("abcdefghijklmnop")
                device.stall()
                self.registry.upsert(
                    device.device_id,
                    model="dotii",
                    token="abcdefghijklmnop",
                    supported=["status", "text", "read_ack"],
                )
                self.manager.sync([self._discovery_for(device)])

            self.assertTrue(
                self._wait_for(lambda: "m1" in self._received_message_ids(first))
            )
            self.assertTrue(
                self._wait_for(lambda: "m1" in self._received_message_ids(second))
            )

            first.send_event({"type": "read_ack", "message_id": "m1"})

            self.assertTrue(
                self._wait_for(
                    lambda: any(
                        item.get("type") == "read_state"
                        and "device-a" in item.get("read_by", [])
                        for item in second.received
                    )
                )
            )

    @staticmethod
    def _discovery_for(device: FakeDevice) -> DiscoveryResult:
        return DiscoveryResult(
            host=device.host,
            device="dotii",
            device_id=device.device_id,
            name="Fake Device",
            port=device.port,
            capabilities=["status", "text", "read_ack"],
        )

    @staticmethod
    def _wait_for(predicate) -> bool:
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        return predicate()

    @staticmethod
    def _received_message_ids(device: FakeDevice) -> list[str]:
        with device._received_lock:
            messages = list(device.received)
        return [
            str(item["message_id"])
            for item in messages
            if item.get("type") == "message" and "message_id" in item
        ]
