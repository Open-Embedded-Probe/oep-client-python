"""TLV helpers: tag(u8) len(u8) value. Bit 7 of the tag marks a critical element."""

from __future__ import annotations

import struct

from . import codec


def encode(items) -> bytes:
    out = bytearray()
    for tag, value in items:
        if len(value) > 255:
            raise ValueError("TLV value exceeds 255 bytes")
        out += bytes((tag, len(value))) + bytes(value)
    return bytes(out)


def decode(data: bytes) -> list[tuple[int, bytes]]:
    items, offset = [], 0
    while offset + 2 <= len(data):
        tag, length = data[offset], data[offset + 1]
        value = data[offset + 2:offset + 2 + length]
        if len(value) != length:
            raise ValueError("truncated TLV")
        items.append((tag, bytes(value)))
        offset += 2 + length
    if offset != len(data):
        raise ValueError("trailing TLV bytes")
    return items


def role_assignment(function: int, role: int, channel: int) -> tuple[int, bytes]:
    return codec.TLV_CORE_ROLE_ASSIGNMENT, struct.pack("<HBH", function, role, channel)


def channel_candidates(tlv: bytes) -> list[int]:
    return [struct.unpack("<H", v)[0] for t, v in decode(tlv) if t == codec.TLV_CORE_CHANNEL_CANDIDATE]
