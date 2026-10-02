"""Idle modes 3 / 4 (probe.config §1, oep-spec 5013ffb), the idle state on every release (core §8), a gpio line taken
by a plan keeping its idle level until the first set (fixture §1, 7b3c319), the boot order (idle before the at-boot
attach, probe.config §2), and find_line (host-development-guide §8.1) - against the fake probe."""

import pytest

from oep_client import __main__ as cli, config, core, endpoint, fake, fixture, host as h


class Clock:
    t = 0

    def __call__(self):
        return self.t


def open_bench():
    ep = endpoint.Endpoint(fake.p4_bench(), Clock())
    hst = h.Host(lambda b: ep.handle(b, 1))
    hst.open(3000)
    return ep, hst


def test_idle_modes_have_names_both_spellings():
    assert config.IDLE["output-low"] == 3 and config.IDLE["output-high"] == 4
    assert config.Idle(channel=7, mode="output_high") == config.Idle(channel=7, mode="output-high")
    assert config.Idle(channel=7, mode="output_low").value() == bytes([7, 0, 3])
    with pytest.raises(ValueError, match="output-high"):
        config.Idle(channel=7, mode="output-medium").value()
    assert config.decode(config.ITEM["idle"], bytes([7, 0, 4])) == config.Idle(channel=7, mode="output-high")


def test_output_idle_drives_and_survives_take_until_the_first_set():
    ep, hst = open_bench()
    cfg = config.ProbeConfig(hst)
    gpio_fn = ep.fns["oep.fixture.gpio"]
    cfg.set([config.Idle(channel=20, mode="output-high"), config.Idle(channel=21, mode="output_low")])
    assert ep.parked[20] == 4 and ep.parked[21] == 3                 # free: driven now
    gpio = fixture.Gpio(hst, gpio_fn)
    core.plan_apply(hst, [(gpio_fn, 1, 20), (gpio_fn, 1, 21)])
    assert gpio.read([20, 21]) == [1, 0]                              # taking it changes nothing (fixture §1)
    gpio.set([(20, gpio.OUTPUT_LOW)])
    assert gpio.read([20, 21]) == [0, 0]
    core.plan_release(hst, [gpio_fn])
    assert ep.parked[20] == 4                                         # released: to the output idle (core §8)
    core.plan_apply(hst, [(gpio_fn, 1, 20)])
    assert gpio.read([20]) == [1]                                     # taken again: the idle level


def test_replacing_and_lapsing_release_to_the_idle_state():
    ep, hst = open_bench()
    cfg = config.ProbeConfig(hst)
    gpio_fn = ep.fns["oep.fixture.gpio"]
    cfg.set([config.Idle(channel=22, mode="output-high")])
    gpio = fixture.Gpio(hst, gpio_fn)
    core.plan_apply(hst, [(gpio_fn, 1, 22)])
    gpio.set([(22, gpio.INPUT)])
    core.plan_apply(hst, [(gpio_fn, 1, 23)])                          # replaced: 22 released
    assert ep.parked[22] == 4 and 22 not in ep.gpio_modes
    core.plan_apply(hst, [(gpio_fn, 1, 22)])
    gpio.set([(22, gpio.OUTPUT_LOW)])
    ep.now = lambda: 10_000_000                                       # the lease lapses: the plan is swept (core §9)
    ep._lapse()
    assert not ep.plan and ep.parked[22] == 4


def test_output_idle_on_an_input_only_channel_is_unsupported():
    ep, hst = open_bench()
    ep.input_only = {24}
    cfg = config.ProbeConfig(hst)
    with pytest.raises(h.Rejected, match="unsupported"):
        cfg.set([config.Idle(channel=24, mode="output-high")])
    cfg.set([config.Idle(channel=24, mode="pull-up")])                # the input modes still are
    with pytest.raises(h.Rejected, match="malformed"):
        cfg.set([bytes([config.ITEM["idle"], 3, 24, 0, 5])])          # no mode 5


def test_boot_applies_idle_before_the_plan_and_the_at_boot_attach():
    ep, hst = open_bench()
    cfg = config.ProbeConfig(hst)
    pair = ep.pairs[1][0]
    gpio_fn = ep.fns["oep.fixture.gpio"]
    cfg.set([config.Idle(channel=20, mode="output-high"), config.Plan(fn=gpio_fn, role=1, channel=20),
             config.Slot(slot=0, wire_fn=1, pins=pair, name="x035", attach="at-boot"),
             config.Label(channel=20, text="power_hi")])
    cfg.save()
    seen = []
    real = ep._auto_attach

    def attach(n):
        seen.append(ep.parked.get(20))                                # the power line's state when the attach starts
        return real(n)
    ep._auto_attach = attach
    ep.reboot(0x5678)
    assert seen == [4]                                                # idle first (probe.config §2)
    h2 = h.Host(lambda b: ep.handle(b, 1))
    h2.open(3000)
    assert ep.gpio_modes.get(20) == 4                                 # the settings' gpio plan took it powered
    assert config.ProbeConfig(h2).state().slots[0].state == "connected"


