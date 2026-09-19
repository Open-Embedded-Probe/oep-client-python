"""Destructive OEP prototype; no compatibility is promised."""

from .prototype import (
    CORE_REQUEST,
    CORE_RESULT,
    FUNCTION_REQUEST,
    FUNCTION_RESULT,
    Endpoint,
    OfferedFunction,
    ProtocolError,
    SerialConnection,
    crc16_ccitt,
    decode_frame,
    encode_frame,
)

__all__ = [
    "CORE_REQUEST", "CORE_RESULT", "FUNCTION_REQUEST", "FUNCTION_RESULT",
    "Endpoint", "OfferedFunction", "ProtocolError", "SerialConnection", "crc16_ccitt",
    "decode_frame", "encode_frame",
]
