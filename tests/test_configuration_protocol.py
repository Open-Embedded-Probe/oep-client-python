import struct

import pytest

from oep_client import (
    ConfigurePlan, Endpoint, GroupPlan, ProbeConfigurationClient,
    RoleAllocation,
)
from oep_client.configuration_protocol import FUNCTION_PROBE_CONFIGURATION
from oep_client.prototype import FUNCTION_RESULT, OUTCOME_SUCCESS, RESOLUTION_COMPLETED


def plan():
    return ConfigurePlan((GroupPlan(
        "uart1", "uart", (
            RoleAllocation("rx", "dut.tx", "uart.rx", 12),
            RoleAllocation("tx", "dut.rx", "uart.tx", 6),
        ), wire_id=1),))


class ConfigurationConnection:
    def __init__(self):
        self.requests = []

    def exchange(self, request):
        self.requests.append(request)
        role, operation, correlation, target = struct.unpack_from("<BBHH", request)
        assert role == 0x10 and target == FUNCTION_PROBE_CONFIGURATION
        if operation == 1:
            assert request[6:] == bytes((1, 2)) + struct.pack(
                "<HBH", 1, 6, 12) + struct.pack("<HBH", 1, 7, 6)
            data = struct.pack("<I", 0x23)
        else:
            assert operation == 2 and request[6:] == struct.pack("<I", 0x23)
            data = b""
        return struct.pack("<BBHHB", FUNCTION_RESULT, RESOLUTION_COMPLETED,
                           correlation, target, OUTCOME_SUCCESS) + data


def test_applies_and_releases_atomic_plan():
    connection = ConfigurationConnection()
    client = ProbeConfigurationClient(Endpoint(), connection)
    allocation = client.apply(plan())
    assert allocation.lease_id == "00000023"
    assert client.release(allocation).succeeded


def test_rejects_plan_without_wire_group():
    invalid = ConfigurePlan((GroupPlan("uart", "uart", (), wire_id=None),))
    with pytest.raises(ValueError, match="wire id"):
        ProbeConfigurationClient(Endpoint(), ConfigurationConnection()).apply(invalid)