def test_idle_from_the_command(capsys, monkeypatch):
    ep, hst = open_bench()

    class FakeLink:
        def close(self):
            pass
    hst.link = FakeLink()
    hst.end()
    monkeypatch.setattr(cli.link, "open_host", lambda target: hst)
    assert cli.main(["config", "idle", "x", "21", "output-high"]) == 0
    assert config.Idle(channel=21, mode="output-high") in config.ProbeConfig(hst).items()
    assert ep.parked[21] == 4


def L(channel, text):
    return config.Label(channel=channel, text=text)


def S(n, name):
    return config.Slot(slot=n, wire_fn=1, pins=(2, 3), name=name)


def test_find_line_slot_name_first_then_the_bare_name_on_one_slot():
    items = [S(0, "x035"), L(5, "nrst"), L(6, "x035.nrst"), L(7, "power_hi")]
    assert config.find_line(items, "x035", "nrst") == 6               # <slot>.<name> first (probe.config §1.3)
    assert config.find_line(items, None, "nrst") == 6                 # None: the one slot
    assert config.find_line(items, 0, "nrst") == 6                    # or its number
    assert config.find_line(items, "x035", "power_hi") == 7           # then the bare name (one slot)
    assert config.find_line(items, "x035", "power_lo") is None
    assert config.find_line([L(7, "power_lo")], None, "power_lo") == 7   # no slot item: the bare name
    with pytest.raises(LookupError, match="no slot 3"):
        config.find_line(items, 3, "nrst")


def test_find_line_ignores_ascii_case():
    items = [S(0, "x035"), L(6, "X035.NRST"), L(7, "Power_Hi")]
    assert config.find_line(items, "x035", "nrst") == 6
    assert config.find_line(items, "x035", "POWER_HI") == 7
    assert config.find_line([L(4, "nrst\u0130")], None, "nrst\u0069") is None   # only A-Z fold


def test_find_line_several_slots():
    items = [S(0, "a"), S(1, "b"), L(5, "a.nrst"), L(6, "b.nrst"), L(7, "power_hi")]
    assert config.find_line(items, "b", "nrst") == 6
    assert config.find_line(items, "a", "power_hi") is None           # no bare name with two or more slot items
    assert config.find_line(items, "c", "power_hi") is None
    with pytest.raises(ValueError, match="name the slot"):
        config.find_line(items, None, "nrst")


def test_find_line_ambiguous_is_none_without_falling_through():
    two = [S(0, "a"), L(5, "a.nrst"), L(6, "A.Nrst"), L(7, "nrst")]
    assert config.find_line(two, "a", "nrst") is None                 # two at the <slot>.<name> step: none (not 7)
    assert config.find_line([S(0, "a"), L(5, "nrst"), L(6, "NRST")], "a", "nrst") is None   # two bare ones
    assert config.find_line([L(5, "nrst"), L(6, "nrst")], None, "nrst") is None            # no slot items
    assert config.line_from_labels([(5, "a.nrst"), (6, "nrst")], 2, "a", "nrst") == 5
    assert config.line_from_labels([(6, "nrst")], 2, "a", "nrst") is None


def test_find_line_reads_the_probe():
    ep, hst = open_bench()
    config.ProbeConfig(hst).set([config.Label(channel=20, text="power_hi")])
    assert config.find_line(hst, None, "power_hi") == 20


def test_replacing_a_gpio_plan_keeps_the_channels_in_both():
    """A power line in both the old and the new plan keeps its drive (core §8; probe 0.0.27 blinked it off)."""
    ep, hst = open_bench()
    gpio_fn = ep.fns["oep.fixture.gpio"]
    config.ProbeConfig(hst).set([config.Idle(channel=23, mode="output-low")])
    gpio = fixture.Gpio(hst, gpio_fn)
    core.plan_apply(hst, [(gpio_fn, 1, 20), (gpio_fn, 1, 21)])
    gpio.set([(20, gpio.OUTPUT_HIGH), (21, gpio.OUTPUT_HIGH)])
    core.plan_apply(hst, [(gpio_fn, 1, 20), (gpio_fn, 1, 23)])
    assert gpio.read([20, 23]) == [1, 0]                              # 20 kept driving, 23 new in its idle
    assert 21 not in ep.gpio_modes and ep.parked[21] == 0            # left the plan: its idle state
