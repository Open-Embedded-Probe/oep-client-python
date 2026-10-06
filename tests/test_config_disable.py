"""oep.probe.config item disable (0x07, probe.config §1): a channel the probe never uses or touches. Any request naming
it is rejected unavailable cause 5 (held by settings) with the channel; scan's count-0 list skips it; the probe never
parks it; disabling a channel in use is cause 1; idle and disable on one channel is malformed."""

import struct

import pytest

from oep_client import __main__ as cli, config, core, endpoint, fake, fixture, host as h, message as m, riscv

from test_config import Clock, open_bench


def _cause(e) -> tuple[str | None, list[int]]:
    assert isinstance(e.value, h.Unavailable), e.value
    if e.value.cause == "held_by_settings":
        assert e.value.holder_kind == "disabled"                            # holder_kind 6 next to the channel
    return e.value.cause, e.value.channels


def test_the_item_round_trips_and_hashes():
    ep, hst = open_bench()
    cfg = config.ProbeConfig(hst)
    assert config.ITEM["disable"] == 0x07 and config.item(config.Disable(channel=40)) == bytes([0x07, 2, 0, 40, 0])
    h1 = cfg.set([config.Disable(channel=41), config.Disable(channel=40), config.Label(channel=40, text="NC")])
    items = cfg.items()
    assert items == [config.Label(channel=40, text="NC"), config.Disable(channel=40), config.Disable(channel=41)]
    assert h1 == config.hash_of(items) == cfg.get()[0]
    assert config.decode(0x07, struct.pack("<H", 9)) == config.Disable(channel=9)
    assert ep.disabled == {40, 41}
    assert cfg.unset([("disable", 41)]) == config.hash_of(items[:2])
    assert ep.requests[-1].payload == bytes([1, 3, 7, 41, 0])                 # len tag channel(u16)
    cfg.set([config.remove("disable", 40)])
    assert ep.disabled == set()


def test_a_plan_naming_a_disabled_channel_is_held_by_settings():
    ep, hst = open_bench()
    config.ProbeConfig(hst).set([config.Disable(channel=20)])
    with pytest.raises(h.Rejected) as e:
        hst.call(core.plan_fn(hst), core.OP_PLAN_APPLY, m.tlv(0x90, struct.pack("<HBH", 4, 1, 20)))
    assert _cause(e) == ("held_by_settings", [20])
    with pytest.raises(core.PinsTaken) as e2:                                 # the client names the setting
        core.plan_apply(hst, [(4, 1, 20)])
    assert "disabled" in e2.value.holders[0][1]
    with pytest.raises(h.Rejected) as e:                                      # a settings plan too
        config.ProbeConfig(hst).set([config.Plan(fn=5, role=1, channel=20)])
    assert _cause(e) == ("held_by_settings", [20])
    with pytest.raises(h.Rejected) as e:                                      # disable and the plan in one set
        config.ProbeConfig(hst).set([config.Disable(channel=21), config.Plan(fn=5, role=1, channel=21)])
    assert _cause(e) == ("held_by_settings", [21]) and ep.disabled == {20}   # nothing changed


def test_a_slot_or_an_attach_naming_a_disabled_channel():
    ep, hst = open_bench()
    cfg = config.ProbeConfig(hst)
    cfg.set([config.Disable(channel=5)])
    with pytest.raises(h.Rejected) as e:
        cfg.set([config.Slot(slot=0, wire_fn=1, pins=(4, 5), name="b")])
    assert _cause(e) == ("held_by_settings", [5])
    wire = riscv.Wire(hst, "oep.wire.rvswd")
    with pytest.raises(h.Rejected) as e:
        wire.attach(pins=(4, 5))
    assert _cause(e) == ("held_by_settings", [5])
    conn, _ = wire.attach(pins=(2, 3))                                        # another pair: as before
    assert conn in ep.conns


def test_an_attach_reset_on_a_disabled_channel():
    ep = endpoint.Endpoint(fake.esp32_v003(), Clock())
    hst = h.Host(lambda b: ep.handle(b, 0))
    hst.open(3000)
    config.ProbeConfig(hst).set([config.Disable(channel=23)])
    with pytest.raises(h.Rejected) as e:
        riscv.Wire(hst, "oep.wire.swio").attach_under_reset(23)
    assert _cause(e) == ("held_by_settings", [23])


def test_scan_refuses_a_named_pair_and_count_0_skips_it():
    ep, hst = open_bench()
    config.ProbeConfig(hst).set([config.Disable(channel=4)])
    wire = riscv.Wire(hst, "oep.wire.rvswd")
    with pytest.raises(h.Rejected) as e:
        wire.scan([(4, 5)])
    assert _cause(e) == ("held_by_settings", [4])
    r = hst.call(1, 0x01, b"\x00")                                            # count 0: the list without (4, 5)
    assert r.payload[0] == 2
    assert {f.pins for f in wire.scan()} == {(2, 3), (6, 7)}


def test_gpio_set_and_read_on_a_disabled_channel():
    ep, hst = open_bench()
    config.ProbeConfig(hst).set([config.Disable(channel=30)])
    gpio = fixture.Gpio(hst, 4)
    for call in (lambda: gpio.set([(30, gpio.OUTPUT_HIGH)]), lambda: gpio.read([30])):
        with pytest.raises(h.Rejected) as e:
            call()
        assert _cause(e) == ("held_by_settings", [30])
        index = m.tlv(endpoint._GPIO.tlv["unavailable_payload"]["index"], b"\x00")
        assert e.value.result.payload.endswith(index)                        # the position in the list


