"""Temporary P0 wire model shared with the Arduino prototype."""

from dataclasses import dataclass
import struct
import time

CORE_REQUEST = 0x01
CORE_RESULT = 0x81
FUNCTION_REQUEST = 0x10
FUNCTION_RESULT = 0x90

CORE_CONFIRM = 0x01
CORE_LIST_FUNCTIONS = 0x02

STATUS_COMPLETED = 0x00
STATUS_REJECTED_OPERATION = 0x01
STATUS_REJECTED_PAYLOAD = 0x02

RESOLUTION_REJECTED = 0x00
RESOLUTION_COMPLETED = 0x01

REJECTION_TARGET = 0x01
REJECTION_OPERATION = 0x02
REJECTION_PAYLOAD = 0x03
REJECTION_UNAVAILABLE = 0x04

OUTCOME_SUCCESS = 0x00
OUTCOME_FAILED = 0x01

FUNCTION_TARGET_CONTROL = 0x0101
FUNCTION_TARGET_MEMORY = 0x0102
FUNCTION_TARGET_FLASH = 0x0103
FUNCTION_FIXTURE_GPIO = 0x0201
FUNCTION_FIXTURE_UART = 0x0202
FUNCTION_FIXTURE_I2C = 0x0203
FUNCTION_FIXTURE_SPI = 0x0204

TARGET_GET_STATUS = 0x01
TARGET_NORMALIZE_USER = 0x02
TARGET_ENTER_PRODUCT_BOOTLOADER = 0x03
TARGET_READ_MEMORY = 0x01
TARGET_PROGRAM_PAGE64 = 0x01


class ProtocolError(ValueError):
    pass


