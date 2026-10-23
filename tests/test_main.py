import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codexbridge.admin_api import handle_request
from codexbridge.codex.app_server import AppServerError, _resolve_codex_command
from codexbridge.companion.store import CompanionStore
from codexbridge.devices.discovery import DiscoveryResult
from codexbridge.devices.manager import DeviceManager
from codexbridge.devices.registry import DeviceRegistry
from codexbridge.hub import BridgeHub
from codexbridge.__main__ import (
    _CodexSourceHolder,
    _CodexState,
    _apply_companion_action,
    _launch_restarted_process,
    _restart_command,
)


class MainRestartTest(unittest.TestCase):
    def test_launch_restart_uses_detached_new_process(self):
        calls = []
        _launch_restarted_process(
            Path("D:/data"),
            popen=lambda command, **kwargs: calls.append((command, kwargs)),
        )
        self.assertEqual(len(calls), 1)
        command, kwargs = calls[0]
        self.assertEqual(command, _restart_command())
        self.assertTrue(kwargs["close_fds"])

    def test_source_restart_command_runs_codexbridge_module(self):
        with patch("codexbridge.__main__.sys.frozen", False, create=True), patch(
            "codexbridge.__main__.sys.executable",
            sys.executable,
        ):
            command = _restart_command()
        self.assertEqual(command[0], sys.executable)
        self.assertEqual(command[1:3], ["-m", "codexbridge"])

    def test_frozen_restart_command_reuses_executable(self):
        with patch("codexbridge.__main__.sys.frozen", True, create=True), patch(
            "codexbridge.__main__.sys.executable",
            r"C:\Apps\CodexBridge\CodexBridge.exe",
        ):
            command = _restart_command()
        self.assertEqual(command, [r"C:\Apps\CodexBridge\CodexBridge.exe"])


