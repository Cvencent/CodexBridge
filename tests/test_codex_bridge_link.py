import json
import sys
import unittest
from pathlib import Path
from unittest import mock

BRIDGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BRIDGE))

from codexbridge.management.codex_pairing import claim_codex_bridge_token


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self, _size=-1):
        return json.dumps(self.payload).encode("utf-8")


class CodexBridgeLinkTests(unittest.TestCase):
    def test_claim_uses_loopback_admin_api_and_hides_token_from_repr(self):
        with mock.patch("codexbridge.management.codex_pairing.urllib.request.urlopen") as urlopen:
            urlopen.return_value = FakeResponse(
                {
                    "ok": True,
                    "device_id": "dotii-001122334455",
                    "token": "0123456789abcdef0123456789abcdef",
                    "port": 8765,
                }
            )
            result = claim_codex_bridge_token("dotii-001122334455")

        self.assertEqual(result.port, 8765)
        self.assertNotIn("0123456789abcdef0123456789abcdef", repr(result))
        self.assertEqual(
            urlopen.call_args.args[0].full_url,
            "http://127.0.0.1:8788/api/admin/pairing?device_id=dotii-001122334455",
        )

    def test_claim_rejects_missing_device_id(self):
        with self.assertRaisesRegex(ValueError, "device_id"):
            claim_codex_bridge_token("dotii-")

    def test_claim_rejects_short_token(self):
        with mock.patch("codexbridge.management.codex_pairing.urllib.request.urlopen") as urlopen:
            urlopen.return_value = FakeResponse(
                {
                    "ok": True,
                    "device_id": "dotii-001122334455",
                    "token": "short",
                    "port": 8765,
                }
            )
            with self.assertRaisesRegex(ValueError, "令牌"):
                claim_codex_bridge_token("dotii-001122334455")


if __name__ == "__main__":
    unittest.main()
