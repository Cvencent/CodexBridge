import unittest

from codexbridge.codex.quota import (
    MAX_ACTION_BYTES,
    MAX_PLAN_BYTES,
    MAX_THREAD_BYTES,
    quota_from_snapshot,
)


def _byte_length(text):
    return len(text.encode("utf-8"))


class QuotaProjectionTest(unittest.TestCase):
    def test_only_present_and_valid_fields_are_emitted(self):
        snapshot = {
            "account": {
                "five_hour_available": True,
                "five_hour_remaining_percent": 64,
                "weekly_available": True,
                "weekly_remaining_percent": 71,
                "weekly_tokens_available": True,
                "weekly_tokens": 12345,
                "five_hour_reset_at_epoch": 1770000000,
                "plan_type": "plus",
                "bad": "ignored",
            },
            "task": {
                "status": "working",
                "thread_id": "t1",
                "current_action": "Running tests",
                "token_count": 42,
                "plan_completed": 2,
                "plan_total": 5,
            },
        }
        quota = quota_from_snapshot(snapshot)
        self.assertEqual(quota["five_hour_remaining_percent"], 64)
        self.assertEqual(quota["weekly_remaining_percent"], 71)
        self.assertEqual(quota["weekly_tokens"], 12345)
        self.assertEqual(quota["five_hour_reset_at"], 1770000000)
        self.assertEqual(quota["plan_type"], "plus")
        self.assertEqual(quota["task_status"], "working")
        self.assertEqual(quota["thread_id"], "t1")
        self.assertEqual(quota["current_action"], "Running tests")
        self.assertEqual(quota["token_count"], 42)
        self.assertEqual(quota["plan_completed"], 2)
        self.assertEqual(quota["plan_total"], 5)
        self.assertEqual(quota["seven_day_tokens"], 12345)
        self.assertNotIn("weekly_reset_at", quota)

    def test_bad_types_are_ignored_and_missing_fields_absent(self):
        quota = quota_from_snapshot(
            {
                "account": {
                    "five_hour_remaining_percent": "64",
                    "weekly_tokens": True,
                    "plan_type": 7,
                },
                "task": {"token_count": None},
            }
        )
        self.assertEqual(quota, {})

    def test_non_dict_snapshot_is_empty(self):
        self.assertEqual(quota_from_snapshot(None), {})

    def test_long_strings_are_clipped_to_keep_the_line_in_budget(self):
        quota = quota_from_snapshot(
            {
                "account": {"plan_type": "P" * 200},
                "task": {
                    "status": "working",
                    "thread_id": "T" * 200,
                    "current_action": "工" * 200,
                },
            }
        )
        self.assertLessEqual(_byte_length(quota["plan_type"]), MAX_PLAN_BYTES)
        self.assertLessEqual(_byte_length(quota["thread_id"]), MAX_THREAD_BYTES)
        self.assertLessEqual(_byte_length(quota["current_action"]), MAX_ACTION_BYTES)
        # Multibyte clipping must stay on a valid UTF-8 boundary.
        self.assertEqual(quota["current_action"].encode("utf-8").decode("utf-8"),
                         quota["current_action"])


if __name__ == "__main__":
    unittest.main()
