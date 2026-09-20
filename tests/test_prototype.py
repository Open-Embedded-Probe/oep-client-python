import struct

import pytest

from oep_client import Endpoint, ProtocolError, decode_frame, encode_frame
from oep_client.prototype import (
    FUNCTION_TARGET_CONTROL,
    FixtureGpioClient,
    FixtureI2cClient,
    FIXTURE_INPUT_PULL_UP_DOWN,
    FixtureUartClient,
    OUTCOME_FAILED,
    OUTCOME_SUCCESS,
    REJECTION_OPERATION,
    RESOLUTION_COMPLETED,
    RESOLUTION_REJECTED,
    TARGET_GET_STATUS,
    TargetFlashClient,
    TargetMemoryClient,
)


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


def test_function_request_and_completed_result():
    endpoint = Endpoint()
    correlation, request = endpoint.function_request(FUNCTION_TARGET_CONTROL, TARGET_GET_STATUS)
    assert request == struct.pack(
        "<BBHH", 0x10, TARGET_GET_STATUS, correlation, FUNCTION_TARGET_CONTROL)
    response = struct.pack(
        "<BBHHBBBB", 0x90, RESOLUTION_COMPLETED, correlation,
        FUNCTION_TARGET_CONTROL, OUTCOME_SUCCESS, 5, 2, 1)
    result = endpoint.parse_function_result(response, correlation, FUNCTION_TARGET_CONTROL)
    assert result.succeeded
    assert result.data == b"\x05\x02\x01"


@pytest.mark.parametrize("resolution, detail", [
    (RESOLUTION_REJECTED, REJECTION_OPERATION),
    (RESOLUTION_COMPLETED, OUTCOME_FAILED),
])
def test_rejection_is_distinct_from_execution_failure(resolution, detail):
    endpoint = Endpoint()
    correlation, _ = endpoint.function_request(FUNCTION_TARGET_CONTROL, 0x7F)
    response = struct.pack(
        "<BBHHB", 0x90, resolution, correlation, FUNCTION_TARGET_CONTROL, detail)
    result = endpoint.parse_function_result(response, correlation, FUNCTION_TARGET_CONTROL)
    assert not result.succeeded
    assert result.resolution == resolution
    assert result.detail == detail


def test_function_result_must_match_target_and_correlation():
    endpoint = Endpoint()
    correlation, _ = endpoint.function_request(FUNCTION_TARGET_CONTROL, TARGET_GET_STATUS)
    response = struct.pack(
        "<BBHHB", 0x90, RESOLUTION_COMPLETED, correlation,
        FUNCTION_TARGET_CONTROL, OUTCOME_SUCCESS)
    with pytest.raises(ProtocolError):
        endpoint.parse_function_result(response, correlation + 1, FUNCTION_TARGET_CONTROL)
    with pytest.raises(ProtocolError):
        endpoint.parse_function_result(response, correlation, FUNCTION_TARGET_CONTROL + 1)


class MemoryConnection:
    def exchange(self, request):
        role, operation, correlation, target, address, length = struct.unpack("<BBHHIB", request)
        assert (role, operation, target, address) == (0x10, 1, 0x0102, 0x08000000)
        data = bytes(range(length))
        return struct.pack("<BBHHB", 0x90, RESOLUTION_COMPLETED, correlation,
                           target, OUTCOME_SUCCESS) + data


def test_bounded_memory_read():
    client = TargetMemoryClient(Endpoint(), MemoryConnection())
    assert client.read(0x08000000, 8) == bytes(range(8))
    assert client.read(0x08000000, 88) == bytes(range(88))
    with pytest.raises(ValueError):
        client.read(0x08000001, 8)
    with pytest.raises(ValueError):
        client.read(0x08000000, 92)


class FlashConnection:
    def exchange(self, request):
        role, operation, correlation, target, address = struct.unpack_from("<BBHHI", request)
        assert (role, operation, target, address) == (0x10, 1, 0x0103, 0x08003FC0)
        assert request[10:] == bytes(range(64))
        return struct.pack("<BBHHB", 0x90, RESOLUTION_COMPLETED, correlation,
                           target, OUTCOME_SUCCESS)


def test_program_flash_page64():
    client = TargetFlashClient(Endpoint(), FlashConnection())
    result = client.program_page64(0x08003FC0, bytes(range(64)))
    assert result.succeeded
    with pytest.raises(ValueError):
        client.program_page64(0x08003FC1, bytes(64))
    with pytest.raises(ValueError):
        client.program_page64(0x08003FC0, bytes(63))


class GpioConnection:
    def exchange(self, request):
        role, operation, correlation, target = struct.unpack_from("<BBHH", request)
        if operation == 1:
            assert request[6] == 27
            data = b"\x01"
        elif operation == 2:
            assert operation == 2 and len(request) == 6
            data = struct.pack("<QQ", 0x35, 0x21)
        else:
            assert operation == 3 and request[6:] == bytes((46, FIXTURE_INPUT_PULL_UP_DOWN))
            data = b""
        return struct.pack("<BBHHB", 0x90, RESOLUTION_COMPLETED, correlation,
                           target, OUTCOME_SUCCESS) + data


def test_fixture_digital_read():
    client = FixtureGpioClient(Endpoint(), GpioConnection())
    assert client.read_digital(27) == 1
    assert client.read_digital_bank() == (0x35, 0x21)
    assert client.configure_digital(46, FIXTURE_INPUT_PULL_UP_DOWN).succeeded


class UartConnection:
    def exchange(self, request):
        role, operation, correlation, target = struct.unpack_from("<BBHH", request)
        assert (role, target) == (0x10, 0x0202)
        if operation == 1:
            assert request[6:] == struct.pack("<I", 115200)
            data = struct.pack("<I", 115200)
        elif operation == 2:
            assert request[6:] == b"PING\n"
            data = bytes((5,))
        else:
            assert request[6:] == b"\x40"
            data = b"\x05PONG\n"
        return struct.pack("<BBHHB", 0x90, RESOLUTION_COMPLETED, correlation,
                           target, OUTCOME_SUCCESS) + data


def test_fixture_uart_operations():
    uart = FixtureUartClient(Endpoint(), UartConnection())
    assert uart.configure(115200) == 115200
    assert uart.write(b"PING\n") == 5
    assert uart.read_available() == b"PONG\n"


class I2cConnection:
    def exchange(self, request):
        role, operation, correlation, target = struct.unpack("<BBHH", request)
        assert (role, operation, target, len(request)) == (0x10, 1, 0x0203, 6)
        data = struct.pack("<BBHHI", 0x07, 3, 4, 5, 100000)
        return struct.pack("<BBHHB", 0x90, RESOLUTION_COMPLETED, correlation,
                           target, OUTCOME_SUCCESS) + data


def test_fixture_i2c_status():
    status = FixtureI2cClient(Endpoint(), I2cConnection()).get_status()
    assert status == {
        "flags": 0x07,
        "last_rx_length": 3,
        "rx_transactions": 4,
        "request_transactions": 5,
        "frequency_hz": 100000,
    }
