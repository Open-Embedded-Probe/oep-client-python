"""Atomic capability-plan application for generic development probes."""
import struct

from .capabilities import Allocation, ConfigurePlan
from .caps_protocol import FUNCTION_NAMES
from .prototype import Endpoint, FunctionResult, ProtocolError

FUNCTION_PROBE_CONFIGURATION = 0x0003
CONFIGURATION_APPLY = 0x01
CONFIGURATION_RELEASE = 0x02
CONFIGURATION_REVISION = 1


class ProbeConfigurationClient:
    """Apply only host-resolved plans; target wiring never reaches the probe."""

    def __init__(self, endpoint: Endpoint, connection) -> None:
        self._endpoint = endpoint
        self._connection = connection

    def _exchange(self, operation: int, payload: bytes = b"") -> FunctionResult:
        correlation, request = self._endpoint.function_request(
            FUNCTION_PROBE_CONFIGURATION, operation, payload)
        return self._endpoint.parse_function_result(
            self._connection.exchange(request), correlation,
            FUNCTION_PROBE_CONFIGURATION)

    @staticmethod
    def _encode(plan: ConfigurePlan) -> bytes:
        entries = []
        for group in plan.groups:
            if group.wire_id is None:
                raise ValueError(f"group {group.group_id} has no probe wire id")
            for role in group.roles:
                try:
                    function = FUNCTION_NAMES.index(role.function)
                except ValueError as error:
                    raise ValueError(
                        f"unknown probe function {role.function}") from error
                entries.append((group.wire_id, function, role.channel))
        if not entries or len(entries) > 17:
            raise ValueError("configuration plan must contain 1..17 roles")
        if len({(group, function) for group, function, _ in entries}) != len(entries):
            raise ValueError("configuration plan repeats a group role")
        if any(group < 0 or group > 0xffff or channel < 0 or channel > 0xffff
               for group, _, channel in entries):
            raise ValueError("configuration plan contains an out-of-range id")
        return bytes((CONFIGURATION_REVISION, len(entries))) + b"".join(
            struct.pack("<HBH", group, function, channel)
            for group, function, channel in entries)

    def apply(self, plan: ConfigurePlan) -> Allocation | FunctionResult:
        result = self._exchange(CONFIGURATION_APPLY, self._encode(plan))
        if not result.succeeded:
            return result
        if len(result.data) != 4:
            raise ProtocolError("malformed configuration lease")
        lease = struct.unpack("<I", result.data)[0]
        if not lease:
            raise ProtocolError("probe returned an empty configuration lease")
        return Allocation(f"{lease:08x}", plan)

    def release(self, allocation: Allocation) -> FunctionResult:
        try:
            lease = int(allocation.lease_id, 16)
        except ValueError as error:
            raise ValueError("allocation has an invalid wire lease id") from error
        if not 0 < lease <= 0xffffffff:
            raise ValueError("allocation has an invalid wire lease id")
        return self._exchange(CONFIGURATION_RELEASE, struct.pack("<I", lease))
