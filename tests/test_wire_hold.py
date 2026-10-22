"""debug §2: a connection closed other than by line loss leaves its lines held at its rest (a CH32L103 keeps its
halt across processes); the wire's hold.{} setting turns it off per port; holds (§2.2) lists the held ports; the hold is a
kind of free state - taken like a free pin, never refused for it."""
import time

import pytest

from oep_client import core, endpoint, host as h, riscv, settings, virtual_bench


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
    assert ("oep.wire.rvswd/0", f"hold.GPIO{pair[0]}", False, "user") in st.show()
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