def crc16_ccitt(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def _cobs_encode(data: bytes) -> bytes:
    output = bytearray(b"\x00")
    code_index = 0
    code = 1
    for byte in data:
        if byte == 0:
            output[code_index] = code
            code_index = len(output)
            output.append(0)
            code = 1
        else:
            output.append(byte)
            code += 1
            if code == 0xFF:
                output[code_index] = code
                code_index = len(output)
                output.append(0)
                code = 1
    output[code_index] = code
    return bytes(output)


def _cobs_decode(data: bytes) -> bytes:
    output = bytearray()
    index = 0
    while index < len(data):
        code = data[index]
        if code == 0 or index + code > len(data) + 1:
            raise ProtocolError("invalid COBS frame")
        index += 1
        output.extend(data[index:index + code - 1])
        index += code - 1
        if code != 0xFF and index < len(data):
            output.append(0)
    return bytes(output)


def encode_frame(message: bytes) -> bytes:
    if len(message) > 255:
        raise ProtocolError("prototype message exceeds 255 bytes")
    raw = bytes((len(message),)) + message
    raw += struct.pack("<H", crc16_ccitt(raw))
    return _cobs_encode(raw) + b"\x00"


def decode_frame(wire: bytes) -> bytes:
    if not wire.endswith(b"\x00") or wire == b"\x00":
        raise ProtocolError("incomplete frame")
    raw = _cobs_decode(wire[:-1])
    if len(raw) < 3 or raw[0] != len(raw) - 3:
        raise ProtocolError("length mismatch")
    expected = struct.unpack_from("<H", raw, len(raw) - 2)[0]
    if crc16_ccitt(raw[:-2]) != expected:
        raise ProtocolError("CRC mismatch")
    return raw[1:-2]


@dataclass(frozen=True)
class OfferedFunction:
    reference: int
    revision: int
    flags: int


@dataclass(frozen=True)
class FunctionResult:
    resolution: int
    target: int
    detail: int
    data: bytes

    @property
    def succeeded(self) -> bool:
        return self.resolution == RESOLUTION_COMPLETED and self.detail == OUTCOME_SUCCESS


class Endpoint:
    """Stateless message builder/parser for the destructive P0 prototype."""

    def __init__(self) -> None:
        self._next_correlation = 1

    def _correlation(self) -> int:
        value = self._next_correlation
        self._next_correlation = value % 0xFFFF + 1
        return value

    def confirm_request(self) -> tuple[int, bytes]:
        correlation = self._correlation()
        return correlation, struct.pack("<BBH4sBB", CORE_REQUEST, CORE_CONFIRM, correlation, b"OEP?", 1, 1)

    def list_functions_request(self) -> tuple[int, bytes]:
        correlation = self._correlation()
        return correlation, struct.pack("<BBHB", CORE_REQUEST, CORE_LIST_FUNCTIONS, correlation, 0)

    def function_request(self, target: int, operation: int, payload: bytes = b"") -> tuple[int, bytes]:
        correlation = self._correlation()
        return correlation, struct.pack("<BBHH", FUNCTION_REQUEST, operation, correlation, target) + payload

    @staticmethod
    def parse_confirm(message: bytes, correlation: int) -> dict[str, int]:
        if len(message) != 14:
            raise ProtocolError("unexpected confirmation length")
        role, operation, actual, identity, status, revision, maximum, inflight, flags = struct.unpack("<BBH4sBBHBB", message)
        if (role, operation, actual, identity) != (CORE_RESULT, CORE_CONFIRM, correlation, b"OEP!"):
            raise ProtocolError("confirmation mismatch")
        return {"status": status, "revision": revision, "maximum_message": maximum,
                "maximum_inflight": inflight, "flags": flags}

    @staticmethod
    def parse_functions(message: bytes, correlation: int) -> list[OfferedFunction]:
        if len(message) < 5 or message[0] != CORE_RESULT or message[1] != CORE_LIST_FUNCTIONS:
            raise ProtocolError("not a function-list result")
        actual = struct.unpack_from("<H", message, 2)[0]
        if actual != correlation or message[4] != STATUS_COMPLETED:
            raise ProtocolError("function-list rejection or correlation mismatch")
        payload = message[5:]
        if not payload or len(payload) != 1 + payload[0] * 4:
            raise ProtocolError("malformed function list")
        return [OfferedFunction(*struct.unpack_from("<HBB", payload, 1 + i * 4))
                for i in range(payload[0])]

    @staticmethod
    def parse_function_result(message: bytes, correlation: int, target: int) -> FunctionResult:
        if len(message) < 7 or message[0] != FUNCTION_RESULT:
            raise ProtocolError("not a function result")
        resolution, actual_correlation, actual_target = struct.unpack_from("<BHH", message, 1)
        if actual_correlation != correlation or actual_target != target:
            raise ProtocolError("function result mismatch")
        if resolution not in (RESOLUTION_REJECTED, RESOLUTION_COMPLETED):
            raise ProtocolError("unknown function resolution")
        detail = message[6]
        data = message[7:]
        if resolution == RESOLUTION_REJECTED and data:
            raise ProtocolError("rejected result has unexpected data")
        return FunctionResult(resolution, target, detail, data)


class TargetControlClient:
    def __init__(self, endpoint: Endpoint, connection: "SerialConnection") -> None:
        self._endpoint = endpoint
        self._connection = connection

    def _exchange(self, operation: int) -> FunctionResult:
        correlation, request = self._endpoint.function_request(
            FUNCTION_TARGET_CONTROL, operation)
        response = self._connection.exchange(request)
        return self._endpoint.parse_function_result(
            response, correlation, FUNCTION_TARGET_CONTROL)

    def get_status(self) -> dict[str, int] | FunctionResult:
        result = self._exchange(TARGET_GET_STATUS)
        if not result.succeeded:
            return result
        if len(result.data) != 3:
            raise ProtocolError("malformed target status")
        return dict(zip(("flags", "start_mode", "boot_status"), result.data))

    def normalize_user(self) -> FunctionResult:
        return self._exchange(TARGET_NORMALIZE_USER)

    def enter_product_bootloader(self) -> FunctionResult:
        return self._exchange(TARGET_ENTER_PRODUCT_BOOTLOADER)


class TargetMemoryClient:
    def __init__(self, endpoint: Endpoint, connection: "SerialConnection") -> None:
        self._endpoint = endpoint
        self._connection = connection

    def read(self, address: int, length: int) -> bytes | FunctionResult:
        if address & 3 or length < 4 or length > 32 or length & 3:
            raise ValueError("prototype reads must be 4-byte aligned and 4..32 bytes")
        correlation, request = self._endpoint.function_request(
            FUNCTION_TARGET_MEMORY, TARGET_READ_MEMORY,
            struct.pack("<IB", address, length))
        response = self._connection.exchange(request)
        result = self._endpoint.parse_function_result(
            response, correlation, FUNCTION_TARGET_MEMORY)
        if not result.succeeded:
            return result
        if len(result.data) != length:
            raise ProtocolError("memory result length mismatch")
        return result.data


class TargetFlashClient:
    def __init__(self, endpoint: Endpoint, connection: "SerialConnection") -> None:
        self._endpoint = endpoint
        self._connection = connection

    def program_page64(self, address: int, data: bytes) -> FunctionResult:
        if address & 63 or len(data) != 64:
            raise ValueError("prototype flash writes require one aligned 64-byte page")
        correlation, request = self._endpoint.function_request(
            FUNCTION_TARGET_FLASH, TARGET_PROGRAM_PAGE64,
            struct.pack("<I", address) + data)
        response = self._connection.exchange(request)
        return self._endpoint.parse_function_result(
            response, correlation, FUNCTION_TARGET_FLASH)


class SerialConnection:
    """Temporary synchronous UART connection for the P0 hardware spike."""

    def __init__(self, port: str, baudrate: int = 115200, timeout: float = 2.0):
        import serial
        self._serial = serial.Serial(port, baudrate, timeout=0.05)
        self._timeout = timeout
        time.sleep(0.2)
        self._serial.reset_input_buffer()

    def close(self) -> None:
        self._serial.close()

    def exchange(self, message: bytes) -> bytes:
        self._serial.write(encode_frame(message))
        self._serial.flush()
        wire = bytearray()
        deadline = time.monotonic() + self._timeout
        while time.monotonic() < deadline:
            byte = self._serial.read(1)
            if not byte:
                continue
            wire += byte
            if byte == b"\0":
                return decode_frame(bytes(wire))
            if len(wire) > 512:
                raise ProtocolError("oversized UART frame")
        raise TimeoutError("OEP prototype response timeout")
