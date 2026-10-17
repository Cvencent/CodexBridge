import unittest

from codexbridge.cbdp.auth import hmac_auth_proof, verify_hmac_auth
from codexbridge.cbdp.capabilities import (
    PROFILE_BUDGETS,
    capability_bitmap,
    capability_names,
    effective_capabilities,
)
from codexbridge.cbdp.tlv import CBDPError


class CBDPCapabilityAuthTests(unittest.TestCase):
    def test_capability_bitmap_assignments_and_unknown_bits(self):
        bitmap = capability_bitmap(["status", "text", "tasks", "approval", "control", "history"])
        self.assertEqual(bitmap, 0x00000317)
        self.assertEqual(capability_names(bitmap), {"status", "text", "tasks", "approval", "control", "history"})
        self.assertEqual(capability_names(bitmap | (1 << 63)), capability_names(bitmap | (1 << 63)))

    def test_effective_is_three_way_intersection(self):
        supported = capability_bitmap(["status", "text", "tasks"])
        enabled = capability_bitmap(["text", "tasks", "queue"])
        transport = capability_bitmap(["tasks", "queue"])
        self.assertEqual(effective_capabilities(supported, enabled, transport), {"tasks"})

    def test_profile_budgets_are_stable(self):
        self.assertEqual(PROFILE_BUDGETS[0].max_frame_bytes, 256)
        self.assertEqual(PROFILE_BUDGETS[1].max_text_chars, 512)
        self.assertEqual(PROFILE_BUDGETS[2].max_chunk_bytes, 16384)

    def test_hmac_auth_matches_protocol_vector(self):
        proof = hmac_auth_proof(
            "0123456789abcdef0123456789abcdef",
            bytes.fromhex("0102030405060708090A0B0C0D0E0F10"),
            bytes(16),
            bytes.fromhex("1112131415161718191A1B1C1D1E1F20"),
            "esp32-lab",
        )
        self.assertEqual(
            proof.hex().upper(),
            "7DF2DEC2FAE61A43D69100EA217F3006"
            "7E05937BF187BCE02AAF355F7DC74A92",
        )
        self.assertTrue(
            verify_hmac_auth(
                proof,
                "0123456789abcdef0123456789abcdef",
                bytes.fromhex("0102030405060708090A0B0C0D0E0F10"),
                bytes(16),
                bytes.fromhex("1112131415161718191A1B1C1D1E1F20"),
                "esp32-lab",
            )
        )
        self.assertFalse(
            verify_hmac_auth(
                b"\x00" * 32,
                "0123456789abcdef0123456789abcdef",
                bytes.fromhex("0102030405060708090A0B0C0D0E0F10"),
                bytes(16),
                bytes.fromhex("1112131415161718191A1B1C1D1E1F20"),
                "esp32-lab",
            )
        )


if __name__ == "__main__":
    unittest.main()
