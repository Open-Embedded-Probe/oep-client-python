"""Atomic capability-plan application for generic development probes."""
import struct

from .capabilities import Allocation, ConfigurePlan
from .caps_protocol import FUNCTION_NAMES
from .prototype import Endpoint, FunctionResult, ProtocolError

FUNCTION_PROBE_CONFIGURATION = 0x0003
CONFIGURATION_APPLY = 0x01
CONFIGURATION_RELEASE = 0x02
CONFIGURATION_REVISION = 1
CONFIGURATION_REVISION_ROLE_IDS = 2


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
                entries.append((group.wire_id, role.wire_role_id, function, role.channel))
        if not entries:
            raise ValueError("configuration plan must contain at least one role")
        has_role_ids = [role_id is not None for _, role_id, _, _ in entries]
        if any(has_role_ids) and not all(has_role_ids):
            raise ValueError("configuration plan mixes role-id revisions")
        revision = (CONFIGURATION_REVISION_ROLE_IDS if all(has_role_ids)
                    else CONFIGURATION_REVISION)
        maximum = 14 if revision == CONFIGURATION_REVISION_ROLE_IDS else 17
        if len(entries) > maximum:
            raise ValueError(f"configuration plan must contain 1..{maximum} roles")
        if len({(group, role_id if revision == 2 else function)
                for group, role_id, function, _ in entries}) != len(entries):
            raise ValueError("configuration plan repeats a group role")
        if any(group < 0 or group > 0xffff or channel < 0 or channel > 0xffff
               for group, _, _, channel in entries):
            raise ValueError("configuration plan contains an out-of-range id")
        if revision == CONFIGURATION_REVISION:
            return bytes((revision, len(entries))) + b"".join(
                struct.pack("<HBH", group, function, channel)
                for group, _, function, channel in entries)
        return bytes((revision, len(entries))) + b"".join(
            struct.pack("<HBBH", group, role_id, function, channel)
            for group, role_id, function, channel in entries)

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
