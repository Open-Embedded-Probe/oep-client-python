"""debug §3.1: an rvswd attach without idle_clock rests the line as the target's family needs - read from the
target_id the attach read (registry oep.wire.rvswd idle_low_family) - and every attach answers the rest it uses;
connections lists it per entry. The host's TLV still decides."""
import struct
import time

import pytest

from oep_client import config, core, endpoint, host as h, message as m, registry as reg, riscv, virtual_bench

RVSWD = reg.WIRE_RVSWD.enum
SHIFT = RVSWD["target_id_family"]["shift"]
LOW_FAMILY = RVSWD["idle_low_family"]["ch32l103"]
L103_ID = (LOW_FAMILY << SHIFT) | 0x00010710          # a CH32L103's DMI 0x7F (the family in bits 31-20)
OTHER_ID = (0x035 << SHIFT) | 0x00010510              # a family not in the table (a CH32X035)


def bench(target_id):
    start = time.monotonic()
    ep = endpoint.Endpoint(virtual_bench.p4_x035(), lambda: int((time.monotonic() - start) * 1000))
    hst = h.Host(ep.handle)
    core.take(hst, 10000)
    fn = core.find(hst, "oep.wire.rvswd")
    (pair,) = [p for (f, p) in ep.targets if f == fn]
    ep.targets[(fn, pair)].target_id = target_id
    return ep, hst, riscv.Wire(hst, "oep.wire.rvswd"), pair


@pytest.mark.parametrize("target_id, given, rest", [
    (L103_ID, None, "low"), (OTHER_ID, None, "high"), (0, None, "high"),
    (L103_ID, "high", "high"), (OTHER_ID, "low", "low"),
], ids=["l103-from-id", "other-from-id", "no-id-high", "host-high-wins", "host-low-wins"])
def test_the_rest_comes_from_the_target_id_unless_the_host_says(target_id, given, rest):
    ep, hst, wire, pair = bench(target_id)
    conn, _ = wire.attach(halt=False, pins=pair, idle_clock=given)
    assert wire.idle_clock == rest
    (row,) = wire.connections()
    assert (row.connection, row.idle_clock) == (conn, rest)
    assert ep.conns[conn].idle_clock == RVSWD["idle_clock"][rest]


@pytest.mark.parametrize("setting, given, target_id, rest", [
    ("high", None, L103_ID, "high"), ("low", None, OTHER_ID, "low"), ("auto", None, L103_ID, "low"),
    ("high", "low", L103_ID, "low"),
], ids=["setting-high-off", "setting-low-off", "setting-auto", "tlv-over-setting"])
def test_the_setting_turns_the_automatic_rest_off_per_port(setting, given, target_id, rest):
    """probe.config rvswd-rest (key: the port's SWCLK channel): high / low fixed, auto the probe's choice; the attach's
    TLV still decides (debug §3.1)."""
    ep, hst, wire, pair = bench(target_id)
    config.ProbeConfig(hst).set([config.RvswdRest(channel=pair[1], mode=setting)])
    assert config.RvswdRest(channel=pair[1], mode=setting) in config.ProbeConfig(hst).items()
    wire.attach(halt=False, pins=pair, idle_clock=given)
    assert wire.idle_clock == rest


def test_an_unused_rest_mode_is_refused():
    ep, hst, wire, pair = bench(L103_ID)
    item = struct.pack("<BHHB", config.ITEM["rvswd_rest"], 3, pair[1], max(config.REST_MODE.values()) + 1)
    with pytest.raises(h.Rejected) as e:
        config.ProbeConfig(hst).set([item])
    assert e.value.result.detail == m.UNSUPPORTED
