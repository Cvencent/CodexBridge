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

    def test_devices_list_hides_private_tokens(self):
        self.registry.mint_token("d1")
        self.registry.upsert_phone(
            "phone-1",
            name="Pixel",
            token_hash="a" * 64,
            supported=["status", "queue"],
            enabled=["status", "queue"],
        )
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
        rendered = json.dumps(payload)
        self.assertNotIn('"token":', rendered)
        self.assertNotIn('"token_hash":', rendered)
        self.assertNotIn("a" * 64, rendered)
        by_id = {item["device_id"]: item for item in payload["devices"]}
        self.assertEqual(by_id["d1"]["kind"], "hardware")
        self.assertEqual(by_id["phone-1"]["kind"], "phone")
        self.assertTrue(by_id["phone-1"]["has_token"])

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

    def test_capabilities_update_sets_text_budget(self):
        body = json.dumps(
            {"device_id": "d1", "enabled": ["status"], "max_text_chars": 200}
        ).encode()
        status, payload = handle_request(
            "POST",
            "/api/admin/devices/capabilities",
            {},
            body,
            "127.0.0.1",
            self.hub,
            self.registry,
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["max_text_chars"], 200)
        self.assertEqual(self.registry.get("d1").max_text_chars, 200)

    def test_text_budget_null_restores_unlimited(self):
        self.registry.get("d1").max_text_chars = 200
        body = json.dumps(
            {"device_id": "d1", "enabled": ["status"], "max_text_chars": None}
        ).encode()
        status, payload = handle_request(
            "POST",
            "/api/admin/devices/capabilities",
            {},
            body,
            "127.0.0.1",
            self.hub,
            self.registry,
        )
        self.assertEqual(status, 200)
        self.assertIsNone(payload["max_text_chars"])

    def test_omitted_text_budget_keeps_device_declaration(self):
        self.registry.get("d1").max_text_chars = 512
        body = json.dumps({"device_id": "d1", "enabled": ["status"]}).encode()
        handle_request(
            "POST",
            "/api/admin/devices/capabilities",
            {},
            body,
            "127.0.0.1",
            self.hub,
            self.registry,
        )
        self.assertEqual(self.registry.get("d1").max_text_chars, 512)

    def test_device_registration_actions_call_manager(self):
        calls = []

        def registration_action(action, device_id):
            calls.append((action, device_id))
            return True

        for action in ("approve", "reject", "revoke"):
            body = json.dumps({"device_id": "pending-1"}).encode()
            status, payload = handle_request(
                "POST",
                f"/api/admin/devices/{action}",
                {},
                body,
                "127.0.0.1",
                self.hub,
                self.registry,
                device_registration_action=registration_action,
            )
            self.assertEqual(status, 200)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["device_id"], "pending-1")

        self.assertEqual(
            calls,
            [
                ("approve", "pending-1"),
                ("reject", "pending-1"),
                ("revoke", "pending-1"),
            ],
        )

    def test_device_registration_action_reports_typed_failure(self):
        def registration_action(action, device_id):
            raise ValueError("registration_expired")

        body = json.dumps({"device_id": "pending-1"}).encode()
        status, payload = handle_request(
            "POST",
            "/api/admin/devices/approve",
            {},
            body,
            "127.0.0.1",
            self.hub,
            self.registry,
            device_registration_action=registration_action,
        )
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "registration_expired")

    def test_device_registration_action_requires_device_id(self):
        body = json.dumps({}).encode()
        status, payload = handle_request(
            "POST",
            "/api/admin/devices/approve",
            {},
            body,
            "127.0.0.1",
            self.hub,
            self.registry,
            device_registration_action=lambda action, device_id: True,
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "device_id_required")

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

    def test_general_settings_default_to_disabled(self):
        status, payload = handle_request(
            "GET",
            "/api/admin/settings/general",
            {},
            b"",
            "127.0.0.1",
            self.hub,
            self.registry,
            startup_enabled=lambda: False,
        )
        self.assertEqual(status, 200)
        self.assertFalse(payload["general"]["startup_enabled"])

    def test_general_settings_can_enable_startup(self):
        state = {"enabled": False}

        def set_enabled(value):
            state["enabled"] = bool(value)

        body = json.dumps({"startup_enabled": True}).encode()
        status, payload = handle_request(
            "POST",
            "/api/admin/settings/general",
            {},
            body,
            "127.0.0.1",
            self.hub,
            self.registry,
            startup_enabled=lambda: state["enabled"],
            set_startup_enabled=set_enabled,
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["general"]["startup_enabled"])
        self.assertTrue(state["enabled"])

    def test_general_settings_reject_non_boolean_startup(self):
        body = json.dumps({"startup_enabled": "yes"}).encode()
        status, payload = handle_request(
            "POST",
            "/api/admin/settings/general",
            {},
            body,
            "127.0.0.1",
            self.hub,
            self.registry,
        )
        self.assertEqual(status, 400)
        self.assertFalse(payload["ok"])

    def test_general_settings_support_opening_logs(self):
        opened = []
        body = json.dumps({"action": "open_logs"}).encode()
        status, payload = handle_request(
            "POST",
            "/api/admin/settings/general",
            {},
            body,
            "127.0.0.1",
            self.hub,
            self.registry,
            open_logs=lambda: opened.append(True),
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(opened, [True])

    def test_companion_port_rejection_reports_the_specific_error(self):
        def reject_port(_payload):
            raise ValueError("port_reserved")

        body = json.dumps({"action": "set_port", "port": 8790}).encode()
        status, payload = handle_request(
            "POST",
            "/api/admin/companion",
            {},
            body,
            "127.0.0.1",
            self.hub,
            self.registry,
            companion_action=reject_port,
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "port_reserved")

    def test_companion_busy_port_reports_conflict(self):
        def busy_port(_payload):
            raise OSError("busy")

        body = json.dumps({"action": "set_port", "port": 9000}).encode()
        status, payload = handle_request(
            "POST",
            "/api/admin/companion",
            {},
            body,
            "127.0.0.1",
            self.hub,
            self.registry,
            companion_action=busy_port,
        )
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "port_in_use")

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
