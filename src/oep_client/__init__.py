"""OEP host client. `oep_client.v1` is the current draft; `oep_client.v0` and the top-level modules below are the
earlier prototype, kept for the tools that still use them.

The prototype's names stay importable from here (`from oep_client import Endpoint`), but are loaded only when asked
for, so importing oep_client.v1 no longer pulls the prototype in.
"""

from importlib import import_module

_LAZY = {
    "prototype": ("CORE_REQUEST", "CORE_RESULT", "FUNCTION_REQUEST", "FUNCTION_RESULT", "ConnectionBusyError",
                  "Endpoint", "FunctionResult", "FixtureGpioClient", "FixtureCaptureClient", "FixtureI2cClient",
                  "FixtureUartClient", "ProbeInfo", "ProbeInfoClient", "OfferedFunction", "ProtocolError",
                  "SerialConnection", "TargetControlClient", "TargetFlashClient", "TargetMemoryClient", "crc16_ccitt",
                  "decode_frame", "encode_frame"),
    "capabilities": ("Allocation", "Caps", "Channel", "ConfigurePlan", "Connection", "ConnectionManifest", "GroupPlan",
                     "LeaseRegistry", "PeripheralGroup", "RoleAllocation", "RoleRequest", "resolve", "VoltageDomain",
                     "resolve_group", "resolve_plan", "validate_caps"),
    "caps_protocol": ("ProbeCapsClient", "caps_to_dict"),
    "configuration_protocol": ("ProbeConfigurationClient",),
    "i2c_capture": ("I2cObservation", "decode_i2c_address", "unpack_rmt_symbols"),
}
_WHERE = {name: module for module, names in _LAZY.items() for name in names}
__all__ = sorted(_WHERE)


def __getattr__(name):
    module = _WHERE.get(name)
    if module is None:
        raise AttributeError(f"module 'oep_client' has no attribute {name!r}")
    value = getattr(import_module(f".{module}", __name__), name)
    globals()[name] = value
    return value
