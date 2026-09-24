"""UART binding framing: CRC-16/CCITT-FALSE and COBS, including the block edges."""

import random

import pytest

from oep_client.v1 import cobs


def test_crc_check_value():
    assert cobs.crc16(b"123456789") == 0x29B1


@pytest.mark.parametrize("data", [
    b"\x00", b"\x00\x00", b"\x11\x00\x22", b"\x11\x22\x00",
    bytes(range(1, 255)),                    # exactly 254 non-zero bytes
    bytes(range(1, 255)) + b"\x00",          # a full block followed by a zero
    bytes(range(1, 255)) + b"\x07",          # a full block followed by more data
    bytes([1]) * 600,                        # several full blocks
])
def test_edges_round_trip_and_never_contain_the_delimiter(data):
    enc = cobs.encode(data)
    assert 0 not in enc
    assert cobs.decode(enc) == data


def test_known_encodings():
    assert cobs.encode(b"\x00") == b"\x01\x01"
    assert cobs.encode(b"\x11\x22\x00\x33") == b"\x03\x11\x22\x02\x33"
    assert cobs.encode(bytes(range(1, 255))) == b"\xff" + bytes(range(1, 255)) + b"\x01"


def test_random_frames_round_trip():
    rng = random.Random(7)
    for _ in range(300):
        msg = rng.randbytes(rng.randrange(1, 600))
        f = cobs.frame(msg)
        assert f[-1] == 0 and 0 not in f[:-1]
        assert cobs.unframe(f[:-1]) == msg


def test_a_flipped_byte_is_caught():
    f = bytearray(cobs.frame(b"\x02\x01\x00\x01\x00hello"))
    f[3] ^= 0x10
    with pytest.raises(cobs.CorruptFrame):
        cobs.unframe(bytes(f[:-1]))
