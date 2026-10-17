from __future__ import annotations

import secrets
import struct
import time
import zlib
from dataclasses import dataclass, field

from codexbridge.cbdp.constants import (
    FLAG_CRC32_PRESENT,
    FLAG_MORE_FRAGMENTS,
    HEADER_SIZE,
    MAGIC,
    TAG_FRAGMENT_INDEX,
    TAG_FRAGMENT_STREAM_ID,
    TAG_FRAGMENT_TOTAL,
    TAG_ORIGINAL_FLAGS,
    TAG_ORIGINAL_HEADER_TLV,
    TAG_ORIGINAL_PAYLOAD_LEN,
    TAG_ORIGINAL_TYPE,
    TAG_PAYLOAD_CRC32,
    TYPE_FRAGMENT,
    UNSUPPORTED_FLAGS,
    VERSION_MAJOR,
)
from codexbridge.cbdp.tlv import CBDPError, decode_tlvs, encode_tlvs, u8, u16, u32

HEADER = struct.Struct(">2sBBHHIIIIHBB")
assert HEADER.size == HEADER_SIZE

# A single frame always fits in this many bytes; anything larger is split into
# FRAGMENT frames whose payloads each stay under the receiver's limit.
MIN_FRAGMENT_PAYLOAD = 64


@dataclass
class Frame:
    message_type: int
    flags: int = 0
    topic_id: int = 0
    seq: int = 0
    ack_seq: int = 0
    code: int = 0
    qos: int = 2
    header: dict[int, bytes] = field(default_factory=dict)
    payload: bytes = b""
    version_major: int = VERSION_MAJOR
    reserved: int = 0


def encode_frame(frame: Frame) -> bytes:
    if not isinstance(frame, Frame):
        raise TypeError("frame must be a Frame")
    if frame.version_major != VERSION_MAJOR:
        raise CBDPError("PROTOCOL_VERSION")
    if frame.message_type not in range(1, 0x1E):
        raise CBDPError("UNSUPPORTED_FEATURE")
    if frame.flags & UNSUPPORTED_FLAGS:
        raise CBDPError("UNSUPPORTED_FEATURE")
    header_tlv = encode_tlvs(frame.header.items())
    header_len = HEADER_SIZE + len(header_tlv)
    if header_len > 0xFFFF:
        raise CBDPError("PAYLOAD_TOO_LARGE")
    payload = bytes(frame.payload)
    if len(payload) > 0xFFFFFFFF:
        raise CBDPError("PAYLOAD_TOO_LARGE")
    fixed = HEADER.pack(
        MAGIC,
        frame.version_major,
        int(frame.message_type),
        int(frame.flags),
        header_len,
        len(payload),
        int(frame.topic_id),
        int(frame.seq),
        int(frame.ack_seq),
        int(frame.code),
        int(frame.qos),
        int(frame.reserved),
    )
    return fixed + header_tlv + payload


def decode_frame(data: bytes, *, max_frame_bytes: int = 65536) -> Frame:
    if not isinstance(data, (bytes, bytearray)):
        raise CBDPError("INVALID_FRAME")
    if max_frame_bytes <= 0 or len(data) > max_frame_bytes:
        raise CBDPError("PAYLOAD_TOO_LARGE")
    if len(data) < HEADER_SIZE:
        raise CBDPError("INVALID_FRAME")
    (
        magic,
        version_major,
        message_type,
        flags,
        header_len,
        payload_len,
        topic_id,
        seq,
        ack_seq,
        code,
        qos,
        reserved,
    ) = HEADER.unpack_from(data, 0)
    if magic != MAGIC:
        raise CBDPError("INVALID_FRAME")
    if version_major != VERSION_MAJOR:
        raise CBDPError("PROTOCOL_VERSION")
    if header_len < HEADER_SIZE or header_len > len(data):
        raise CBDPError("INVALID_FRAME")
    if header_len + payload_len != len(data):
        raise CBDPError("INVALID_FRAME")
    if flags & UNSUPPORTED_FLAGS:
        raise CBDPError("UNSUPPORTED_FEATURE")
    if qos not in {0, 1, 2}:
        raise CBDPError("INVALID_FRAME")
    header = decode_tlvs(data[HEADER_SIZE:header_len])
    payload = bytes(data[header_len:])
    if flags & FLAG_CRC32_PRESENT:
        raw_crc = header.get(TAG_PAYLOAD_CRC32)
        if raw_crc is None or len(raw_crc) != 4:
            raise CBDPError("INVALID_FRAME")
        if struct.unpack(">I", raw_crc)[0] != (zlib.crc32(payload) & 0xFFFFFFFF):
            raise CBDPError("FRAGMENT_ERROR")
    return Frame(
        message_type=message_type,
        flags=flags,
        topic_id=topic_id,
        seq=seq,
        ack_seq=ack_seq,
        code=code,
        qos=qos,
        header=header,
        payload=payload,
        version_major=version_major,
        reserved=reserved,
    )


