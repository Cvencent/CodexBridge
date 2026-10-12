import tempfile
import threading
import time
import unittest
from pathlib import Path

from codexbridge.devices.discovery import DiscoveryResult
from codexbridge.devices.manager import DeviceManager
from codexbridge.devices.registry import DeviceRegistry
from codexbridge.hub import BridgeHub
from codexbridge.messages_store import MessageRecord


class FakeSession:
    online = threading.Event()
    created = None
    fail_before_online = False
    info_name = "Desk"

    def __init__(self, host, port, token, **kwargs):
        self.host = host
        self.port = port
        self.token = token
        self.kwargs = kwargs
        self.sent = []
        self.info_capabilities = None
        self.stopped = threading.Event()
        FakeSession.created = self

    def run_once(self):
        if self.fail_before_online:
            return
        self.info_capabilities = self.kwargs["on_device_info"](
            {
                "device_id": "d1",
                "model": "dotii",
                "name": FakeSession.info_name,
                "firmware": "test",
                "capabilities": [
                    "status",
                    "text",
                    "results",
                    "read_ack",
                ],
            }
        )
        self.kwargs["on_online"]()
        FakeSession.online.set()
        self.stopped.wait(2.0)
        self.kwargs["on_offline"]()

    def send(self, message):
        self.sent.append(message)

    def stop(self):
        self.stopped.set()


class DeviceManagerTest(unittest.TestCase):
    def setUp(self):
        FakeSession.online.clear()
        FakeSession.created = None
        FakeSession.fail_before_online = False
        FakeSession.info_name = "Desk"
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.registry = DeviceRegistry(Path(tmp.name) / "devices.json")
        self.registry.upsert(
            "d1",
            model="dotii",
            token="abcdefghijklmnop",
            supported=["status", "text", "results", "read_ack"],
        )
        self.hub = BridgeHub(self.registry)
        self.hub.publish(
            MessageRecord("m1", "t1", "result", "completed", "t", "b", 0.0)
        )
        self.manager = DeviceManager(
            self.registry,
            self.hub,
            session_factory=FakeSession,
        )
        self.addCleanup(self.manager.stop)

    def test_discovery_starts_session_and_replays_unread(self):
        result = DiscoveryResult(
            host="127.0.0.1",
            device="dotii",
            device_id="d1",
            name="Desk",
            port=8765,
            capabilities=["status", "text", "results", "read_ack"],
        )
        self.manager.sync([result])
        self.assertTrue(FakeSession.online.wait(1.0))
        self.assertNotIn("protocol", FakeSession.created.kwargs)
        self.assertNotIn("capabilities", FakeSession.created.kwargs)
        self.assertNotIn("on_hello", FakeSession.created.kwargs)
        self.assertIn("on_device_info", FakeSession.created.kwargs)
        self.assertEqual(
            FakeSession.created.info_capabilities,
            {"status", "text", "results", "read_ack"},
        )
        self.assertEqual(self.registry.get("d1").name, "Desk")
        self.assertIs(self.hub._sessions["d1"], FakeSession.created)
        self.assertEqual(FakeSession.created.sent[0]["type"], "message")
        self.assertEqual(FakeSession.created.sent[0]["message_id"], "m1")

    def test_discovery_name_survives_missing_device_info_name(self):
        result = DiscoveryResult(
            host="127.0.0.1",
            device="dotii",
            device_id="d1",
            name="Desk From Discovery",
            port=8765,
            capabilities=["status", "text", "results", "read_ack"],
        )
        FakeSession.info_name = None
        self.manager.sync([result])
        self.assertTrue(FakeSession.online.wait(1.0))
        self.assertEqual(self.registry.get("d1").name, "Desk From Discovery")

    def test_failed_session_is_removed_and_can_retry(self):
        result = DiscoveryResult(
            host="127.0.0.1",
            device="dotii",
            device_id="d1",
            name="Desk",
            port=8765,
            capabilities=["status", "text", "results", "read_ack"],
        )
        FakeSession.fail_before_online = True
        self.manager.sync([result])

        deadline = time.monotonic() + 1.0
        while "d1" in self.manager._sessions and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertNotIn("d1", self.manager._sessions)

        FakeSession.fail_before_online = False
        self.manager.sync([result])
        self.assertTrue(FakeSession.online.wait(1.0))

    def test_remove_stops_session_and_clears_hub(self):
        result = DiscoveryResult(
            host="127.0.0.1",
            device="dotii",
            device_id="d1",
            name="Desk",
            port=8765,
            capabilities=["status", "text", "results", "read_ack"],
        )
        self.manager.sync([result])
        self.assertTrue(FakeSession.online.wait(1.0))

        self.manager.remove("d1")

        self.assertTrue(FakeSession.created.stopped.is_set())
        self.assertNotIn("d1", self.manager._sessions)
        self.assertNotIn("d1", self.hub._sessions)
