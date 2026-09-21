"""Destructive OEP prototype; no compatibility is promised."""

from .prototype import (
    CORE_REQUEST,
    CORE_RESULT,
    FUNCTION_REQUEST,
    FUNCTION_RESULT,
    ConnectionBusyError,
    Endpoint,
    FunctionResult,
    FixtureGpioClient,
    FixtureUartClient,
    ProbeInfo,
    ProbeInfoClient,
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
from .capabilities import Caps, Channel, Connection, ConnectionManifest, PeripheralGroup, resolve

__all__ = [
    "CORE_REQUEST", "CORE_RESULT", "FUNCTION_REQUEST", "FUNCTION_RESULT",
    "ConnectionBusyError", "Endpoint", "FunctionResult", "FixtureGpioClient", "FixtureUartClient", "ProbeInfo", "ProbeInfoClient", "OfferedFunction", "ProtocolError", "SerialConnection",
    "TargetControlClient", "TargetFlashClient", "TargetMemoryClient", "crc16_ccitt",
    "decode_frame", "encode_frame",
    "Caps", "Channel", "Connection", "ConnectionManifest", "PeripheralGroup", "resolve",
]
