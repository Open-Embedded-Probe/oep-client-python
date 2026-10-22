"""oep-spec 9ed53e7 / 3cc50c8 against the virtual bench: describe of an fn not offered is unknown_function (core §4.3), unset of
an item the probe does not declare is unsupported with the value's place (core §13.5), and an idle change takes effect
at once on a free channel and at the next release on a held one (core §13.8)."""

import struct

import pytest

from oep_client import core, settings, endpoint, virtual_bench, fixture, host as h, message as m, riscv

V003_PAIR = (16, 0xFFFF)                                          # esp32-v003: swio on 16


def v003(silent=True):
    clock = Clock()
    ep = endpoint.Endpoint(virtual_bench.esp32_v003(), clock)
    tg = ep.targets[(1, V003_PAIR)]
    tg.silent_until_reset = silent
    return ep, clock, tg


class Clock:
    t = 0

    def __call__(self):
        return self.t


def open_probe(probe=None):
    ep = endpoint.Endpoint(probe or virtual_bench.p4_bench(), Clock())
    hst = h.Host(lambda b: ep.handle(b, 1))
    hst.open(3000)
    return ep, hst


def idle(hst, values: dict) -> None:
    """channel.{}.idle values by channel (None: unset), through the client."""
    st = settings.Settings(hst)
    sets = {f"channel.GPIO{ch}.idle": mode for ch, mode in values.items() if mode is not None}
    unsets = [f"channel.GPIO{ch}.idle" for ch, mode in values.items() if mode is None]
    if sets:
        st.set_named(settings.CORE_CATEGORY, sets)
    if unsets:
        st.unset_named(settings.CORE_CATEGORY, unsets)


def rejected(fn):
    with pytest.raises(h.Rejected) as e:
        fn()
    return e.value.result


# ---- core §4.3 --------------------------------------------------------------------------------------------------

def test_describe_of_an_fn_not_offered_is_unknown_function():
    ep, hst = open_probe()
    missing = max(ep.names) + 1
    r = rejected(lambda: hst.call(m.CORE_FN, m.OP_DESCRIBE, struct.pack("<HH", missing, 0), locked=False))
    assert r.detail == m.UNKNOWN_FUNCTION and r.payload == b""
    r = rejected(lambda: hst.call(m.CORE_FN, m.OP_DESCRIBE, struct.pack("<H", 0), locked=False))
    assert r.detail == m.MALFORMED                                 # shorter than the fixed part stays malformed


# ---- unset of an undeclared tag -----------------------------------------------------------------------------------

@pytest.mark.parametrize("number", [0x30, 0xB0, 0x7E])
def test_unset_of_an_undeclared_item_is_unsupported_with_its_place(number):
    ep, hst = open_probe()
    body = bytes([2]) + struct.pack("<HBH", 0, ep.setting_number("channel.{}.idle"), 20) + struct.pack("<HBH", 0, number, 1)
    with pytest.raises(h.Unsupported) as e:                         # a declared one first, then one past the list
        hst.call(m.CORE_FN, settings.OP["unset"], body)
    assert e.value.item == 1


# ---- an idle change: at once when free, at the next release when held ---------------------------------------------

def test_idle_set_and_unset_on_a_free_channel_apply_at_once():
    ep, hst = open_probe()
    idle(hst, {20: "high"})
    assert ep.parked[20] == 4
    idle(hst, {20: "low"})
    assert ep.parked[20] == 3
    idle(hst, {20: None})
    assert ep.parked[20] == 0


def test_idle_change_on_a_channel_a_plan_holds_waits_for_the_release():
    ep, hst = open_probe()
    idle(hst, {20: "low"})
    g = fixture.Gpio(hst, ep.fns["oep.fixture.gpio"])
    core.plan_apply(hst, [(g.fn, 1, 20), (g.fn, 1, 21)])
    g.set([(21, g.OUTPUT_HIGH)])
    idle(hst, {20: "high", 21: "low"})
    assert g.read([20, 21]) == [0, 1]                              # held: the lines stay as they were
    assert ep.parked[20] == 3                                      # the old idle state, not yet the new one
    core.plan_release(hst, [g.fn])
    assert (ep.parked[20], ep.parked[21]) == (4, 3)                # released: the new idle states


def test_idle_unset_on_a_held_channel_waits_for_the_release():
    ep, hst = open_probe()
    idle(hst, {20: "high"})
    g = fixture.Gpio(hst, ep.fns["oep.fixture.gpio"])
    core.plan_apply(hst, [(g.fn, 1, 20)])
    idle(hst, {20: None})
    assert g.read([20]) == [1]                                     # taken in the old idle state, still so
    core.plan_release(hst, [g.fn])
    assert ep.parked[20] == 0                                      # released: Hi-Z


def test_idle_change_on_a_connection_pin_waits_for_the_detach():
    ep, clock, _ = v003(silent=False)
    hst = h.Host(lambda b: ep.handle(b, 1))
    hst.open(3000)
    wire = riscv.Wire(hst, "oep.wire.swio")
    conn, _ = wire.attach(halt=False, pins=V003_PAIR)
    swio = V003_PAIR[0]
    idle(hst, {swio: "high"})
    assert ep.parked.get(swio, 0) == 0                             # the connection holds it
    wire.detach(conn)
    assert ep.parked[swio] == 4
