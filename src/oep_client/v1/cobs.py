"""UART binding framing (provisional): message + CRC-16 little endian, COBS-encoded, ended by 0x00.

CRC-16/CCITT-FALSE: poly 0x1021, init 0xFFFF, no reflection, no final xor ("123456789" -> 0x29B1).
Mirrors oep-probe-arduino OepFrame (CobsReader / writeCobsFrame).
"""

from __future__ import annotations


class CorruptFrame(ValueError):
    pass


def crc16(data: bytes, crc: int = 0xFFFF) -> int:
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def encode(data: bytes) -> bytes:
    """Standard COBS (no delimiter): blocks of length+1 and up to 254 non-zero bytes; a full block implies no
    zero, and when the data ends right after one an empty block follows."""
    out = bytearray()
    i, n = 0, len(data)
    while True:
        j = i
        while j < n and data[j] != 0 and j - i < 254:
            j += 1
        code = j - i + 1
        out.append(code)
        out += data[i:j]
        if code == 0xFF:
            i = j
            continue
        if j >= n:
            return bytes(out)
        i = j + 1


def decode(raw: bytes) -> bytes:
    out = bytearray()
    i, n = 0, len(raw)
    while i < n:
        code = raw[i]
        i += 1
        if code == 0 or i + code - 1 > n:
            raise CorruptFrame("COBS block runs past the frame")
        out += raw[i:i + code - 1]
        i += code - 1
        if code != 0xFF and i < n:
            out.append(0)
    return bytes(out)


def frame(message: bytes) -> bytes:
    crc = crc16(message)
    return encode(message + bytes([crc & 0xFF, crc >> 8])) + b"\x00"


def unframe(raw: bytes) -> bytes:
    """raw: the bytes between two delimiters (without the 0x00)."""
    data = decode(raw)
    if len(data) < 3:
        raise CorruptFrame("frame shorter than a message and its CRC")
    body, got = data[:-2], data[-2] | data[-1] << 8
    if crc16(body) != got:
        raise CorruptFrame("CRC mismatch")
    return body
