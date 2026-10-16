import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codexbridge.codex.app_server import AppServerError, _resolve_codex_command
from codexbridge.__main__ import (
    _CodexSourceHolder,
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
