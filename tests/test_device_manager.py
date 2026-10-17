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


# The assertions cover behaviour, not latency: a loaded CI runner can starve
# the freshly started session thread for far longer than a second.
ONLINE_TIMEOUT_SECONDS = 10.0


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
        self.assertTrue(FakeSession.online.wait(ONLINE_TIMEOUT_SECONDS))
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
        # The attach-time replay runs on the hub worker, so wait for it to land
        # instead of assuming it beat the discovery thread.
        deadline = time.time() + 2.0
        while not FakeSession.created.sent and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual(FakeSession.created.sent[0]["type"], "message")
        self.assertEqual(FakeSession.created.sent[0]["message_id"], "m1")

    def test_pending_device_does_not_open_a_session(self):
        result = DiscoveryResult(
            host="127.0.0.1",
            device="dotii",
            device_id="pending-1",
            name="New Desk",
            port=8765,
            capabilities=["status", "text"],
            registration_state="pending",
            registration_nonce="r_0123456789abcdef",
        )
        self.manager.sync([result])

        deadline = time.monotonic() + 0.3
        while time.monotonic() < deadline and "pending-1" in self.manager._sessions:
            time.sleep(0.01)
        self.assertNotIn("pending-1", self.manager._sessions)
        record = self.registry.get("pending-1")
        self.assertEqual(record.registration_state, "pending")
        self.assertFalse(record.has_token)
        self.assertEqual(record.last_address, "127.0.0.1")

    def test_approve_sends_offer_before_opening_a_session(self):
        result = DiscoveryResult(
            host="127.0.0.1",
            device="dotii",
            device_id="pending-1",
            name="New Desk",
            port=8765,
            capabilities=["status", "text"],
            registration_state="pending",
            registration_nonce="r_0123456789abcdef",
        )
        self.manager.sync([result])
        calls = []

        def register_offer(host, port, token, **kwargs):
            calls.append((host, port, token, kwargs))
            return True

        self.manager._send_register_offer = register_offer
        self.assertTrue(self.manager.approve("pending-1"))

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "127.0.0.1")
        self.assertEqual(calls[0][1], 8765)
        self.assertEqual(calls[0][3]["device_id"], "pending-1")
        self.assertEqual(
            calls[0][3]["registration_nonce"],
            "r_0123456789abcdef",
        )
        record = self.registry.get("pending-1")
        self.assertEqual(record.registration_state, "approved")
        self.assertTrue(record.has_token)

    def test_approve_rejects_expired_or_rebound_nonce(self):
        self.registry.upsert(
            "pending-1",
            model="dotii",
            supported=["status"],
            host="127.0.0.1",
            port=8765,
        )
        record = self.registry.get("pending-1")
        record.registration_state = "pending"
        record.registration_nonce = "r_0123456789abcdef"
        record.registration_address = "127.0.0.1"
        record.registration_discovered_at = time.time() - 601.0
        calls = []
        self.manager._send_register_offer = lambda *args, **kwargs: calls.append(kwargs) or True

        with self.assertRaisesRegex(ValueError, "registration_expired"):
            self.manager.approve("pending-1")
        self.assertEqual(calls, [])

        record.registration_discovered_at = time.time()
        record.registration_address = "10.0.0.9"
        with self.assertRaisesRegex(ValueError, "registration_address_changed"):
            self.manager.approve("pending-1")
        self.assertEqual(calls, [])

    def test_reject_and_revoke_clear_pending_credentials(self):
        result = DiscoveryResult(
            host="127.0.0.1",
            device="dotii",
            device_id="pending-1",
            name="New Desk",
            port=8765,
            capabilities=["status", "text"],
            registration_state="pending",
            registration_nonce="r_0123456789abcdef",
        )
        self.manager.sync([result])

        self.assertTrue(self.manager.reject("pending-1"))
        self.assertEqual(
            self.registry.get("pending-1").registration_state,
            "rejected",
        )
        self.manager.sync([result])
        self.assertEqual(
            self.registry.get("pending-1").registration_state,
            "rejected",
        )
        self.assertTrue(self.manager.revoke("pending-1"))
        self.assertEqual(
            self.registry.get("pending-1").registration_state,
            "revoked",
        )
        self.assertFalse(self.registry.get("pending-1").has_token)

    def test_heartbeat_arrives_before_device_session_timeout(self):
        self.assertLess(
            self.manager.heartbeat_seconds,
            self.manager.timeout_seconds,
            "the host must beat before the device closes the session",
        )

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
        self.assertTrue(FakeSession.online.wait(ONLINE_TIMEOUT_SECONDS))
        self.assertEqual(self.registry.get("d1").name, "Desk From Discovery")

    def test_legacy_record_migrates_token_to_discovered_device_id(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        legacy = Path(tmp.name) / "bridge.json"
        legacy.write_text(
            '{"host":"127.0.0.1","port":8765,"token":"legacy-token-abcdef"}',
            encoding="utf-8",
        )
        self.registry.migrate_legacy(legacy)
        result = DiscoveryResult(
            host="127.0.0.1",
            device="FoloOS-AI-Passport",
            device_id="passport-device",
            name="Passport",
            port=8765,
            capabilities=["status", "text", "read_ack"],
        )
        self.manager.sync([result])
        self.assertTrue(FakeSession.online.wait(ONLINE_TIMEOUT_SECONDS))
        record = self.registry.get("passport-device")
        self.assertEqual(record.token, "legacy-token-abcdef")
        self.assertIsNone(self.registry.get("legacy:127.0.0.1"))

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

        deadline = time.monotonic() + ONLINE_TIMEOUT_SECONDS
        while "d1" in self.manager._sessions and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertNotIn("d1", self.manager._sessions)

        FakeSession.fail_before_online = False
        self.manager.sync([result])
        self.assertTrue(FakeSession.online.wait(ONLINE_TIMEOUT_SECONDS))

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
        self.assertTrue(FakeSession.online.wait(ONLINE_TIMEOUT_SECONDS))

        self.manager.remove("d1")

        self.assertTrue(FakeSession.created.stopped.is_set())
        self.assertNotIn("d1", self.manager._sessions)
        self.assertNotIn("d1", self.hub._sessions)