def encode_fragments(frame: Frame, max_frame_bytes: int) -> list[bytes]:
    """Encode one logical frame as transport-sized wire chunks.

    Protocol 14.1 requires a logical message that overflows ``max_frame_bytes``
    to be split rather than dropped or handed to TCP as one oversized frame.
    The returned list is the exact byte sequence to write, in order; a frame
    that already fits comes back as a single encoded frame.
    """

    encoded = encode_frame(frame)
    if max_frame_bytes <= 0:
        raise CBDPError("PAYLOAD_TOO_LARGE")
    if len(encoded) <= max_frame_bytes:
        return [encoded]
    if not frame.payload:
        # Header-only frames cannot be trimmed further: the fixed header plus
        # the declared TLVs simply do not fit the negotiated budget.
        raise CBDPError("PAYLOAD_TOO_LARGE")

    header_tlv = encode_tlvs(frame.header.items())
    # Measure the real per-fragment overhead from the first-fragment header
    # instead of guessing TLV sizes; the first fragment carries the original
    # header TLV and is therefore the largest.
    probe_header = {
        TAG_FRAGMENT_STREAM_ID: u32(0),
        TAG_FRAGMENT_INDEX: u16(0),
        TAG_FRAGMENT_TOTAL: u16(1),
        TAG_ORIGINAL_TYPE: u8(0),
        TAG_ORIGINAL_FLAGS: u16(0),
        TAG_ORIGINAL_PAYLOAD_LEN: u32(0),
        TAG_ORIGINAL_HEADER_TLV: header_tlv,
    }
    base_overhead = HEADER_SIZE + len(encode_tlvs(probe_header.items()))
    chunk_bytes = max_frame_bytes - base_overhead
    if chunk_bytes < MIN_FRAGMENT_PAYLOAD:
        raise CBDPError("PAYLOAD_TOO_LARGE")
    payload = bytes(frame.payload)
    total = (len(payload) + chunk_bytes - 1) // chunk_bytes
    if total > 512:
        raise CBDPError("PAYLOAD_TOO_LARGE")
    stream_id = int.from_bytes(secrets.token_bytes(4), "big")
    fragments: list[bytes] = []
    for index in range(total):
        chunk = payload[index * chunk_bytes : (index + 1) * chunk_bytes]
        header = {
            TAG_FRAGMENT_STREAM_ID: u32(stream_id),
            TAG_FRAGMENT_INDEX: u16(index),
            TAG_FRAGMENT_TOTAL: u16(total),
            TAG_ORIGINAL_TYPE: u8(int(frame.message_type)),
            TAG_ORIGINAL_FLAGS: u16(int(frame.flags)),
            TAG_ORIGINAL_PAYLOAD_LEN: u32(len(payload)),
        }
        if index == 0:
            header[TAG_ORIGINAL_HEADER_TLV] = header_tlv
        flags = FLAG_MORE_FRAGMENTS if index < total - 1 else 0
        fragment = Frame(
            message_type=TYPE_FRAGMENT,
            flags=flags,
            topic_id=int(frame.topic_id),
            seq=int(frame.seq),
            ack_seq=int(frame.ack_seq),
            code=int(frame.code),
            qos=int(frame.qos),
            header=header,
            payload=chunk,
        )
        encoded_fragment = encode_frame(fragment)
        if len(encoded_fragment) > max_frame_bytes:
            raise CBDPError("PAYLOAD_TOO_LARGE")
        fragments.append(encoded_fragment)
    return fragments


