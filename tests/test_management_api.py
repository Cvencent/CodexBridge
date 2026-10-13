import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from codexbridge.management import server as management_server
from codexbridge.management.codex_pairing import PairingToken


class ManagementApiTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.environment = mock.patch.dict(
            "os.environ",
            {
                "LOCALAPPDATA": str(self.root),
                "STATE_DISPLAY_BRIDGE_TOKEN": "management-token-1234",
            },
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.server = management_server.build_server(
            host="127.0.0.1",
            port=0,
            state_path=None,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop_server)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def _stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def _json(self, path, *, method="GET", body=None):
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            self.base + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            return json.loads(response.read().decode("utf-8"))

    def test_snapshot_excludes_codex_and_keeps_device_sections(self):
        snapshot = self.server.snapshot()
        self.assertNotIn("codex", snapshot)
        self.assertIn("custom", snapshot)
        self.assertIn("bambu", snapshot)
        self.assertIn("modules", snapshot)
        self.assertIn("dotii", snapshot)
        self.assertIn("display", snapshot)

    def test_overview_excludes_codex_tab_and_collector_fields(self):
        overview = self._json("/api/v1/admin/overview")
        self.assertNotIn("codex_check", overview)
        self.assertNotIn("collector_state", overview["bridge"])
        self.assertEqual(
            [item["id"] for item in overview["modules"]],
            ["bambu", "custom", "dotii"],
        )

    def test_configure_route_claims_codexbridge_token_with_read_device_id(self):
        self.server.bluetooth.device_status = {"device_id": "dotii-001122334455"}
        with mock.patch.object(
            management_server,
            "claim_codex_bridge_token",
            return_value=PairingToken(
                "dotii-001122334455",
                "codex-token-1234567890",
                8765,
            ),
        ) as claim:
            with mock.patch.object(
                self.server.bluetooth,
                "start_configure",
                return_value=True,
            ) as configure:
                payload = {
                    "address": "AA:BB:CC:DD:EE:FF",
                    "ssid": "Studio",
                    "password": "secret",
                }
                result = self._json(
                    "/api/v1/admin/bluetooth/configure",
                    method="POST",
                    body=payload,
                )
        self.assertTrue(result["ok"])
        claim.assert_called_once_with("dotii-001122334455")
        kwargs = configure.call_args.kwargs
        self.assertEqual(kwargs["codex_bridge_token"], "codex-token-1234567890")
        self.assertEqual(kwargs["bridge_token"], self.server.token)

    def test_configure_route_rejects_missing_device_id(self):
        self.server.bluetooth.device_status = {}
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self._json(
                "/api/v1/admin/bluetooth/configure",
                method="POST",
                body={"address": "AA:BB:CC:DD:EE:FF", "ssid": "Studio"},
            )
        self.assertEqual(raised.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
