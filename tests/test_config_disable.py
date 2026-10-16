"""oep.probe.config item disable (0x07, probe.config §1): a channel the probe never uses or touches. Any request naming
it is rejected unavailable cause 5 (held by settings) with the channel; the probe never parks it; disabling a channel
in use is cause 1; idle and disable on one channel is unavailable cause 5."""

import struct

import pytest

from oep_client import __main__ as cli, config, core, endpoint, virtual_bench, fixture, host as h, message as m, riscv

from test_config import Clock, open_bench


def _cause(e) -> tuple[str | None, list[int]]:
    """The unavailable payload: cause, channel, fn (core §4.3; no holder_fn / holder_kind)."""
    assert isinstance(e.value, h.Unavailable), e.value
    assert not hasattr(e.value, "holder_kind") and not hasattr(e.value, "holder_fn")
    return e.value.cause, e.value.channels


def test_the_item_round_trips_and_moves_the_generation():
    ep, hst = open_bench()
    cfg = config.ProbeConfig(hst)
    assert config.ITEM["disable"] == 3 and config.item(config.Disable(channel=40)) == bytes([3, 2, 0, 40, 0])   # number len key, no data
    g1 = cfg.set([config.Disable(channel=41), config.Disable(channel=40), config.Label(channel=40, text="NC")])
    items = cfg.items()
    assert items == [config.Label(channel=40, text="NC"), config.Disable(channel=40), config.Disable(channel=41)]
    assert g1 == cfg.get()[0] == 2                                            # the change counter (probe.config §2)
    assert config.decode("disable", 9, b"") == config.Disable(channel=9)
    assert ep.disabled == {40, 41}
    g2 = cfg.unset([("disable", 41)])
    assert ep.requests[-1].payload == bytes([1, config.ITEM["disable"], 41, 0])   # count, then number key(u16)
    assert g2 == cfg.get()[0] == 3 and config.same_items(cfg.items(), items[:2])
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


def test_an_attach_naming_a_disabled_channel():
    ep, hst = open_bench()
    config.ProbeConfig(hst).set([config.Disable(channel=5)])
    wire = riscv.Wire(hst, "oep.wire.rvswd")
    with pytest.raises(h.Rejected) as e:
        wire.attach(pins=(4, 5))
    assert _cause(e) == ("held_by_settings", [5])
    conn, _ = wire.attach(pins=(2, 3))                                        # another pair: as before
    assert conn in ep.conns


def test_an_attach_reset_on_a_disabled_channel():
    ep = endpoint.Endpoint(virtual_bench.esp32_v003(), Clock())
    hst = h.Host(lambda b: ep.handle(b, 0))
    hst.open(3000)
    config.ProbeConfig(hst).set([config.Disable(channel=23)])
    with pytest.raises(h.Rejected) as e:
        riscv.Wire(hst, "oep.wire.swio").attach_under_reset(23)
    assert _cause(e) == ("held_by_settings", [23])


def test_scan_refuses_a_named_pair_with_a_disabled_channel():
    """debug §1: a disabled pin in any candidate refuses the whole scan (cause 5); the host leaves it out."""
    ep, hst = open_bench()
    config.ProbeConfig(hst).set([config.Disable(channel=4)])
    wire = riscv.Wire(hst, "oep.wire.rvswd")
    for pairs in ([(4, 5)], None):                                              # None: every candidate pair
        with pytest.raises(h.Rejected) as e:
            wire.scan(pairs)
        assert _cause(e) == ("held_by_settings", [4])
    assert {f.pins for f in wire.scan([(2, 3), (6, 7)])} == {(2, 3), (6, 7)}


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
    conn, _ = riscv.Wire(hst, "oep.wire.rvswd").attach(pins=(2, 3))          # a connection
    with pytest.raises(h.Rejected) as e:
        cfg.set([config.Disable(channel=2)])
    assert _cause(e) == ("pin_in_use", [2])
    assert ep.disabled == set()
    core.plan_release(hst, [4])                                               # once free, it can be disabled
    cfg.set([config.Disable(channel=30)])
    assert ep.disabled == {30}


def test_idle_and_disable_on_one_channel_is_held_by_settings():
    """probe.config §1: an idle and a disable on one channel after the change: unavailable cause 5 with the channel;
    the settings and the generation stay."""
    ep, hst = open_bench()
    cfg = config.ProbeConfig(hst)
    with pytest.raises(h.Rejected) as e:
        cfg.set([config.Idle(channel=40, mode="pull-up"), config.Disable(channel=40)])
    assert _cause(e) == ("held_by_settings", [40])
    cfg.set([config.Idle(channel=40, mode="pull-up")])
    with pytest.raises(h.Rejected) as e:
        cfg.set([config.Disable(channel=40)])
    assert _cause(e) == ("held_by_settings", [40])
    assert ep.disabled == set() and cfg.get()[0] == 2


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
        assert e.value.result.payload == bytes([config.ITEM["disable"]])     # the item's tag (probe.config §1)
    assert ep.disabled == set()
