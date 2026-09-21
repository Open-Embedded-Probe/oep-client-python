"""Destructive OEP prototype; no compatibility is promised."""

from .prototype import (
    CORE_REQUEST,
    CORE_RESULT,
    FUNCTION_REQUEST,
    FUNCTION_RESULT,
    ConnectionBusyError,
    Endpoint,
    FunctionResult,
    FixtureGpioClient, FixtureCaptureClient,
    FixtureI2cClient,
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
from .capabilities import (
    Allocation, Caps, Channel, ConfigurePlan, Connection, ConnectionManifest,
    GroupPlan, LeaseRegistry, PeripheralGroup, RoleAllocation, RoleRequest, resolve,
    VoltageDomain, resolve_group, resolve_plan, validate_caps,
)
from .caps_protocol import ProbeCapsClient, caps_to_dict
from .configuration_protocol import ProbeConfigurationClient
from .i2c_capture import I2cObservation, decode_i2c_address, unpack_rmt_symbols

__all__ = [
    "CORE_REQUEST", "CORE_RESULT", "FUNCTION_REQUEST", "FUNCTION_RESULT",
    "ConnectionBusyError", "Endpoint", "FunctionResult", "FixtureGpioClient", "FixtureCaptureClient", "FixtureI2cClient", "FixtureUartClient", "ProbeInfo", "ProbeInfoClient", "OfferedFunction", "ProtocolError", "SerialConnection",
    "TargetControlClient", "TargetFlashClient", "TargetMemoryClient", "crc16_ccitt",
    "decode_frame", "encode_frame",
    "Allocation", "Caps", "Channel", "ConfigurePlan", "Connection",
    "ConnectionManifest", "GroupPlan", "LeaseRegistry", "PeripheralGroup", "RoleAllocation",
    "RoleRequest", "resolve", "resolve_group", "resolve_plan",
    "validate_caps", "VoltageDomain",
    "ProbeCapsClient",
    "ProbeConfigurationClient",
    "caps_to_dict",
    "I2cObservation", "decode_i2c_address", "unpack_rmt_symbols",
]