class FrameReassembler:
    """Reassemble ``FRAGMENT`` frames back into the original logical frame.

    Only a transport helper: it keeps one in-progress stream per id, accepts
    out-of-order arrival, times out stale streams, and never lets a reassembled
    message exceed the caller's byte budget.
    """

    def __init__(
        self,
        max_frame_bytes: int,
        *,
        max_message_bytes: int = 1048576,
        timeout_seconds: float = 30.0,
    ):
        self.max_frame_bytes = int(max_frame_bytes)
        # A reassembled logical message is allowed to exceed one transport
        # frame; this is its own ceiling, not the per-frame budget.
        self.max_message_bytes = int(max_message_bytes)
        self.timeout_seconds = float(timeout_seconds)
        self._streams: dict[int, dict] = {}

    def reset(self) -> None:
        self._streams.clear()

    def accept(self, frame: Frame) -> Frame | None:
        """Feed one frame; return the original frame once it is complete."""

        if frame.message_type != TYPE_FRAGMENT:
            return frame
        try:
            stream_id = struct.unpack(
                ">I", frame.header[TAG_FRAGMENT_STREAM_ID]
            )[0]
            index = struct.unpack(">H", frame.header[TAG_FRAGMENT_INDEX])[0]
            total = struct.unpack(">H", frame.header[TAG_FRAGMENT_TOTAL])[0]
        except (KeyError, struct.error) as error:
            raise CBDPError("FRAGMENT_ERROR") from error
        if total < 1 or total > 512 or index >= total:
            raise CBDPError("FRAGMENT_ERROR")
        original_type = frame.header.get(TAG_ORIGINAL_TYPE)
        original_flags = frame.header.get(TAG_ORIGINAL_FLAGS)
        original_len = frame.header.get(TAG_ORIGINAL_PAYLOAD_LEN)
        if original_type is None or original_flags is None or original_len is None:
            raise CBDPError("FRAGMENT_ERROR")
        if index == 0 and TAG_ORIGINAL_HEADER_TLV not in frame.header:
            raise CBDPError("FRAGMENT_ERROR")
        if index != 0 and TAG_ORIGINAL_HEADER_TLV in frame.header:
            raise CBDPError("FRAGMENT_ERROR")

        now = time.monotonic()
        stream = self._streams.get(stream_id)
        if stream is None or now - stream["updated"] > self.timeout_seconds:
            stream = {
                "total": total,
                "parts": {},
                "bytes": 0,
                "header_tlv": None,
                "original_type": original_type,
                "original_flags": original_flags,
                "total_budget": int.from_bytes(original_len, "big"),
                "updated": now,
            }
            self._streams[stream_id] = stream
        if (
            stream["total"] != total
            or stream["original_type"] != original_type
            or stream["total_budget"] != int.from_bytes(original_len, "big")
        ):
            self._streams.pop(stream_id, None)
            raise CBDPError("FRAGMENT_ERROR")
        if index == 0:
            stream["header_tlv"] = frame.header[TAG_ORIGINAL_HEADER_TLV]
        stream["updated"] = now
        if index not in stream["parts"]:
            stream["parts"][index] = bytes(frame.payload)
            stream["bytes"] += len(frame.payload)
        if stream["bytes"] > self.max_message_bytes:
            self._streams.pop(stream_id, None)
            raise CBDPError("PAYLOAD_TOO_LARGE")
        if len(stream["parts"]) < total:
            return None
        if stream["header_tlv"] is None:
            # Every fragment arrived but none was index 0, so the original
            # header is missing; the stream cannot be reconstructed.
            self._streams.pop(stream_id, None)
            raise CBDPError("FRAGMENT_ERROR")
        self._streams.pop(stream_id, None)
        payload = b"".join(stream["parts"][key] for key in sorted(stream["parts"]))
        if len(payload) != stream["total_budget"]:
            raise CBDPError("FRAGMENT_ERROR")
        return Frame(
            message_type=int.from_bytes(original_type, "big"),
            flags=int.from_bytes(original_flags, "big"),
            topic_id=frame.topic_id,
            seq=frame.seq,
            ack_seq=frame.ack_seq,
            code=frame.code,
            qos=frame.qos,
            header=decode_tlvs(stream["header_tlv"]),
            payload=payload,
        )
