from __future__ import annotations

import struct
from collections.abc import Iterable, Mapping

from codexbridge.cbdp.constants import ERROR_CODES


class CBDPError(ValueError):
    def __init__(self, name: str):
        super().__init__(name)
        self.name = name
        self.code = ERROR_CODES.get(name, ERROR_CODES["INVALID_FRAME"])


def u8(value: int) -> bytes:
    return struct.pack(">B", int(value))


def u16(value: int) -> bytes:
    return struct.pack(">H", int(value))


def u32(value: int) -> bytes:
    return struct.pack(">I", int(value))


def u64(value: int) -> bytes:
    return struct.pack(">Q", int(value))


def i64(value: int) -> bytes:
    return struct.pack(">q", int(value))


def utf8(value: str) -> bytes:
    return str(value).encode("utf-8")


def bool_value(value: bool) -> bytes:
    return b"\x01" if value else b"\x00"


def encode_tlvs(fields: Iterable[tuple[int, bytes]]) -> bytes:
    encoded: list[bytes] = []
    seen: set[int] = set()
    for tag, value in fields:
        if not isinstance(tag, int) or isinstance(tag, bool) or not 0 <= tag <= 0xFFFF:
            raise CBDPError("INVALID_FRAME")
        if tag in seen:
            raise CBDPError("INVALID_FRAME")
        if not isinstance(value, (bytes, bytearray)):
            raise CBDPError("INVALID_FRAME")
        if len(value) > 0xFFFF:
            raise CBDPError("INVALID_FRAME")
        seen.add(tag)
        encoded.append(struct.pack(">HH", tag, len(value)) + bytes(value))
    encoded.sort(key=lambda item: struct.unpack(">H", item[:2])[0])
    return b"".join(encoded)


def encode_array(element_tag: int, elements: Iterable[bytes]) -> bytes:
    """Encode an ``array`` value: u16 count followed by wrapped elements.

    Protocol 8.12 requires every array element to be a complete
    ``element_tag + u16 length + value`` TLV. Callers pass only the element
    value; the wrapper is applied here.
    """

    if not isinstance(element_tag, int) or isinstance(element_tag, bool):
        raise CBDPError("INVALID_FRAME")
    if not 0 <= element_tag <= 0xFFFF:
        raise CBDPError("INVALID_FRAME")
    encoded: list[bytes] = []
    for value in elements:
        if not isinstance(value, (bytes, bytearray)):
            raise CBDPError("INVALID_FRAME")
        if len(value) > 0xFFFF:
            raise CBDPError("INVALID_FRAME")
        encoded.append(struct.pack(">HH", element_tag, len(value)) + bytes(value))
    if len(encoded) > 0xFFFF:
        raise CBDPError("INVALID_FRAME")
    return struct.pack(">H", len(encoded)) + b"".join(encoded)


def decode_tlvs(
    data: bytes,
    *,
    max_tlvs: int = 256,
    max_depth: int = 8,
    depth: int = 0,
) -> dict[int, bytes]:
    if not isinstance(data, (bytes, bytearray)):
        raise CBDPError("INVALID_FRAME")
    if depth > max_depth:
        raise CBDPError("INVALID_FRAME")
    result: dict[int, bytes] = {}
    offset = 0
    count = 0
    limit = len(data)
    while offset < limit:
        if limit - offset < 4:
            raise CBDPError("INVALID_FRAME")
        tag, length = struct.unpack_from(">HH", data, offset)
        offset += 4
        end = offset + length
        if end > limit:
            raise CBDPError("INVALID_FRAME")
        if tag in result:
            raise CBDPError("INVALID_FRAME")
        count += 1
        if count > max_tlvs:
            raise CBDPError("INVALID_FRAME")
        result[tag] = bytes(data[offset:end])
        offset = end
    return result


def decode_array(
    data: bytes,
    expected_tag: int | None = None,
    *,
    max_elements: int = 4096,
) -> list[tuple[int, bytes]]:
    """Decode an ``array`` value: u16 count followed by wrapped elements."""

    if not isinstance(data, (bytes, bytearray)) or len(data) < 2:
        raise CBDPError("INVALID_FRAME")
    count = struct.unpack_from(">H", data, 0)[0]
    if count > max_elements:
        raise CBDPError("INVALID_FRAME")
    offset = 2
    limit = len(data)
    elements: list[tuple[int, bytes]] = []
    for _ in range(count):
        if limit - offset < 4:
            raise CBDPError("INVALID_FRAME")
        tag, length = struct.unpack_from(">HH", data, offset)
        offset += 4
        end = offset + length
        if end > limit:
            raise CBDPError("INVALID_FRAME")
        if expected_tag is not None and tag != expected_tag:
            raise CBDPError("INVALID_FRAME")
        elements.append((tag, bytes(data[offset:end])))
        offset = end
    if offset != limit:
        raise CBDPError("INVALID_FRAME")
    return elements


def require_tlv(fields: Mapping[int, bytes], tag: int) -> bytes:
    value = fields.get(tag)
    if value is None:
        raise CBDPError("INVALID_PARAM")
    return value


def decode_u8(value: bytes) -> int:
    if len(value) != 1:
        raise CBDPError("INVALID_PARAM")
    return value[0]


def decode_u16(value: bytes) -> int:
    if len(value) != 2:
        raise CBDPError("INVALID_PARAM")
    return struct.unpack(">H", value)[0]


def decode_u32(value: bytes) -> int:
    if len(value) != 4:
        raise CBDPError("INVALID_PARAM")
    return struct.unpack(">I", value)[0]


def decode_u64(value: bytes) -> int:
    if len(value) != 8:
        raise CBDPError("INVALID_PARAM")
    return struct.unpack(">Q", value)[0]


def decode_i64(value: bytes) -> int:
    if len(value) != 8:
        raise CBDPError("INVALID_PARAM")
    return struct.unpack(">q", value)[0]


def decode_bool(value: bytes) -> bool:
    if len(value) != 1:
        raise CBDPError("INVALID_PARAM")
    return value != b"\x00"


def decode_utf8(value: bytes) -> str:
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError as error:
        raise CBDPError("INVALID_PARAM") from error
