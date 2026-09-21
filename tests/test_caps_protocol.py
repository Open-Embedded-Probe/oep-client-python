import struct

import pytest

from oep_client import Endpoint, ProbeCapsClient, ProtocolError, caps_to_dict
from oep_client.caps_protocol import FUNCTION_NAMES, FUNCTION_PROBE_CAPS
from oep_client.prototype import FUNCTION_RESULT, OUTCOME_SUCCESS, RESOLUTION_COMPLETED


def function_mask(*names):
    return sum(1 << FUNCTION_NAMES.index(name) for name in names)


class CapsConnection:
    def exchange(self, request):
        role, operation, correlation, target = struct.unpack_from("<BBHH", request)
        assert role == 0x10 and target == FUNCTION_PROBE_CAPS
        ordinal = request[6] if len(request) == 7 else None
        if operation == 1:
            assert ordinal is None
            data = bytes((1, 2, 1, 1))
        elif operation == 2 and ordinal == 0:
            data = struct.pack("<HBBQ", 12, 0, 1,
                               function_mask("gpio.in", "uart.rx"))
        elif operation == 2 and ordinal == 1:
            data = struct.pack("<HBBQ", 6, 0, 1,
                               function_mask("gpio.in", "gpio.out", "uart.tx"))
        elif operation == 3 and ordinal == 0:
            data = struct.pack("<HBBQQ", 1, 1, 0,
                               function_mask("uart.rx", "uart.tx"), 0)
        elif operation == 4 and ordinal == 0:
            data = struct.pack("<BBHH", 1, 1, 3300, 3600)
        else:
            raise AssertionError((operation, ordinal))
        return struct.pack("<BBHHB", FUNCTION_RESULT, RESOLUTION_COMPLETED,
                           correlation, target, OUTCOME_SUCCESS) + data


def test_reads_mcu_independent_paged_caps():
    caps = ProbeCapsClient(Endpoint(), CapsConnection()).get_caps()
    assert caps.channels[0].id == 12
    assert caps.channels[0].functions == frozenset(("gpio.in", "uart.rx"))
    assert caps.channels[1].voltage_domains == frozenset(("domain:1",))
    assert caps.groups[0].id == "uart0"
    assert caps.groups[0].wire_id == 1
    assert caps.groups[0].roles == frozenset(("rx", "tx"))
    assert caps.voltage_domains[0].nominal_mv == 3300
    assert caps_to_dict(caps)["groups"] == [{
        "id": "uart0", "kind": "uart", "roles": ["rx", "tx"],
        "exclusive_with": [], "wire_id": 1, "instance": 0}]


class BadSummaryConnection:
    def exchange(self, request):
        _, _, correlation, target = struct.unpack_from("<BBHH", request)
        return struct.pack("<BBHHB", FUNCTION_RESULT, RESOLUTION_COMPLETED,
                           correlation, target, OUTCOME_SUCCESS) + bytes((2, 0, 0, 0))


def test_rejects_unknown_caps_revision():
    with pytest.raises(ProtocolError, match="unsupported"):
        ProbeCapsClient(Endpoint(), BadSummaryConnection()).get_caps()
