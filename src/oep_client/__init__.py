"""Destructive OEP prototype; no compatibility is promised."""

from .prototype import (
    CORE_REQUEST,
    CORE_RESULT,
    FUNCTION_REQUEST,
    FUNCTION_RESULT,
    Endpoint,
    FunctionResult,
    FixtureGpioClient,
    OfferedFunction,
    ProtocolError,
    SerialConnection,
    TargetControlClient,
    TargetFlashClient,
    TargetMemoryClient,
    crc16_ccitt,
    decode_frame,
    encode_frame,
)

__all__ = [
    "CORE_REQUEST", "CORE_RESULT", "FUNCTION_REQUEST", "FUNCTION_RESULT",
    "Endpoint", "FunctionResult", "FixtureGpioClient", "OfferedFunction", "ProtocolError", "SerialConnection",
    "TargetControlClient", "TargetFlashClient", "TargetMemoryClient", "crc16_ccitt",
    "decode_frame", "encode_frame",
]
