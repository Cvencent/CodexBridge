"""Regression tests for the app-server stdout drain."""

from __future__ import annotations

import io
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from codexbridge.codex import app_server as app_server_module
from codexbridge.codex.app_server import AppServerClient


class StdoutLimitTests(unittest.TestCase):
    def test_oversized_line_is_dropped_without_json_parsing(self) -> None:
        client = AppServerClient(["codex"], Path("."))
        oversized = "x" * (client.MAX_STDOUT_LINE_CHARS + 128) + "\n"
        small = json.dumps({"method": "test/notification", "params": {"ok": True}}) + "\n"
        client.process = SimpleNamespace(stdout=io.StringIO(oversized + small))

        with mock.patch.object(
            app_server_module.json, "loads", wraps=json.loads
        ) as loads:
            client._read_stdout()

        self.assertEqual(1, loads.call_count)
        self.assertEqual("test/notification", client.notifications.get_nowait()["method"])
        self.assertGreater(client.oversized_stdout_lines, 0)


if __name__ == "__main__":
    unittest.main()
