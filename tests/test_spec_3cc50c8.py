"""oep-spec 9ed53e7 / 3cc50c8 against the fake: describe of an fn not offered is unknown_function (core §4.3), unset of
an undeclared item tag is unsupported with the tag as received (probe.config §2), the retry with reset counts in
last_try_at_ns (§3.3), and an idle change takes effect at once on a free channel and at the next release on a held one
(probe.config §1: the level and the drive)."""

import struct

import pytest

from oep_client import config, core, endpoint, fake, fixture, host as h, message as m, riscv
from oep_client.fixture import Drive

from test_drive_and_boot_reset import NRST, V003_PAIR, items, slot_state, v003


class Clock:
    t = 0

    def __call__(self):
        return self.t


def open_probe(probe=None):
    ep = endpoint.Endpoint(probe or fake.p4_bench(), Clock())
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
    body = bytes([2, 3, config.ITEM["idle"], 20, 0, 3, tag, 1, 0])  # a declared key first, then the undeclared tag
    r = rejected(lambda: hst.call(cfg_fn, config.ProbeConfig.UNSET, body))
    assert r.detail == m.UNSUPPORTED and r.payload == bytes([tag])


# ---- last_try_at_ns counts the retry with reset ---------------------------------------------------------------------

def test_the_retry_with_reset_updates_last_try_at():
    ep, clock, _ = v003(reset_line=4)                              # the retry does not help: stays absent
    ep.load_config(items())
    st = slot_state(ep)
    assert st.reset_at_ns == 7_000_000
    hold = endpoint.RETRY_RESET_HOLD_MS
    assert st.last_try_at_ns == (7 + hold) * 1_000_000             # the retry's attach, after the hold
    clock.t = 7 + 1000                                             # retry_ms 1000 counts from the retry, not before
    ep.tick()
    assert slot_state(ep).last_try_at_ns == (7 + hold) * 1_000_000
    clock.t = 7 + hold + 1000
    ep.tick()
    assert slot_state(ep).last_try_at_ns == (7 + hold + 1000) * 1_000_000
    assert len(ep.slot_reset_log) == 1


def test_last_try_at_without_a_retry_with_reset_is_the_first_attempt():
    ep, _, _ = v003()
    ep.load_config(items(boot_reset=False))
    st = slot_state(ep)
    assert st.last_try_at_ns == 7_000_000 and st.reset_at_ns is None


# ---- an idle change: at once when free, at the next release when held ---------------------------------------------

def test_idle_set_and_unset_on_a_free_channel_apply_at_once():
    ep, hst = open_probe()
    cfg = config.ProbeConfig(hst)
    cfg.set([config.Idle(channel=20, mode="output-high", drive=Drive.level(1))])
    assert ep.parked[20] == 4 and ep.parked_drive[20] == 1
    cfg.set([config.Idle(channel=20, mode="output-low", drive=Drive.level(3))])
    assert ep.parked[20] == 3 and ep.parked_drive[20] == 3
    cfg.unset([("idle", 20)])
    assert ep.parked[20] == 0 and 20 not in ep.parked_drive


def test_idle_change_on_a_channel_a_plan_holds_waits_for_the_release():
    ep, hst = open_probe()
    cfg = config.ProbeConfig(hst)
    cfg.set([config.Idle(channel=20, mode="output-low", drive=Drive.level(0))])
    g = fixture.Gpio(hst, ep.fns["oep.fixture.gpio"])
    core.plan_apply(hst, [(g.fn, 1, 20), (g.fn, 1, 21)])
    g.set([(21, g.OUTPUT_HIGH, 3)])
    cfg.set([config.Idle(channel=20, mode="output-high", drive=Drive.level(2)),
             config.Idle(channel=21, mode="output-low", drive=Drive.level(1))])
    st = g.read_state([20, 21])
    assert st.levels == [0, 1] and st.drive == [0, 3]              # held: the lines stay as they were
    assert ep.parked[20] == 3 and ep.parked_drive[20] == 0         # the old idle state, not yet the new one
    core.plan_release(hst, [g.fn])
    assert (ep.parked[20], ep.parked_drive[20]) == (4, 2)          # released: the new idle, level and drive
    assert (ep.parked[21], ep.parked_drive[21]) == (3, 1)


def test_idle_unset_on_a_held_channel_waits_for_the_release():
    ep, hst = open_probe()
    cfg = config.ProbeConfig(hst)
    cfg.set([config.Idle(channel=20, mode="output-high", drive=Drive.level(1))])
    g = fixture.Gpio(hst, ep.fns["oep.fixture.gpio"])
    core.plan_apply(hst, [(g.fn, 1, 20)])
    cfg.unset([("idle", 20)])
    st = g.read_state([20])
    assert st.levels == [1] and st.drive == [1]                    # taken in the old idle state, still so
    core.plan_release(hst, [g.fn])
    assert ep.parked[20] == 0 and 20 not in ep.parked_drive        # released: Hi-Z


def test_idle_change_on_a_connection_pin_waits_for_the_detach():
    ep, clock, _ = v003(silent=False)
    hst = h.Host(lambda b: ep.handle(b, 1))
    hst.open(3000)
    wire = riscv.Wire(hst, "oep.wire.swio")
    conn, _ = wire.attach(halt=False, pins=V003_PAIR)
    swio = V003_PAIR[0]
    config.ProbeConfig(hst).set([config.Idle(channel=swio, mode="output-high", drive=Drive.level(1))])
    assert ep.parked.get(swio, 0) == 0                             # the connection holds it
    wire.detach(conn)
    assert ep.parked[swio] == 4 and ep.parked_drive[swio] == 1


def test_idle_change_on_a_slot_pin_without_a_connection_applies_at_once():
    ep, clock, tg = v003(silent=True)                              # no answer: a slot, no connection on its pin
    ep.load_config(items(boot_reset=False))
    swio = V003_PAIR[0]
    hst = h.Host(lambda b: ep.handle(b, 1))
    hst.open(3000)
    cfg = config.ProbeConfig(hst)
    cfg.set([config.Idle(channel=swio, mode="pull-up")])
    assert ep.parked[swio] == 1                                    # neither a plan nor a connection uses it: free
    tg.silent_until_reset = False
    clock.t = 7 + 1000
    ep.tick()                                                      # the slot's retry attaches: now held
    assert slot_state(ep).state == "connected"
    cfg.set([config.Idle(channel=swio, mode="pull-down")])
    assert ep.parked[swio] == 1                                    # held by the connection: not yet
    cfg.unset([("slot", 0)])                                       # the slot goes, its connection with it
    assert ep.parked[swio] == 2
