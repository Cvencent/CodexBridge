import unittest

from codexbridge.cbdp.account import (
    account_rate_limits_payload,
    account_token_usage_payload,
)
from codexbridge.cbdp.constants import (
    EVENT_TOKEN_USAGE_CHANGED,
    EVENT_RATE_LIMITS_CHANGED,
    TAG_RATE_LIMITS,
    TAG_RATE_LIMIT_WINDOW,
    TAG_REMAINING_PERCENT,
    TAG_RESET_AT_MS,
    TAG_SEVEN_DAY_AS_OF,
    TAG_SEVEN_DAY_TOKENS,
    TAG_SEVEN_DAY_WINDOW_DAYS,
)
from codexbridge.cbdp.events import SCOPE_SUMMARY, event_scope
from codexbridge.cbdp.tlv import decode_array, decode_tlvs, decode_u8, decode_u64


def _rate_windows(payload):
    windows = {}
    for _tag, element in decode_array(payload[TAG_RATE_LIMITS]):
        fields = decode_tlvs(element)
        name = fields[TAG_RATE_LIMIT_WINDOW].decode("utf-8")
        windows[name] = fields
    return windows


class AccountEventNumberingTests(unittest.TestCase):
    def test_event_numbers_match_the_v1_spec(self):
        self.assertEqual(EVENT_RATE_LIMITS_CHANGED, 0x0E02)
        self.assertEqual(EVENT_TOKEN_USAGE_CHANGED, 0x0E03)

    def test_tlv_numbers_match_the_v1_spec(self):
        self.assertEqual(TAG_RATE_LIMITS, 0x0183)
        self.assertEqual(TAG_RATE_LIMIT_WINDOW, 0x0184)
        self.assertEqual(TAG_REMAINING_PERCENT, 0x0185)
        self.assertEqual(TAG_RESET_AT_MS, 0x0186)
        self.assertEqual(TAG_SEVEN_DAY_TOKENS, 0x018D)
        self.assertEqual(TAG_SEVEN_DAY_AS_OF, 0x018E)
        self.assertEqual(TAG_SEVEN_DAY_WINDOW_DAYS, 0x018F)

    def test_account_events_are_summary_scope(self):
        self.assertEqual(event_scope(EVENT_RATE_LIMITS_CHANGED), SCOPE_SUMMARY)
        self.assertEqual(event_scope(EVENT_TOKEN_USAGE_CHANGED), SCOPE_SUMMARY)


class RateLimitPayloadTests(unittest.TestCase):
    def test_both_windows_carry_remaining_percent_and_reset_ms(self):
        payload = account_rate_limits_payload(
            {
                "five_hour_remaining_percent": 64,
                "five_hour_reset_at": 1770000000,
                "weekly_remaining_percent": 71,
                "weekly_reset_at": 1771000000,
            }
        )

        windows = _rate_windows(payload)

        self.assertEqual(set(windows), {"five_hour", "weekly"})
        self.assertEqual(decode_u8(windows["five_hour"][TAG_REMAINING_PERCENT]), 64)
        self.assertEqual(
            decode_u64(windows["five_hour"][TAG_RESET_AT_MS]), 1770000000000
        )
        self.assertEqual(decode_u8(windows["weekly"][TAG_REMAINING_PERCENT]), 71)
        self.assertEqual(
            decode_u64(windows["weekly"][TAG_RESET_AT_MS]), 1771000000000
        )

    def test_missing_window_is_absent_not_zero(self):
        payload = account_rate_limits_payload({"weekly_remaining_percent": 71})

        self.assertEqual(set(_rate_windows(payload)), {"weekly"})

    def test_values_are_clamped_to_percent_range(self):
        payload = account_rate_limits_payload(
            {"five_hour_remaining_percent": 250, "weekly_remaining_percent": -5}
        )

        values = sorted(
            decode_u8(fields[TAG_REMAINING_PERCENT])
            for fields in _rate_windows(payload).values()
        )

        self.assertEqual(values, [0, 100])

    def test_bad_types_are_ignored(self):
        payload = account_rate_limits_payload(
            {"five_hour_remaining_percent": "64", "weekly_remaining_percent": True}
        )

        self.assertEqual(payload, {})

    def test_reset_without_remaining_is_still_useful(self):
        payload = account_rate_limits_payload({"weekly_reset_at": 1771000000})

        windows = _rate_windows(payload)

        self.assertIn("weekly", windows)
        self.assertNotIn(TAG_REMAINING_PERCENT, windows["weekly"])


class TokenUsagePayloadTests(unittest.TestCase):
    def test_seven_day_total_is_emitted(self):
        payload = account_token_usage_payload(
            {"seven_day_tokens": 43210, "seven_day_as_of": "2026-10-23"}
        )

        self.assertEqual(decode_u64(payload[TAG_SEVEN_DAY_TOKENS]), 43210)
        self.assertEqual(payload[TAG_SEVEN_DAY_AS_OF].decode("utf-8"), "2026-10-23")
        self.assertEqual(decode_u8(payload[TAG_SEVEN_DAY_WINDOW_DAYS]), 7)

    def test_negative_or_bad_total_is_dropped(self):
        self.assertEqual(account_token_usage_payload({"seven_day_tokens": -1}), {})
        self.assertEqual(account_token_usage_payload({"seven_day_tokens": "x"}), {})


if __name__ == "__main__":
    unittest.main()
