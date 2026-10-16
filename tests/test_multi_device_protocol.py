"""One protocol, several devices, including a phone.

This is the shape the user asked for: a notification is not written per client,
it is fanned out to whoever is subscribed, each receiving content sized for its
own screen. A read made by any client -- hardware or phone -- clears the badge
for all of them.
"""
import tempfile
import time
import unittest
from pathlib import Path

from codexbridge.devices.discovery import DiscoveryResult
from codexbridge.devices.manager import DeviceManager
from codexbridge.devices.registry import DeviceRegistry
from codexbridge.hub import BridgeHub
from codexbridge.messages_store import MessageRecord
from codexbridge.read_state import ReadStateStore
from tests.fake_device import FakeDevice

TOKEN = "abcdefghijklmnop"
# The record under test is a task result, so the joining devices declare the
# `results` capability as well -- the host must not send it otherwise.
DEVICE_CAPS = ["status", "text", "read_ack", "results"]
BODY = "第一段结论。" * 60


class FakePhone:
    """The companion service only needs ``publish`` to receive retractions."""

    def __init__(self):
        self.events = []

    def publish(self, payload):
        self.events.append(payload)


class MultiDeviceProtocolTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.read_state = ReadStateStore(root / "read-state.json")
        self.registry = DeviceRegistry(root / "devices.json")
        self.hub = BridgeHub(self.registry, read_state=self.read_state)
        self.phone = FakePhone()
        self.hub.companion_events = self.phone
        self.hub.publish(
            MessageRecord("result:t1:7", "t1", "result", "completed",
                          "结果", BODY, 7.0)
        )
        self.hub.publish(
            MessageRecord("status:t1:working", "t1", "status", "working",
                          "状态", "Running tests", 1.0,
                          data={"task": {"turn_id": "t-1",
                                         "current_action": "Running tests",
                                         "plan_completed": 1,
                                         "plan_total": 4}})
        )
        self.manager = DeviceManager(self.registry, self.hub)
        self.addCleanup(self.manager.stop)

    def _join(self, device: FakeDevice, capabilities=None) -> FakeDevice:
        declared = list(capabilities or device._capabilities)
        device.expect_handshake(TOKEN)
        device.stall()
        self.registry.upsert(
            device.device_id,
            model="dotii",
            token=TOKEN,
            supported=declared,
        )
        self.manager.sync([DiscoveryResult(
            host=device.host,
            device="dotii",
            device_id=device.device_id,
            name=device.device_id,
            port=device.port,
            capabilities=declared,
        )])
        self._wait(lambda: any(
            m.get("type") == "message" for m in device.received))
        return device

    @staticmethod
    def _wait(predicate, timeout=3.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        return predicate()

    def bodies(self, device, message_id="result:t1:7"):
        parts = [m.get("body", "") for m in device.received
                 if m.get("type") == "message" and m.get("message_id") == message_id]
        return "".join(parts)

    def test_each_device_gets_content_sized_for_its_own_screen(self):
        with FakeDevice(device_id="esp32", capabilities=DEVICE_CAPS,
                     max_text_chars=48) as small, \
                FakeDevice(device_id="desktop", capabilities=DEVICE_CAPS) as big:
            self._join(small)
            self._join(big)
            small_body = self.bodies(small)
            big_body = self.bodies(big)
            self.assertTrue(small_body)
            self.assertEqual(len(small_body), 48, small_body[-6:])
            self.assertTrue(small_body.endswith("..."))
            self.assertEqual(len(big_body), len(BODY))
            # identical protocol, one field apart
            self.assertEqual({m["type"] for m in small.received[:1]}, {"bridge_auth"})

    def test_a_read_on_hardware_clears_the_phone_and_the_other_device(self):
        with FakeDevice(device_id="esp32", capabilities=DEVICE_CAPS) as small, \
                FakeDevice(device_id="desktop", capabilities=DEVICE_CAPS) as big:
            self._join(small)
            self._join(big)
            small.send_event({"type": "read_ack", "message_id": "result:t1:7"})
            self._wait(lambda: any(
                m.get("type") == "read_state" and m.get("message_id") == "result:t1:7"
                and "esp32" in m.get("read_by", [])
                for m in big.received))
            self.assertTrue(any(
                m.get("type") == "read_state" and "esp32" in m.get("read_by", [])
                for m in big.received), big.received)
            self.assertEqual(
                [("thread_status", "idle")] ,
                [(e["type"], e["status"]) for e in self.phone.events])
            self.assertTrue(self.read_state.is_message_read("result:t1:7"))

    def test_a_read_on_the_phone_clears_every_device(self):
        with FakeDevice(device_id="esp32", capabilities=DEVICE_CAPS) as small, \
                FakeDevice(device_id="desktop", capabilities=DEVICE_CAPS) as big:
            self._join(small)
            self._join(big)
            self.read_state.mark_thread("t1", 7, source="phone-1")
            for device in (small, big):
                self.assertTrue(self._wait(lambda d=device: any(
                    m.get("type") == "read_state"
                    and m.get("message_id") == "result:t1:7"
                    for m in d.received)), device.device_id)

    def test_state_pull_returns_live_state_even_after_everyone_read_it(self):
        with FakeDevice(device_id="esp32", capabilities=DEVICE_CAPS) as small:
            self._join(small)
            self.read_state.mark_thread("t1", 99, source="phone-1")
            small.send_event({"type": "codex_state_request"})
            self.assertTrue(self._wait(lambda: any(
                m.get("type") == "codex_status_snapshot" for m in small.received)))
            snap = [m for m in small.received
                    if m.get("type") == "codex_status_snapshot"][-1]
            self.assertEqual("working", snap["status"])
            self.assertEqual("Running tests", snap["current_action"])
            self.assertEqual(1, snap["plan_completed"])
            self.assertEqual(4, snap["plan_total"])
            counters = [m for m in small.received if m.get("type") == "codex_state"]
            self.assertTrue(counters)

    def test_capabilities_declared_by_a_device_are_the_only_ones_used(self):
        with FakeDevice(device_id="audio-only",
                        capabilities=["status", "read_ack"]) as dev:
            self._join(dev, capabilities=["status", "read_ack"])
            ids = {m.get("message_id") for m in dev.received
                   if m.get("type") == "message"}
            self.assertIn("status:t1:working", ids)
            self.assertNotIn("result:t1:7", ids, "no results capability")
            self.assertEqual(
                {"read_ack", "status"},
                set(dev.received[1]["capabilities"]))

    def test_new_device_joining_later_gets_the_live_state_not_the_history(self):
        with FakeDevice(device_id="first", capabilities=DEVICE_CAPS) as first:
            self._join(first)
            first.send_event({"type": "read_ack", "message_id": "result:t1:7"})
            self._wait(lambda: self.read_state.is_message_read("result:t1:7"))
            with FakeDevice(device_id="second", capabilities=DEVICE_CAPS) as second:
                self._join(second)
                ids = {m.get("message_id") for m in second.received
                       if m.get("type") == "message"}
                self.assertNotIn("result:t1:7", ids)
                self.assertIn("status:t1:working", ids,
                              "live state must still arrive on reconnect")


if __name__ == "__main__":
    unittest.main()
