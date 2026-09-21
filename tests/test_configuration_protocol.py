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


def test_encodes_multiple_groups_as_one_atomic_apply():
    combined = ConfigurePlan((
        plan().groups[0],
        GroupPlan("i2c_target1", "i2c_target", (
            RoleAllocation("sda", "dut.sda", "i2c.sda", 50),
            RoleAllocation("scl", "dut.scl", "i2c.scl", 52),
        ), wire_id=2),
    ))
    encoded = ProbeConfigurationClient._encode(combined)
    assert encoded == bytes((1, 4)) + b"".join((
        struct.pack("<HBH", 1, 6, 12),
        struct.pack("<HBH", 1, 7, 6),
        struct.pack("<HBH", 2, 8, 50),
        struct.pack("<HBH", 2, 9, 52),
    ))


def test_encodes_revision2_stable_role_ids():
    revision2 = ConfigurePlan((GroupPlan(
        "uart0", "uart", (
            RoleAllocation("rx", "dut.tx", "uart.rx", 12, 1),
            RoleAllocation("tx", "dut.rx", "uart.tx", 6, 2),
        ), wire_id=1),))
    assert ProbeConfigurationClient._encode(revision2) == bytes((2, 2)) + b"".join((
        struct.pack("<HBBH", 1, 1, 6, 12),
        struct.pack("<HBBH", 1, 2, 7, 6),
    ))
