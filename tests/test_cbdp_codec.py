import struct
import unittest

from codexbridge.cbdp.constants import (
    FLAG_COMPRESSED,
    FLAG_CRC32_PRESENT,
    QOS_TELEMETRY,
    TAG_PAYLOAD_CRC32,
    TYPE_PING,
    TYPE_PAIRING,
)
from codexbridge.cbdp.frame import (
    Frame,
    FrameReassembler,
    decode_frame,
    encode_frame,
    encode_fragments,
)
from codexbridge.cbdp.tlv import CBDPError, decode_tlvs, encode_tlvs, u32


class CBDPCodecTests(unittest.TestCase):
    def test_ping_matches_protocol_vector(self):
        raw = encode_frame(Frame(message_type=TYPE_PING, qos=QOS_TELEMETRY))
        self.assertEqual(
            raw.hex(" ").upper(),
            "CB 01 01 05 00 00 00 1C 00 00 00 00 "
            "00 00 00 00 00 00 00 00 00 00 00 00 "
            "00 00 02 00",
        )
        self.assertEqual(decode_frame(raw).message_type, TYPE_PING)

    def test_unknown_tlv_is_returned_but_does_not_break_known_tags(self):
        raw = encode_tlvs([(0x0097, u32(7)), (0x7FFF, b"future")])
        self.assertEqual(decode_tlvs(raw)[0x7FFF], b"future")

    def test_pairing_message_type_is_encodable(self):
        raw = encode_frame(Frame(message_type=TYPE_PAIRING))
        self.assertEqual(decode_frame(raw).message_type, TYPE_PAIRING)

    def test_duplicate_tlv_is_rejected(self):
        raw = struct.pack(">HH", 0x0097, 4) + u32(1) + struct.pack(">HH", 0x0097, 4) + u32(2)
        with self.assertRaises(CBDPError):
            decode_tlvs(raw)

    def test_truncated_or_trailing_tlv_is_rejected(self):
        with self.assertRaises(CBDPError):
            decode_tlvs(b"\x00\x97\x00\x04\x00")
        with self.assertRaises(CBDPError):
            decode_tlvs(b"\x00")

    def test_payload_and_header_bounds_are_checked(self):
        raw = encode_frame(Frame(message_type=TYPE_PING, header={0x7FFF: b"x"}, payload=b"abc"))
        decoded = decode_frame(raw)
        self.assertEqual(decoded.payload, b"abc")
        with self.assertRaises(CBDPError):
            decode_frame(raw[:-1])
        with self.assertRaises(CBDPError):
            decode_frame(raw + b"x")

    def test_wrong_version_and_unsupported_flags_are_reported(self):
        raw = bytearray(encode_frame(Frame(message_type=TYPE_PING)))
        raw[2] = 2
        with self.assertRaises(CBDPError) as error:
            decode_frame(bytes(raw))
        self.assertEqual(error.exception.name, "PROTOCOL_VERSION")
        with self.assertRaises(CBDPError) as error:
            decode_frame(encode_frame(Frame(message_type=TYPE_PING, flags=FLAG_COMPRESSED)))
        self.assertEqual(error.exception.name, "UNSUPPORTED_FEATURE")

    def test_crc_is_verified(self):
        payload = b"hello"
        crc = struct.pack(">I", __import__("zlib").crc32(payload) & 0xFFFFFFFF)
        raw = encode_frame(
            Frame(
                message_type=TYPE_PING,
                flags=FLAG_CRC32_PRESENT,
                header={TAG_PAYLOAD_CRC32: crc},
                payload=payload,
            )
        )
        self.assertEqual(decode_frame(raw).payload, payload)
        broken = bytearray(raw)
        broken[-1] ^= 1
        with self.assertRaises(CBDPError) as error:
            decode_frame(bytes(broken))
        self.assertEqual(error.exception.name, "FRAGMENT_ERROR")

    def test_max_frame_bytes_is_enforced(self):
        raw = encode_frame(Frame(message_type=TYPE_PING, payload=b"1234"))
        with self.assertRaises(CBDPError) as error:
            decode_frame(raw, max_frame_bytes=len(raw) - 1)
        self.assertEqual(error.exception.name, "PAYLOAD_TOO_LARGE")

    def test_oversized_frame_is_fragmented_not_dropped(self):
        payload = bytes(range(256)) * 8
        logical = Frame(
            message_type=0x0A,
            flags=0x0400,
            topic_id=0x10000001,
            header={0x0037: b"\x03\x01"},
            payload=payload,
        )
        fragments = encode_fragments(logical, 256)
        self.assertGreater(len(fragments), 1)
        self.assertTrue(all(len(item) <= 256 for item in fragments))
        for index, raw in enumerate(fragments):
            decoded = decode_frame(raw, max_frame_bytes=256)
            self.assertEqual(decoded.message_type, 0x1B)
            if index < len(fragments) - 1:
                self.assertTrue(decoded.flags & 0x0001)

    def test_fragments_reassemble_out_of_order(self):
        payload = bytes(range(256)) * 8
        logical = Frame(
            message_type=0x0A,
            flags=0x0400,
            topic_id=0x10000001,
            header={0x0037: b"\x03\x01"},
            payload=payload,
        )
        fragments = [
            decode_frame(raw, max_frame_bytes=256)
            for raw in encode_fragments(logical, 256)
        ]
        reassembler = FrameReassembler(256)
        result = None
        for frame in reversed(fragments):
            candidate = reassembler.accept(frame)
            if candidate is not None:
                result = candidate
        self.assertIsNotNone(result)
        self.assertEqual(result.message_type, 0x0A)
        self.assertEqual(result.payload, payload)
        self.assertEqual(result.header, {0x0037: b"\x03\x01"})

    def test_frame_that_fits_is_not_fragmented(self):
        logical = Frame(message_type=0x0A, payload=b"small")
        self.assertEqual(len(encode_fragments(logical, 65536)), 1)
        with self.assertRaises(CBDPError) as error:
            encode_fragments(
                Frame(message_type=0x0A, header={0x7FFF: b"x" * 4096}), 256
            )
        self.assertEqual(error.exception.name, "PAYLOAD_TOO_LARGE")


if __name__ == "__main__":
    unittest.main()
