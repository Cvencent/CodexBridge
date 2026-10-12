import json
import tempfile
import unittest
from pathlib import Path

from codexbridge import VERSION
from codexbridge.admin_api import handle_request
from codexbridge.devices.registry import DeviceRegistry
from codexbridge.hub import BridgeHub


class AdminApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.registry = DeviceRegistry(Path(self.tmp.name) / "devices.json")
        self.registry.upsert("d1", model="dotii", supported=["status", "text"])
        self.hub = BridgeHub(self.registry)

    def test_non_loopback_is_forbidden(self):
        status, _ = handle_request(
            "GET",
            "/api/v1/admin/status",
            {},
            b"",
            "10.0.0.9",
            self.hub,
            self.registry,
        )
        self.assertEqual(status, 403)

    def test_status_reports_version(self):
        status, payload = handle_request(
            "GET",
            "/api/v1/admin/status",
            {},
            b"",
            "127.0.0.1",
            self.hub,
            self.registry,
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["version"], VERSION)

    def test_pairing_mints_token_and_stores_it(self):
        status, payload = handle_request(
            "GET",
            "/api/v1/admin/pairing",
            {"device_id": ["d2"]},
            b"",
            "127.0.0.1",
            self.hub,
            self.registry,
        )
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(payload["token"]), 16)
        self.assertEqual(self.registry.get("d2").token, payload["token"])

    def test_devices_list_hides_token(self):
        status, payload = handle_request(
            "GET",
            "/api/v1/admin/devices",
            {},
            b"",
            "127.0.0.1",
            self.hub,
            self.registry,
        )
        self.assertEqual(status, 200)
        self.assertNotIn("token", json.dumps(payload))

    def test_capabilities_update_filters_unknown(self):
        body = json.dumps({"device_id": "d1", "enabled": ["status", "nope"]}).encode()
        status, _ = handle_request(
            "POST",
            "/api/v1/admin/devices/capabilities",
            {},
            body,
            "127.0.0.1",
            self.hub,
            self.registry,
        )
        self.assertEqual(status, 200)
        self.assertEqual(self.registry.get("d1").enabled, {"status"})
