import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from codexbridge.__main__ import _launch_restarted_process, _restart_command


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
