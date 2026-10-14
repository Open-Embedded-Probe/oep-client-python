"""oep-spec 9ed53e7 / 3cc50c8 against the virtual bench: describe of an fn not offered is unknown_function (core §4.3), unset of
an undeclared item tag is unsupported with the tag as received (probe.config §2), and an idle change takes effect at
once on a free channel and at the next release on a held one (probe.config §1)."""

import struct

import pytest

from oep_client import config, core, endpoint, virtual_bench, fixture, host as h, message as m, riscv

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

@pytest.mark.parametrize("tag", [0x30, 0xB0, 0x7E])
def test_unset_of_an_undeclared_tag_is_unsupported_with_the_tag_as_received(tag):
    ep, hst = open_probe()
    cfg_fn = ep.fns["oep.probe.config"]
    assert tag & 0x7F not in ep.items
    body = bytes([2, config.ITEM["idle"], 20, 0, tag, 1, 0])      # a declared key first, then the undeclared tag
    r = rejected(lambda: hst.call(cfg_fn, config.ProbeConfig.UNSET, body))
    assert r.detail == m.UNSUPPORTED and r.payload == bytes([tag])


# ---- an idle change: at once when free, at the next release when held ---------------------------------------------

def test_idle_set_and_unset_on_a_free_channel_apply_at_once():
    ep, hst = open_probe()
    cfg = config.ProbeConfig(hst)
    cfg.set([config.Idle(channel=20, mode="output-high")])
    assert ep.parked[20] == 4
    cfg.set([config.Idle(channel=20, mode="output-low")])
    assert ep.parked[20] == 3
    cfg.unset([("idle", 20)])
    assert ep.parked[20] == 0


def test_idle_change_on_a_channel_a_plan_holds_waits_for_the_release():
    ep, hst = open_probe()
    cfg = config.ProbeConfig(hst)
    cfg.set([config.Idle(channel=20, mode="output-low")])
    g = fixture.Gpio(hst, ep.fns["oep.fixture.gpio"])
    core.plan_apply(hst, [(g.fn, 1, 20), (g.fn, 1, 21)])
    g.set([(21, g.OUTPUT_HIGH)])
    cfg.set([config.Idle(channel=20, mode="output-high"), config.Idle(channel=21, mode="output-low")])
    assert g.read([20, 21]) == [0, 1]                              # held: the lines stay as they were
    assert ep.parked[20] == 3                                      # the old idle state, not yet the new one
    core.plan_release(hst, [g.fn])
    assert (ep.parked[20], ep.parked[21]) == (4, 3)                # released: the new idle states


def test_idle_unset_on_a_held_channel_waits_for_the_release():
    ep, hst = open_probe()
    cfg = config.ProbeConfig(hst)
    cfg.set([config.Idle(channel=20, mode="output-high")])
    g = fixture.Gpio(hst, ep.fns["oep.fixture.gpio"])
    core.plan_apply(hst, [(g.fn, 1, 20)])
    cfg.unset([("idle", 20)])
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
    config.ProbeConfig(hst).set([config.Idle(channel=swio, mode="output-high")])
    assert ep.parked.get(swio, 0) == 0                             # the connection holds it
    wire.detach(conn)
    assert ep.parked[swio] == 4
