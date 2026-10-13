import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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
            "/api/admin/status",
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
            "/api/admin/status",
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
            "/api/admin/pairing",
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
            "/api/admin/devices",
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
            "/api/admin/devices/capabilities",
            {},
            body,
            "127.0.0.1",
            self.hub,
            self.registry,
        )
        self.assertEqual(status, 200)
        self.assertEqual(self.registry.get("d1").enabled, {"status"})

    def test_remove_notifies_device_manager(self):
        removed = []
        body = json.dumps({"device_id": "d1"}).encode()
        status, _ = handle_request(
            "POST",
            "/api/admin/devices/remove",
            {},
            body,
            "127.0.0.1",
            self.hub,
            self.registry,
            on_device_removed=removed.append,
        )
        self.assertEqual(status, 200)
        self.assertEqual(removed, ["d1"])

    def test_old_versioned_path_is_not_served(self):
        status, _ = handle_request(
            "GET",
            "/api/v1/admin/status",
            {},
            b"",
            "127.0.0.1",
            self.hub,
            self.registry,
        )
        self.assertEqual(status, 404)

    def test_codex_status_reports_collector_state(self):
        status = {
            "enabled": True,
            "collector_state": "online",
            "collector_detail": "数据采集正常",
            "last_snapshot_epoch": 123,
        }
        code, payload = handle_request(
            "GET",
            "/api/admin/codex-status",
            {},
            b"",
            "127.0.0.1",
            self.hub,
            self.registry,
            codex_status=lambda: status,
        )
        self.assertEqual(code, 200)
        self.assertEqual(payload["codex"], status)
        self.assertTrue(payload["ok"])

    def test_settings_page_is_served(self):
        status, payload = handle_request(
            "GET",
            "/settings",
            {},
            b"",
            "127.0.0.1",
            self.hub,
            self.registry,
        )
        self.assertEqual(status, 200)
        self.assertIn("语音识别", payload)

    def test_speech_settings_hides_api_key(self):
        from codexbridge.speech_source import save_speech_config

        path = self.registry.path.parent / "speech-source.json"
        save_speech_config(
            {
                "source": "openai",
                "endpoint": "https://example.test/v1/audio/transcriptions",
                "api_key": "secret-token",
            },
            path,
        )
        status, payload = handle_request(
            "GET",
            "/api/admin/settings/speech",
            {},
            b"",
            "127.0.0.1",
            self.hub,
            self.registry,
            speech_config_path=path,
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["speech"]["api_key_set"])
        self.assertNotIn("secret-token", json.dumps(payload))

    def test_speech_connection_test_does_not_save_unsaved_config(self):
        from codexbridge.speech_source import load_speech_config

        path = self.registry.path.parent / "speech-source.json"
        body = json.dumps(
            {
                "source": "openai",
                "endpoint": "http://127.0.0.1:9/v1/audio/transcriptions",
                "api_key": "probe-only-key",
            }
        ).encode()
        with patch(
            "codexbridge.admin_api.probe_speech_config",
            return_value=(True, "连接成功"),
        ):
            status, payload = handle_request(
                "POST",
                "/api/admin/settings/speech/test",
                {},
                body,
                "127.0.0.1",
                self.hub,
                self.registry,
                speech_config_path=path,
            )
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(load_speech_config(path)["source"], "default")
