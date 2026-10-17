import struct
import unittest


from codexbridge.cbdp.constants import (
    FLAG_REQUEST,
    TYPE_HELLO,
    TYPE_PING,
)
from codexbridge.cbdp.frame import Frame, encode_frame
from codexbridge.devices.session import detect_wire_protocol


class WireProtocolDetectionTests(unittest.TestCase):
    def test_legacy_json_line_is_detected(self):
        self.assertEqual(
            detect_wire_protocol(b'{"type":"bridge_auth","token":"x"}\n'),
            "legacy_json",
        )

    def test_cbdp_fixed_header_is_detected(self):
        raw = encode_frame(Frame(message_type=TYPE_PING, flags=0))
        self.assertEqual(detect_wire_protocol(raw), "cbdp")

    def test_incomplete_prefix_is_unknown(self):
        self.assertEqual(detect_wire_protocol(b"{"), "legacy_json")
        self.assertEqual(detect_wire_protocol(b"\xCB"), "unknown")
        self.assertEqual(detect_wire_protocol(b"x"), "unknown")


if __name__ == "__main__":
    unittest.main()