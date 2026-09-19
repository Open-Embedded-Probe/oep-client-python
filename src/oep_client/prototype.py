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

FUNCTION_TARGET_CONTROL = 0x0101
FUNCTION_TARGET_MEMORY = 0x0102
FUNCTION_TARGET_FLASH = 0x0103
FUNCTION_FIXTURE_GPIO = 0x0201
FUNCTION_FIXTURE_UART = 0x0202
FUNCTION_FIXTURE_I2C = 0x0203
FUNCTION_FIXTURE_SPI = 0x0204


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
