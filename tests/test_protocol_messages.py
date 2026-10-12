import unittest

from codexbridge.protocol.messages import decode, encode, split_text, wire_messages


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
