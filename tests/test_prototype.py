import struct

import pytest

from oep_client import Endpoint, ProtocolError, decode_frame, encode_frame


def test_frame_round_trip_and_corruption():
    message = bytes(range(32)) + b"\0\xff"
    wire = encode_frame(message)
    assert decode_frame(wire) == message
    damaged = bytearray(wire)
    damaged[4] ^= 1
    with pytest.raises(ProtocolError):
        decode_frame(bytes(damaged))


def test_confirmation_correlation():
    endpoint = Endpoint()
    correlation, request = endpoint.confirm_request()
    assert request == struct.pack("<BBH4sBB", 1, 1, correlation, b"OEP?", 1, 1)
    response = struct.pack("<BBH4sBBHBB", 0x81, 1, correlation, b"OEP!", 0, 1, 64, 1, 1)
    assert endpoint.parse_confirm(response, correlation)["maximum_message"] == 64
    with pytest.raises(ProtocolError):
        endpoint.parse_confirm(response, correlation + 1)


def test_function_list():
    endpoint = Endpoint()
    correlation, _ = endpoint.list_functions_request()
    entries = struct.pack("<HBBHBB", 0x0101, 1, 0, 0x0103, 1, 1)
    response = struct.pack("<BBHB", 0x81, 2, correlation, 0) + bytes((2,)) + entries
    functions = endpoint.parse_functions(response, correlation)
    assert [(item.reference, item.revision, item.flags) for item in functions] == [
        (0x0101, 1, 0), (0x0103, 1, 1)
    ]
