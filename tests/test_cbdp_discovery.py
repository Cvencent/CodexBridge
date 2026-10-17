import socket
import unittest

from codexbridge.cbdp.capabilities import capability_bitmap
from codexbridge.cbdp.constants import (
    FLAG_REQUEST,
    FLAG_RESPONSE,
    TAG_BRIDGE_INSTANCE_ID,
    TAG_CAPABILITY_BITS,
    TAG_DEVICE_ID,
    TAG_DISCOVERY_NONCE,
    TAG_DISCOVERY_SCOPE,
    TAG_FIRMWARE,
    TAG_MODEL,
    TAG_PORT,
    TAG_PROFILE,
    TAG_SUPPORTED_CAPS,
    TAG_TRANSPORT_SECURITY,
    TYPE_DISCOVERY,
    TYPE_DISCOVERY_REPLY,
)
from codexbridge.cbdp.frame import Frame, encode_frame
from codexbridge.cbdp.tlv import u8, u16, utf8
from codexbridge.devices.discovery import (
    build_cbdp_discovery_request,
    parse_cbdp_reply,
)


class CbdpDiscoveryTests(unittest.TestCase):
    def test_request_has_nonce_and_scope(self):
        nonce = bytes(range(16))
        raw = build_cbdp_discovery_request(nonce)
        from codexbridge.cbdp.frame import decode_frame

        frame = decode_frame(raw)
        self.assertEqual(frame.message_type, TYPE_DISCOVERY)
        self.assertEqual(frame.flags & FLAG_REQUEST, FLAG_REQUEST)
        self.assertEqual(frame.header[TAG_DISCOVERY_NONCE], nonce)
        self.assertEqual(frame.header[TAG_DISCOVERY_SCOPE], u8(2))

    def test_reply_requires_matching_nonce(self):
        nonce = bytes(range(16))
        frame = Frame(
            message_type=TYPE_DISCOVERY_REPLY,
            flags=FLAG_RESPONSE,
            header={
                TAG_DEVICE_ID: utf8("dev-1"),
                TAG_MODEL: utf8("panel"),
                TAG_FIRMWARE: utf8("1.0"),
                TAG_PROFILE: u8(1),
                TAG_SUPPORTED_CAPS: capability_bitmap(["status", "text"]).to_bytes(8, "big"),
                TAG_PORT: u16(8765),
                TAG_DISCOVERY_NONCE: nonce,
                TAG_TRANSPORT_SECURITY: u8(0),
            },
        )
        result = parse_cbdp_reply(encode_frame(frame), "127.0.0.1", expected_nonce=nonce)
        self.assertIsNotNone(result)
        self.assertEqual(result.device_id, "dev-1")
        self.assertEqual(result.protocol, "cbdp")
        self.assertIn("status", result.capabilities)
        self.assertIsNone(
            parse_cbdp_reply(encode_frame(frame), "127.0.0.1", expected_nonce=b"x" * 16)
        )


if __name__ == "__main__":
    unittest.main()
