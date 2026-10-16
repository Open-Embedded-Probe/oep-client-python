"""Idle modes 3 / 4 (probe.config §1, oep-spec 5013ffb), the idle state on every release (core §8), a gpio line taken
by a plan keeping its idle level until the first set (fixture §1, 7b3c319), the boot order (idle before the at-boot
attach, probe.config §2), and find_line (host-development-guide §18.1) - against the virtual bench."""

import pytest

from oep_client import __main__ as cli, config, core, endpoint, virtual_bench, fixture, host as h, message as m


class Clock:
    t = 0

    def __call__(self):
        return self.t


def open_bench():
    ep = endpoint.Endpoint(virtual_bench.p4_bench(), Clock())
    hst = h.Host(lambda b: ep.handle(b, 1))
    hst.open(3000)
    return ep, hst


def test_idle_modes_have_names_both_spellings():
    assert config.IDLE["output-low"] == 3 and config.IDLE["output-high"] == 4
    assert config.Idle(channel=7, mode="output_high") == config.Idle(channel=7, mode="output-high")
    assert config.Idle(channel=7, mode="output_low").value() == bytes([7, 0, 3])          # channel mode (probe.config §1)
    with pytest.raises(ValueError, match="output-high"):
        config.Idle(channel=7, mode="output-medium").value()
    assert config.decode("idle", 7, bytes([4])) == config.Idle(channel=7, mode="output-high")


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
    ep.input_only = {26}
    cfg = config.ProbeConfig(hst)
    with pytest.raises(h.Unsupported) as e:
        cfg.set([config.Idle(channel=26, mode="output-high")])
    assert e.value.tag == config.ITEM["idle"]                          # the item's tag as received (probe.config §1)
    cfg.set([config.Idle(channel=26, mode="pull-up")])                # the input modes still are
    with pytest.raises(h.Unsupported) as e:
        cfg.set([m.tlv(config.ITEM["idle"], bytes([26, 0, 5]), critical=True)])         # mode 5: left unused
    assert e.value.tag == config.ITEM["idle"] | 0x80
    with pytest.raises(h.Unsupported) as e:                           # the same without bit 7 (core §2.3)
        cfg.set([m.tlv(config.ITEM["idle"], bytes([26, 0, 5]))])
    assert e.value.tag == config.ITEM["idle"]


def test_boot_applies_the_saved_idle_before_the_first_answer():
    """probe.config §3: at boot the saved disable and idle are applied before the probe answers; a gpio plan the next
    session makes takes the line in that state (fixture §1)."""
    ep, hst = open_bench()
    cfg = config.ProbeConfig(hst)
    gpio_fn = ep.fns["oep.fixture.gpio"]
    cfg.set([config.Idle(channel=20, mode="output-high"), config.Label(channel=20, text="power_hi")])
    cfg.save()
    ep.reboot(0x5678)
    assert ep.parked[20] == 4                                         # before any request
    h2 = h.Host(lambda b: ep.handle(b, 1))
    h2.open(3000)
    core.plan_apply(h2, [(gpio_fn, 1, 20)])
    assert fixture.Gpio(h2, gpio_fn).read([20]) == [1]                # taken powered


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


def test_find_line_profile_name_first_then_the_bare_name_for_one_target():
    items = [L(5, "nrst"), L(6, "x035.nrst"), L(7, "power_hi")]
    assert config.find_line(items, "nrst", "x035") == 6               # <profile>.<name> first (host guide §5.3)
    assert config.find_line(items, "nrst") == 5                       # one target, no profile: the bare name
    assert config.find_line(items, "power_hi", "x035") == 7           # then the bare name (one target)
    assert config.find_line(items, "power_hi", "x035", single=False) is None   # several targets: no bare name
    assert config.find_line(items, "power_lo", "x035") is None


def test_find_line_ignores_ascii_case():
    items = [L(6, "X035.NRST"), L(7, "Power_Hi")]
    assert config.find_line(items, "nrst", "x035") == 6
    assert config.find_line(items, "POWER_HI", "x035") == 7
    assert config.find_line([L(4, "nrst\u0130")], "nrst\u0069") is None   # only A-Z fold


def test_find_line_ambiguous_is_none_without_falling_through():
    two = [L(5, "a.nrst"), L(6, "A.Nrst"), L(7, "nrst")]
    assert config.find_line(two, "nrst", "a") is None                 # two at the <profile>.<name> step: none (not 7)
    assert config.find_line([L(5, "nrst"), L(6, "NRST")], "nrst", "a") is None   # two bare ones
    assert config.line_from_labels([(5, "a.nrst"), (6, "nrst")], "nrst", "a", single=False) == 5
    assert config.line_from_labels([(6, "nrst")], "nrst", "a", single=False) is None


def test_find_line_reads_the_probe():
    ep, hst = open_bench()
    config.ProbeConfig(hst).set([config.Label(channel=20, text="power_hi")])
    assert config.find_line(hst, "power_hi") == 20


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
