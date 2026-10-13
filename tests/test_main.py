import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from codexbridge.codex.app_server import AppServerError
from codexbridge.__main__ import (
    _CodexSourceHolder,
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
