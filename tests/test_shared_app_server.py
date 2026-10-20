import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from codexbridge import VERSION
from codexbridge.codex.app_server import AppServerClient, CodexAppServerSource


class SharedServerTests(unittest.TestCase):
    def test_identical_read_requests_are_coalesced(self):
        with tempfile.TemporaryDirectory() as folder:
            client = AppServerClient(["codex"], Path(folder))
            calls = []

            def send(message):
                calls.append(message)
                client._pending[message["id"]].put({"result": {"sequence": 1}})

            with patch.object(client, "_send", side_effect=send):
                first = client.request("thread/read", {"threadId": "t1"})
                second = client.request("thread/read", {"threadId": "t1"})

        self.assertEqual(first, {"sequence": 1})
        self.assertEqual(second, {"sequence": 1})
        self.assertEqual(len(calls), 1)

    def test_injected_shared_client_is_not_closed_by_source(self):
        with tempfile.TemporaryDirectory() as folder:
            client = Mock()
            with patch(
                "codexbridge.codex.app_server._resolve_codex_command",
                side_effect=AssertionError("injected client must not resolve Codex CLI"),
            ):
                source = CodexAppServerSource(
                    Path(folder),
                    Path(folder),
                    client=client,
                    owns_client=False,
                )
            source.close()
            client.close.assert_not_called()

    def test_app_server_identity_matches_desktop_for_hydration(self):
        with tempfile.TemporaryDirectory() as folder:
            client = AppServerClient(["codex"], Path(folder))
            with patch.object(client, "_desktop_socket", return_value=None), \
                    patch("subprocess.Popen", return_value=Mock()), \
                    patch("threading.Thread"), \
                    patch.object(client, "request") as request, \
                    patch.object(client, "notify"):
                client.start()
            init = request.call_args.args[1]["clientInfo"]
            self.assertEqual(init["name"], "Codex Desktop")
            self.assertEqual(init["title"], "CodexBridge")
            self.assertEqual(init["version"], VERSION)
            params = request.call_args.args[1]
            self.assertTrue(params["capabilities"]["experimentalApi"])

    def test_stale_socket_file_is_not_treated_as_a_live_desktop(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "app-server-control").mkdir()
            (root / "app-server-control" / "app-server-control.sock").touch()
            with patch.dict("os.environ", {"CODEX_HOME": folder}):
                self.assertIsNone(AppServerClient._desktop_socket())

    def test_shared_client_uses_desktop_proxy_when_socket_accepts_connections(self):
        with tempfile.TemporaryDirectory() as folder:
            client = AppServerClient(["codex"], Path(folder), prefer_shared=True)
            with patch.object(client, "_desktop_socket", return_value=Path(folder) / "control.sock"), \
                    patch("subprocess.Popen", return_value=Mock()) as spawn, \
                    patch("threading.Thread"), patch.object(client, "request"), \
                    patch.object(client, "notify"):
                client.start()
            self.assertEqual(spawn.call_args.args[0], [
                "codex", "app-server", "proxy", "--sock", str(Path(folder) / "control.sock"),
            ])

    def test_missing_shared_server_keeps_legacy_transport(self):
        with tempfile.TemporaryDirectory() as folder:
            client = AppServerClient(["codex"], Path(folder), prefer_shared=True)
            with patch.object(client, "_desktop_socket", return_value=None), \
                    patch("subprocess.Popen", return_value=Mock()) as spawn, \
                    patch("threading.Thread"), patch.object(client, "request"), \
                    patch.object(client, "notify"):
                client.start()
            self.assertEqual(spawn.call_args.args[0], ["codex", "app-server", "--listen", "stdio://"])

    def test_app_server_runs_below_normal_priority_on_windows(self):
        with tempfile.TemporaryDirectory() as folder:
            client = AppServerClient(["codex"], Path(folder))
            with patch.object(client, "_desktop_socket", return_value=None), \
                    patch("subprocess.Popen", return_value=Mock()) as spawn, \
                    patch("threading.Thread"), patch.object(client, "request"), \
                    patch.object(client, "notify"):
                client.start()
            flags = spawn.call_args.kwargs["creationflags"]
            self.assertTrue(flags & subprocess.CREATE_NO_WINDOW)
            self.assertTrue(flags & subprocess.BELOW_NORMAL_PRIORITY_CLASS)
    def test_connection_status_explains_when_desktop_realtime_is_unavailable(self):
        with tempfile.TemporaryDirectory() as folder:
            client = AppServerClient(["codex"], Path(folder), prefer_shared=True)
            with patch.object(client, "_desktop_socket", return_value=None), \
                    patch("subprocess.Popen", return_value=Mock()), \
                    patch("threading.Thread"), patch.object(client, "request"), \
                    patch.object(client, "notify"):
                client.start()
            self.assertEqual(client.connection_status(), {
                "desktop_realtime_shared": False,
                "desktop_sync_mode": "polling",
                "transport": "stdio",
                "reason": "shared_endpoint_unavailable",
            })
