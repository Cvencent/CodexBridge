"""Provider failure messages must reach the phone in readable form."""

from __future__ import annotations

import json
import unittest

from codexbridge.codex.app_server import _flatten_provider_error, _turn_error, thread_snapshot


class FlattenProviderErrorTests(unittest.TestCase):
    def test_unwraps_nested_json_and_keeps_the_provider_prefix(self) -> None:
        inner = "[workbuddy] upstream client (http 400): " + json.dumps(
            {"code": 11129, "msg": "invalid function call parameters", "requestId": "abc"}
        )
        payload = json.dumps({"error": {"message": inner}})

        self.assertEqual(
            _flatten_provider_error(payload),
            "[workbuddy] upstream client (http 400): invalid function call parameters",
        )

    def test_plain_text_passes_through(self) -> None:
        self.assertEqual(_flatten_provider_error("network unreachable"), "network unreachable")

    def test_blank_input_is_empty(self) -> None:
        self.assertEqual(_flatten_provider_error("   "), "")

    def test_turn_error_handles_missing_error(self) -> None:
        self.assertEqual(_turn_error({"status": "completed"}), "")
        self.assertEqual(_turn_error({"error": None}), "")

    def test_turn_error_reads_string_errors(self) -> None:
        self.assertEqual(_turn_error({"error": "boom"}), "boom")


class ThreadSnapshotErrorTests(unittest.TestCase):
    def _thread(self, error):
        turn = {
            "id": "turn-1",
            "status": "failed",
            "startedAt": 1791000000,
            "completedAt": 1791000010,
            "items": [],
        }
        if error is not None:
            turn["error"] = error
        return {"id": "t1", "name": "任务", "turns": [turn]}

    def test_failed_turn_exposes_error_message(self) -> None:
        payload = json.dumps({"error": {"message": "upstream said no"}})
        snapshot = thread_snapshot(self._thread({"message": payload}))

        self.assertEqual(snapshot["status"], "failed")
        self.assertEqual(snapshot["error_message"], "upstream said no")

    def test_completed_turn_has_empty_error_message(self) -> None:
        snapshot = thread_snapshot(self._thread(None))
        self.assertEqual(snapshot["error_message"], "")

    def test_empty_thread_has_error_message_field(self) -> None:
        self.assertIn("error_message", thread_snapshot(None))


if __name__ == "__main__":
    unittest.main()