class CodexCommandResolutionTest(unittest.TestCase):
    def test_finds_official_cli_when_path_was_not_inherited(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            executable = root / "OpenAI" / "Codex" / "bin" / "version-2" / "codex.exe"
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"")

            with patch.dict(
                "os.environ",
                {"LOCALAPPDATA": str(root), "APPDATA": str(root / "npm")},
                clear=False,
            ), patch(
                "codexbridge.codex.app_server.shutil.which",
                return_value=None,
            ):
                command = _resolve_codex_command(None, root)

        self.assertEqual(command, [str(executable)])

    def test_explicit_command_wins_over_install_discovery(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            explicit = root / "custom-codex.exe"
            explicit.write_bytes(b"")

            with patch.dict(
                "os.environ",
                {"LOCALAPPDATA": str(root / "missing"), "APPDATA": str(root / "npm")},
                clear=False,
            ), patch(
                "codexbridge.codex.app_server.shutil.which",
                return_value=None,
            ):
                command = _resolve_codex_command(str(explicit), root)

        self.assertEqual(command, [str(explicit)])


class CodexStatePersistenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "codex-collection.json"

    def test_defaults_to_enabled_without_a_state_file(self):
        state = _CodexState(self.path)
        self.assertTrue(state.snapshot()["enabled"])

    def test_off_survives_a_restart(self):
        # Turning collection off has to stick: coming back on at the next
        # launch would put the stutter straight back.
        _CodexState(self.path).set_enabled(False)
        self.assertFalse(_CodexState(self.path).snapshot()["enabled"])

    def test_on_survives_a_restart(self):
        _CodexState(self.path).set_enabled(True)
        self.assertTrue(_CodexState(self.path).snapshot()["enabled"])

    def test_a_corrupt_state_file_falls_back_to_enabled(self):
        self.path.write_text("{not json", encoding="utf-8")
        self.assertTrue(_CodexState(self.path).snapshot()["enabled"])

    def test_switching_off_reports_a_paused_collector(self):
        state = _CodexState(self.path)
        state.set_enabled(False)
        snapshot = state.snapshot()
        self.assertEqual(snapshot["collector_state"], "paused")
        self.assertIn("关闭", snapshot["collector_detail"])


class CodexSourceHolderTest(unittest.TestCase):
    def test_request_uses_the_active_source_client(self):
        class Client:
            def __init__(self):
                self.calls = []

            def request(self, method, params=None):
                self.calls.append((method, params))
                return {"ok": True}

        source = type("Source", (), {"client": Client()})()
        holder = _CodexSourceHolder()
        holder.set(source)

        self.assertEqual(holder.request("project/list", {"limit": 100}), {"ok": True})
        self.assertEqual(source.client.calls, [("project/list", {"limit": 100})])

        holder.set(None)
        with self.assertRaises(AppServerError):
            holder.request("project/list", {"limit": 100})

    def test_holder_uses_fallback_client_when_collector_is_not_ready(self):
        class Client:
            def __init__(self):
                self.calls = []

            def request(self, method, params=None):
                self.calls.append((method, params))
                return {"source": "fallback"}

        holder = _CodexSourceHolder()
        client = Client()
        holder._fallback = client
        holder._fallback_cwd = Path("D:/work")

        self.assertEqual(
            holder.request("project/list", {"limit": 100}),
            {"source": "fallback"},
        )
        self.assertEqual(client.calls, [("project/list", {"limit": 100})])

    def test_holder_reuses_a_shared_fallback_client(self):
        class Client:
            def __init__(self):
                self.started = 0
                self.calls = []

            def start(self):
                self.started += 1

            def request(self, method, params=None):
                self.calls.append((method, params))
                return {"source": "shared"}

            def close(self):
                pass

        holder = _CodexSourceHolder()
        client = Client()
        holder.configure(
            None,
            Path("D:/work"),
            Path("D:/data"),
            fallback_client=client,
            owns_fallback=False,
        )

        self.assertEqual(holder.request("project/list", {"limit": 100}), {"source": "shared"})
        self.assertEqual(client.started, 1)
        self.assertEqual(client.calls, [("project/list", {"limit": 100})])

    def test_holder_refuses_to_start_codex_while_collection_is_off(self):
        class Client:
            def __init__(self):
                self.started = 0

            def start(self):
                self.started += 1

            def request(self, method, params=None):
                return {"source": "fallback"}

        holder = _CodexSourceHolder(enabled=lambda: False)
        client = Client()
        holder.configure(
            None,
            Path("D:/work"),
            Path("D:/data"),
            fallback_client=client,
            owns_fallback=False,
        )

        # The switch exists to stop exactly this: a device catalog request
        # reviving the private app-server the user just turned off.
        with self.assertRaises(AppServerError):
            holder.request("project/list", {"limit": 100})
        self.assertEqual(client.started, 0)


class UnifiedDeviceAdminTest(unittest.TestCase):
    """The phone and a pending ESP must appear in one device list."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.registry = DeviceRegistry(root / "devices.json")
        self.store = CompanionStore(root / "companion.json", registry=self.registry)
        self.hub = BridgeHub(self.registry)
        self.manager = DeviceManager(self.registry, self.hub)
        self.addCleanup(self.manager.stop)
        self.actions = []

    def _pair_phone(self):
        code = self.store.pairing_code()["code"]
        return self.store.claim(code, "Pixel", client_id="install-1")

    def _discover_pending_hardware(
        self,
        device_id="dotii-a1",
        host="127.0.0.1",
        nonce="r_0123456789abcdef",
    ):
        self.manager.sync(
            [
                DiscoveryResult(
                    host=host,
                    device="dotii",
                    device_id=device_id,
                    name="Desk",
                    port=8765,
                    capabilities=["status", "text", "queue"],
                    registration_state="pending",
                    registration_nonce=nonce,
                )
            ]
        )
        return self.registry.get(device_id)

    def _registration_action(self, action, device_id):
        self.actions.append((action, device_id))
        return True

    def _list_devices(self, host="127.0.0.1"):
        status, payload = handle_request(
            "GET",
            "/api/admin/devices",
            {},
            b"",
            host,
            self.hub,
            self.registry,
        )
        self.assertEqual(status, 200, payload)
        return payload["devices"]

    def test_phone_and_pending_hardware_share_one_device_list(self):
        phone = self._pair_phone()
        self._discover_pending_hardware()

        devices = self._list_devices()
        by_id = {row["device_id"]: row for row in devices}
        self.assertEqual(set(by_id), {phone["phone_id"], "dotii-a1"})

        phone_row = by_id[phone["phone_id"]]
        self.assertEqual(phone_row["kind"], "phone")
        self.assertEqual(phone_row["transport"], "companion_http")
        self.assertEqual(phone_row["registration_state"], "approved")
        self.assertEqual(phone_row["registration_method"], "pairing_code")
        self.assertTrue(phone_row["has_token"])
        self.assertIn("queue", phone_row["supported"])
        self.assertNotIn("token_hash", phone_row)
        self.assertNotIn("token", phone_row)

        pending_row = by_id["dotii-a1"]
        self.assertEqual(pending_row["kind"], "hardware")
        self.assertEqual(pending_row["transport"], "device_tcp")
        self.assertEqual(pending_row["registration_state"], "pending")
        self.assertEqual(pending_row["registration_method"], "admin_approval")
        self.assertFalse(pending_row["has_token"])
        self.assertNotIn("registration_nonce", pending_row)

    def test_pending_hardware_is_not_online_before_approval(self):
        self._discover_pending_hardware()

        pending = self._list_devices()
        self.assertEqual(len(pending), 1)
        self.assertFalse(pending[0]["online"])
        self.assertEqual(pending[0]["registration_state"], "pending")
        self.assertNotIn("dotii-a1", self.hub._sessions)

    def test_approve_route_is_loopback_only(self):
        self._discover_pending_hardware()
        self.manager._send_register_offer = lambda *args, **kwargs: True

        status, payload = handle_request(
            "POST",
            "/api/admin/devices/approve",
            {},
            b'{"device_id": "dotii-a1"}',
            "192.168.1.77",
            self.hub,
            self.registry,
            device_registration_action=self._registration_action,
        )
        self.assertEqual(status, 403, payload)
        self.assertEqual(payload["error"], "loopback_required")
        self.assertEqual(self.actions, [])

        status, payload = handle_request(
            "POST",
            "/api/admin/devices/approve",
            {},
            b'{"device_id": "dotii-a1"}',
            "127.0.0.1",
            self.hub,
            self.registry,
            device_registration_action=self._registration_action,
        )
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["changed"])
        self.assertEqual(self.actions, [("approve", "dotii-a1")])

    def test_rejected_stays_rejected_until_nonce_or_reregistration(self):
        self._discover_pending_hardware()
        self.assertTrue(self.manager.reject("dotii-a1"))
        self.assertEqual(
            self.registry.get("dotii-a1").registration_state,
            "rejected",
        )

        # Repeating the same announcement must not silently re-open a device
        # the admin already rejected.
        self._discover_pending_hardware()
        self.assertEqual(
            self.registry.get("dotii-a1").registration_state,
            "rejected",
        )

        # A fresh nonce is a new registration attempt and returns to pending.
        self._discover_pending_hardware(nonce="r_fedcba9876543210")
        self.assertEqual(
            self.registry.get("dotii-a1").registration_state,
            "pending",
        )

    def test_revoke_then_reapprove_restores_a_token(self):
        self._discover_pending_hardware()
        self.assertTrue(self.manager.revoke("dotii-a1"))
        self.assertEqual(
            self.registry.get("dotii-a1").registration_state,
            "revoked",
        )

        # Re-registration puts the device back in the queue for approval.
        self._discover_pending_hardware()
        self.assertEqual(
            self.registry.get("dotii-a1").registration_state,
            "pending",
        )
        self.manager._send_register_offer = lambda *args, **kwargs: True
        self.assertTrue(self.manager.approve("dotii-a1"))
        record = self.registry.get("dotii-a1")
        self.assertEqual(record.registration_state, "approved")
        self.assertTrue(record.has_token)

    def test_deleted_hardware_is_rediscovered_as_pending(self):
        self._discover_pending_hardware()
        self.manager._send_register_offer = lambda *args, **kwargs: True
        self.assertTrue(self.manager.approve("dotii-a1"))
        self.assertTrue(self.registry.get("dotii-a1").has_token)

        status, payload = handle_request(
            "POST",
            "/api/admin/devices/remove",
            {},
            b'{"device_id": "dotii-a1"}',
            "127.0.0.1",
            self.hub,
            self.registry,
            on_device_removed=self.manager.remove,
        )
        self.assertEqual(status, 200, payload)
        self.assertIsNone(self.registry.get("dotii-a1"))

        self._discover_pending_hardware()
        rediscovered = self.registry.get("dotii-a1")
        self.assertEqual(rediscovered.registration_state, "pending")
        self.assertFalse(rediscovered.has_token)


class CompanionActionTest(unittest.TestCase):
    def test_show_intermediate_events_action_updates_store(self):
        calls = []
        status = {"show_intermediate_events": True}

        result = _apply_companion_action(
            {"show_intermediate_events": True},
            companion=None,
            discovery=None,
            store=None,
            status=lambda: status,
            set_show_intermediate_events=calls.append,
        )

        self.assertEqual(calls, [True])
        self.assertEqual(result, {"companion": status})

    def test_discovery_port_action_rebinds_udp_and_returns_fresh_status(self):
        class Companion:
            def __init__(self):
                self.calls = []

            def set_port(self, port):
                self.calls.append(port)

        class Discovery:
            def __init__(self):
                self.calls = []

            def set_port(self, port):
                self.calls.append(port)

        class Store:
            def __init__(self):
                self.calls = []

            def set_discovery_port(self, port):
                self.calls.append(port)

        companion = Companion()
        discovery = Discovery()
        store = Store()
        status = {"port": 8568, "discovery_port": 18791, "ports": []}

        result = _apply_companion_action(
            {"action": "set_port", "kind": "discovery", "port": 18791},
            companion=companion,
            discovery=discovery,
            store=store,
            status=lambda: status,
        )

        self.assertEqual(discovery.calls, [18791])
        self.assertEqual(companion.calls, [])
        self.assertEqual(result, {"companion": status})