def test_disabling_a_channel_in_use_is_pin_in_use():
    ep, hst = open_bench()
    cfg = config.ProbeConfig(hst)
    core.plan_apply(hst, [(4, 1, 30)])                                        # a session's plan
    with pytest.raises(h.Rejected) as e:
        cfg.set([config.Disable(channel=30)])
    assert _cause(e) == ("pin_in_use", [30])
    cfg.set([config.Plan(fn=5, role=1, channel=31)])                          # a settings plan
    with pytest.raises(h.Rejected) as e:
        cfg.set([config.Disable(channel=31)])
    assert _cause(e) == ("pin_in_use", [31])
    cfg.set([config.Slot(slot=0, wire_fn=1, pins=(6, 7), name="c")])          # a slot
    with pytest.raises(h.Rejected) as e:
        cfg.set([config.Disable(channel=7)])
    assert _cause(e) == ("pin_in_use", [7])
    riscv.Wire(hst, "oep.wire.rvswd").attach(pins=(2, 3))                     # a connection
    with pytest.raises(h.Rejected) as e:
        cfg.set([config.Disable(channel=2)])
    assert _cause(e) == ("pin_in_use", [2])
    assert ep.disabled == set()
    cfg.unset([("plan", 5)])                                                  # once free, it can be disabled
    cfg.set([config.Disable(channel=31)])
    assert ep.disabled == {31}


def test_idle_and_disable_on_one_channel_is_malformed():
    ep, hst = open_bench()
    cfg = config.ProbeConfig(hst)
    with pytest.raises(h.Rejected, match="malformed"):
        cfg.set([config.Idle(channel=40, mode="pull-up"), config.Disable(channel=40)])
    cfg.set([config.Idle(channel=40, mode="pull-up")])
    with pytest.raises(h.Rejected, match="malformed"):
        cfg.set([config.Disable(channel=40)])
    assert ep.disabled == set()


def test_unset_enables_it_again():
    ep, hst = open_bench()
    cfg = config.ProbeConfig(hst)
    cfg.set([config.Disable(channel=20)])
    assert ep.parked[20] == 0                                                 # parked at boot (before the set)
    ep.parked.pop(20, None)
    cfg.unset([("disable", 20)])
    assert ep.parked[20] == 0                                                 # a free pin again: Hi-Z
    core.plan_apply(hst, [(4, 1, 20)])
    assert (4, 1, 20) in ep.plan


def test_saved_disable_applies_at_boot_before_any_park():
    ep, hst = open_bench()
    cfg = config.ProbeConfig(hst)
    cfg.set([config.Disable(channel=20), config.Idle(channel=21, mode="pull-down")])
    cfg.save()
    ep.reboot(0x4321)
    assert ep.disabled == {20}
    assert 20 not in ep.parked and ep.parked[21] == 2 and ep.parked[22] == 0  # never touched; idle; Hi-Z
    hst2 = h.Host(lambda b: ep.handle(b, 1))
    hst2.open(3000)
    with pytest.raises(h.Rejected) as e:
        core.plan_apply(hst2, [(4, 1, 20)])
    assert e.value.result.detail == m.UNAVAILABLE
    assert config.Disable(channel=20) in config.ProbeConfig(hst2).items()


def test_a_released_pin_is_parked_but_not_a_disabled_one():
    ep, hst = open_bench()
    cfg = config.ProbeConfig(hst)
    core.plan_apply(hst, [(4, 1, 30)])
    ep.parked.clear()
    core.plan_release(hst, [4])
    assert ep.parked == {30: 0}
    riscv.Wire(hst, "oep.wire.rvswd").attach(pins=(4, 5))
    cfg.set([config.Disable(channel=31)])
    ep.parked.clear()
    ep.lose_connections()
    assert ep.parked == {4: 0, 5: 0}
    ep.reboot(0x99)                                                           # not saved: gone with the reboot
    assert ep.disabled == set() and ep.parked[31] == 0


def test_describe_is_unchanged():
    ep, hst = open_bench()
    before = core.describe(hst, 4)
    config.ProbeConfig(hst).set([config.Disable(channel=30)])
    assert core.describe(hst, 4) == before


def test_the_disable_command(monkeypatch):
    ep, hst = open_bench()

    class FakeLink:
        def close(self):
            pass
    hst.link = FakeLink()
    hst.end()
    monkeypatch.setattr(cli.link, "open_host", lambda target: hst)
    assert cli.main(["config", "disable", "x", "40", "41", "--save"]) == 0
    assert ep.disabled == {40, 41} and ep.saved is not None
    assert cli.main(["config", "remove", "x", "disable", "41"]) == 0
    assert ep.disabled == {40}


def test_disabling_a_channel_the_firmware_does_not_declare_is_unsupported():
    ep, hst = open_bench()
    cfg = config.ProbeConfig(hst)
    for ch in (24, 60):                                                       # reserved on this probe; past its channels
        with pytest.raises(h.Unsupported) as e:
            cfg.set([config.Disable(channel=ch)])
        assert struct.pack("<H", ch) in e.value.result.payload
    assert ep.disabled == set()
