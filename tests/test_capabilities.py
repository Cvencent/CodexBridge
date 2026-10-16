import unittest

from codexbridge.protocol.capabilities import MINIMAL, default_for, effective, normalize


class CapabilityTest(unittest.TestCase):
    def test_unknown_capability_is_dropped(self):
        self.assertEqual(normalize(["status", "nope"]), {"status"})

    def test_effective_is_intersection(self):
        self.assertEqual(effective(["status", "text"], ["text", "read_ack"]), {"text"})

    def test_unknown_model_gets_minimal(self):
        self.assertEqual(default_for("unknown-model"), set(MINIMAL))

    def test_ai_passport_default_covers_its_screen_and_mic(self):
        # The passport records voice and renders task lists/results, so those
        # must be on by default; an ESP32 still only claims MINIMAL extras.
        self.assertEqual(
            default_for("FoloOS-AI-Passport"),
            set(MINIMAL) | {"tasks", "results", "audio", "voice", "approval"},
        )

    def test_quota_is_known_and_dotii_default(self):
        self.assertEqual(normalize(["quota", "nope"]), {"quota"})
        self.assertIn("quota", default_for("dotii"))
