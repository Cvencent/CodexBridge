import unittest

from codexbridge.protocol.messages import (
    MAX_LINE_BYTES,
    decode,
    encode,
    split_text,
    wire_messages,
)


class MessageWireTest(unittest.TestCase):
    def test_round_trip(self):
        self.assertEqual(decode(encode({"type": "bridge_ready"})), {"type": "bridge_ready"})

    def test_non_json_line_is_not_a_message(self):
        self.assertIsNone(decode(b"[device] boot ok"))

    def test_encode_rejects_non_dict(self):
        with self.assertRaises(TypeError):
            encode(["not", "a", "dict"])

    def test_split_text_keeps_characters_intact(self):
        chunks = split_text("中文" * 400, limit=480)
        self.assertTrue(all(len(c.encode("utf-8")) <= 480 for c in chunks))
        self.assertEqual("".join(chunks), "中文" * 400)

    def test_long_message_is_chunked_with_sequence(self):
        records = wire_messages({"type": "message", "body": "中" * 600})
        self.assertGreater(len(records), 1)
        self.assertEqual(decode(records[0])["total"], len(records))
        self.assertEqual(decode(records[-1])["seq"], len(records) - 1)

    def test_long_message_metadata_respects_line_limit(self):
        body = "中" * 600
        records = wire_messages(
            {
                "type": "message",
                "message_id": "m1",
                "thread_id": "t" * 200,
                "kind": "result",
                "status": "completed",
                "title": "题" * 500,
                "body": body,
                "created_at": 0.0,
            }
        )
        decoded = [decode(record) for record in records]

        self.assertTrue(all(len(record) <= MAX_LINE_BYTES for record in records))
        self.assertEqual("".join(item["body"] for item in decoded), body)
        self.assertLessEqual(len(decoded[0]["title"].encode("utf-8")), 160)
        self.assertTrue(decoded[0]["title"].endswith("..."))


class ClipUtf8Test(unittest.TestCase):
    def test_character_budget_applies_first(self):
        from codexbridge.protocol.messages import clip_utf8

        self.assertEqual(clip_utf8("一二三四五六", max_chars=4), "一...")

    def test_byte_budget_never_splits_a_character(self):
        from codexbridge.protocol.messages import clip_utf8

        out = clip_utf8("一二三四五六七八", max_bytes=10)
        # 7 usable bytes after the suffix fit two 3-byte characters, never a
        # partial third one.
        self.assertLessEqual(len(out.encode("utf-8")), 10)
        self.assertEqual(out, "一二" + "...")

    def test_both_budgets_together_take_the_tighter(self):
        from codexbridge.protocol.messages import clip_utf8

        # A three-character budget is exactly the ellipsis, and the byte budget
        # is satisfied by it, so the character rule wins and nothing is lost.
        self.assertEqual(clip_utf8("abcdefgh", max_chars=3, max_bytes=3), "...")
        # A character budget wider than the bytes available is cut down again.
        self.assertEqual(clip_utf8("一二三四五", max_chars=5, max_bytes=9), "一二" + "...")

    def test_short_text_passes_through(self):
        from codexbridge.protocol.messages import clip_utf8

        self.assertEqual(clip_utf8("ok", max_bytes=240), "ok")
        self.assertEqual(clip_utf8("ok"), "ok")

    def test_non_strings_become_empty(self):
        from codexbridge.protocol.messages import clip_utf8

        self.assertEqual(clip_utf8(None, max_bytes=8), "")
        self.assertEqual(clip_utf8(5, max_bytes=8), "")

    def test_tiny_byte_budget_does_not_crash(self):
        from codexbridge.protocol.messages import clip_utf8

        self.assertEqual(clip_utf8("abcdef", max_bytes=2), "")
