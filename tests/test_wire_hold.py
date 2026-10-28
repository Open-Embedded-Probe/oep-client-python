"""debug §2: a connection closed other than by line loss leaves its lines held at its rest (a CH32L103 keeps its
halt across processes); the wire's hold.{} setting turns it off per port; holds (§2.2) lists the held ports; the hold is a
kind of free state - taken like a free pin, never refused for it."""
import time

import pytest

from oep_client import core, endpoint, host as h, riscv, settings, virtual_bench, registry as reg

NO_SWCLK = 0xFFFF   # a one-wire link's swclk (debug §2.1)


def bench():
    start = time.monotonic()
    ep = endpoint.Endpoint(virtual_bench.p4_x035(), lambda: int((time.monotonic() - start) * 1000))
    hst = h.Host(ep.handle)
    core.take(hst, 10000)
    fn = core.find(hst, "oep.wire.rvswd")
    (pair,) = [p for (f, p) in ep.targets if f == fn]
    ep.targets[(fn, pair)].needs_drive = True                 # a CH32L103: the halt goes once the line is not driven
    return ep, hst, riscv.Wire(hst, "oep.wire.rvswd"), pair


def halt_then_end(ep, hst, wire, pair):
    conn, _ = wire.attach(halt=True, pins=pair)
    hst.end()                                                  # the process ends: its connection closes (core §9)
    core.take(hst, 10000)


def test_the_halt_survives_another_session_by_default():
    ep, hst, wire, pair = bench()
    halt_then_end(ep, hst, wire, pair)
    assert [p for p, _ in wire.holds()] == [pair]
    assert wire.connections() == []                            # a hold is no connection
    wire.attach(halt=False, pins=pair)                         # taken like a free pin (never cause 1 / 5)
    assert wire.halted is False and ep.targets[(core.find(hst, "oep.wire.rvswd"), pair)].halted
    assert wire.holds() == []                                  # the taker carries on


def test_wire_hold_off_lets_the_lines_go_and_the_target_loses_its_halt():
    ep, hst, wire, pair = bench()
    st = settings.Settings(hst)
    st.set_named("oep.wire.rvswd/0", {f"hold.GPIO{pair[0]}": False})
    assert ("oep.wire.rvswd/0", f"hold.GPIO{pair[0]}", False) in st.show()
    halt_then_end(ep, hst, wire, pair)
    assert wire.holds() == []
    assert not ep.targets[(core.find(hst, "oep.wire.rvswd"), pair)].halted


def test_a_lost_line_is_not_held():
    ep, hst, wire, pair = bench()
    wire.attach(halt=True, pins=pair)
    ep.lose()
    assert wire.holds() == []


def test_an_idle_item_on_a_held_pin_ends_the_hold():
    ep, hst, wire, pair = bench()
    halt_then_end(ep, hst, wire, pair)
    assert wire.holds()
    settings.Settings(hst).set_named(settings.CORE_CATEGORY, {f"channel.GPIO{pair[1]}.idle": "pull-down"})
    assert wire.holds() == []


def test_holds_needs_no_lock_and_tells_each_ports_rest():
    """debug §2.2: holds is lock-free; an rvswd entry's rest is the closed connection's idle_clock (0 high, 1 low)."""
    ep, hst, wire, pair = bench()
    conn, _ = wire.attach(halt=True, pins=pair)
    rest = reg.WIRE_RVSWD.enum["idle_clock"][wire.connections()[0].idle_clock]   # the connection shows it by name
    hst.end()
    free = riscv.Wire(h.Host(ep.handle), "oep.wire.rvswd")    # no session at all
    assert free.holds() == [(pair, rest)]


def test_a_swio_hold_has_no_swclk_and_rest_0():
    """debug §2.2: swio's entry has swclk 0xFFFF; rest is 0 on wires other than rvswd."""
    ep = endpoint.Endpoint(virtual_bench.esp32_v003(), lambda: 0)
    hst = h.Host(lambda b: ep.handle(b, 0))
    core.take(hst, 10000)
    wire = riscv.Wire(hst, "oep.wire.swio")
    wire.attach(halt=True)
    hst.end()
    core.take(hst, 10000)
    assert [(p[1], r) for p, r in wire.holds()] == [(NO_SWCLK, 0)]


def test_a_restart_ends_the_hold():
    """debug §2: a hold ends when the probe restarts."""
    ep, hst, wire, pair = bench()
    halt_then_end(ep, hst, wire, pair)
    assert wire.holds()
    ep.reboot()
    hst2 = h.Host(ep.handle)
    assert riscv.Wire(hst2, "oep.wire.rvswd").holds() == []


@pytest.mark.parametrize("name", sorted(virtual_bench.PROFILES))
def test_every_wire_answers_holds_without_the_lock(name):
    """debug §3 / §5: holds (0x14) is required on every wire (rvswd, swio, swd) and needs no lock."""
    ep = endpoint.Endpoint(virtual_bench.PROFILES[name](), lambda: 0)
    hst = h.Host(lambda b: ep.handle(b, 0))
    wires = [n for n in ep.names.values() if n.startswith("oep.wire.")]
    for wire in wires:
        assert riscv.Wire(hst, wire).holds() == []
