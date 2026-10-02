"""oep pins against the fake endpoint: a probe whose pins the host chooses (an ESP32-P4 shape) with a CH32V003-like
target outside it (the endpoint's gpio_world hook): power from channel 5, SWIO 19, NRST 4 (a weak pull-up), an idle-high
UART on 22 / 23, an output the app toggles on 21, a board pull-up on 7 and a line held low on 3."""

import random

import pytest

from oep_client import catalog, config, endpoint, fake, host, pins, targets
from oep_client.fixture import Gpio

PINS = [p for p in range(30) if p not in (24, 25)]
WIRE, DM, GPIO, CFG = 1, 2, 4, 10
TARGET_PINS = {4, 6, 19, 21, 22, 23}          # the target's pins: an unpowered target sinks the probe's pull-up
V003_ID = 0x00310510
OPTION_ON = 0x08F75AA5                        # RDPR a5, nRDPR 5a, USER f7 (RST_MODE 10), nUSER 08
OPTION_OFF = 0x00FF5AA5                       # USER ff: RST_MODE 11, PD7 is a GPIO


def profile() -> fake.FakeProbe:
    return fake.FakeProbe("p4-pins", 1024, [
        fake._core("3.0.0", "esp32p4", "30eda0ea068b", 30, [24, 25], "", {},
                   fake._transports([(fake.TRANSPORT["usb_serial_jtag"], 0xFF)])),
        fake.Offered(WIRE, 0, "oep.wire.swio", (catalog.role_channels(1, PINS), catalog.role_channels(3, PINS),
                                                catalog.u8(fake.MAX_CONNECTIONS, 1))),
        fake.Offered(DM, 0, "oep.target.riscv-dm", (catalog.u32(catalog.FEATURES, 0b1111),
                                                    catalog.u16(catalog.MAX_LENGTH, fake.block_max_length(1024)))),
        fake.Offered(3, 0, "oep.target.console", (catalog.tlv(fake.MECHANISMS, bytes([0, 1, 2])),)),
        fake._gpio(GPIO, PINS),
        fake._config(CFG, 0, slots_max=2),
    ])


class World:
    """The lines outside the probe, as the fake's gpio reads them (endpoint.gpio_world)."""

    def __init__(self, ep, powered_by=5, active=True):
        self.ep, self.powered_by, self.active, self.reads = ep, powered_by, active, 0

    def powered(self) -> bool:
        return self.powered_by is None or self.ep.gpio_modes.get(self.powered_by) == Gpio.OUTPUT_HIGH

    def in_reset(self) -> bool:
        return self.ep.gpio_modes.get(4) in (Gpio.OPEN_DRAIN_LOW, Gpio.INPUT_PULLDOWN)

    def __call__(self, ch: int, mode: int) -> int:
        pull = {Gpio.INPUT_PULLUP: 1, Gpio.INPUT_PULLDOWN: 0, Gpio.INPUT_PULLUP_PULLDOWN: 0}.get(mode, 0)
        if ch == 7:
            return 1                                   # the probe board's own strong pull-up
        if ch == 3:
            return 0                                   # held low on the board
        if not self.powered():
            return 0 if ch in TARGET_PINS else pull
        if ch in (22, 23):
            return 1                                   # the idle-high UART: push-pull
        if ch == 4:
            return 0 if mode == Gpio.INPUT_PULLDOWN else 1   # the reset line's weak pull-up
        if ch == 21 and self.active and not self.in_reset():
            self.reads += 1
            return self.reads & 1                      # the app's output, toggling
        return pull


class Time:
    def __init__(self):
        self.s = 0.0

    def clock(self) -> float:
        return self.s

    def sleep(self, s: float) -> None:
        self.s += s


def bench(option=OPTION_ON, powered_by=5, active=True):
    t = Time()
    ep = endpoint.Endpoint(profile(), lambda: int(t.s * 1000))
    ep.gpio_world = World(ep, powered_by, active)
    ep.targets[(WIRE, (0, 0xFFFF))].present = False     # the fake's default target pair: nothing there
    ep.targets[(WIRE, (19, 0xFFFF))] = endpoint.FakeTarget(target_id=V003_ID, reset_line=4,
                                                         mem={targets.V00X_OPTION: option})
    hst = host.Host(ep.handle, rng=random.Random(1))
    hst.open(lease_ms=60000)
    return ep, hst, t


def finder(hst, t, lines, **kw) -> pins.PinFinder:
    return pins.PinFinder(hst, say=lines.append, sleep=t.sleep, clock=t.clock, probe="P", **kw)


def kinds(report) -> dict[str, list[int]]:
    out: dict[str, list[int]] = {}
    for c in report.channels:
        out.setdefault(c.kind, []).append(c.channel)
    return out


def test_finds_the_swio_pin_and_the_reset_line_without_being_told():
    ep, hst, t = bench()
    lines = []
    r = finder(hst, t, lines, power=5).run()
    k = kinds(r)
    assert k[pins.DRIVEN_HIGH] == [7, 22, 23] and k[pins.DRIVEN_LOW] == [3] and k[pins.ACTIVE] == [21]
    assert k[pins.PULLED_UP] == [4] and 19 in k[pins.FLOATING]
    assert set(r.follows_power) == TARGET_PINS
    assert r.stopped == [4]                                   # held low, the app stopped
    assert r.found == [(19, 0xFFFF)] and not {3, 5, 7, 21, 22, 23} & set(r.scanned)
    assert r.family == "ch32v00x" and r.target_id.startswith("00310510") and r.nrst_enabled is True
    assert r.reset_channel == 4 and r.reset_dpc == 0
    assert "--wire swio --pins 19" in r.slot and not r.saved
    assert any("label" in line and " 4 " in line and ".nrst" in line for line in lines)   # the reset line as a label
    assert any(".power_hi" in line for line in lines) and any("output-high" in line for line in lines)
    assert r.elapsed_s < 60


def test_safety_never_drives_a_driven_or_active_channel_and_releases_every_plan():
    ep, hst, t = bench()
    finder(hst, t, [], power=5).run()
    drives = {Gpio.OUTPUT_LOW, Gpio.OUTPUT_HIGH, Gpio.OPEN_DRAIN_LOW}
    assert not [(c, m) for c, m in ep.gpio_log if c in (3, 7, 21, 22, 23) and m in drives]
    assert {c for c, m in ep.gpio_log if m in (Gpio.OUTPUT_LOW, Gpio.OUTPUT_HIGH)} == {5}   # only the power
    assert not ep.plan                                        # every plan released
    assert not ep.conns                                       # every connection closed
    assert not config.ProbeConfig(hst).items()                # nothing written


def test_without_power_the_power_channel_is_never_touched():
    ep, hst, t = bench(powered_by=None)                       # powered from elsewhere
    r = finder(hst, t, [], exclude=[5]).run()
    assert not [c for c, _ in ep.gpio_log if c == 5]
    assert r.found == [(19, 0xFFFF)] and r.reset_channel == 4 and r.follows_power == []


def test_option_bytes_without_nrst_skip_the_confirm():
    ep, hst, t = bench(option=OPTION_OFF)
    lines = []
    r = finder(hst, t, lines, power=5).run()
    assert r.nrst_enabled is False and "RST_MODE 11" in r.nrst and r.reset_channel is None
    assert any("none to find" in line for line in lines)


def test_nothing_active_the_attach_under_reset_decides():
    ep, hst, t = bench(active=False)
    lines = []
    r = finder(hst, t, lines, power=5).run()
    assert pins.ACTIVE not in kinds(r) and r.stopped == []
    assert r.reset_channel == 4 and r.reset_how == "dpc"
    assert any("attach under reset alone decides" in line for line in lines)


def test_save_writes_the_slot_and_the_lines_as_labels():
    ep, hst, t = bench()
    r = finder(hst, t, [], power=5, save=True, slot=1, name="v003").run()
    assert r.saved and "reset" not in r.slot                   # a slot has no reset field: the line is a label (§1.3)
    items = config.ProbeConfig(hst).items()
    slots = [it for it in items if isinstance(it, config.Slot)]
    assert [(s.slot, s.name, s.wire_fn, s.pins) for s in slots] == [(1, "v003", WIRE, (19, 0xFFFF))]
    labels = {(it.channel, it.text) for it in items if isinstance(it, config.Label)}
    assert labels == {(4, "v003.nrst"), (5, "v003.power_hi")}
    assert config.find_line(items, "v003", "nrst") == 4


def test_steps_are_optional():
    ep, hst, t = bench()
    r = finder(hst, t, [], power=5).run(steps=("classify",))
    assert r.channels and r.found == [] and r.reset_channel is None and not ep.plan
    with pytest.raises(SystemExit):
        finder(hst, t, [], power=5).run(steps=("bogus",))


@pytest.mark.parametrize("pu, pd, both, kind", [
    ([1] * 4, [0] * 4, [0] * 4, pins.FLOATING), ([1] * 4, [0] * 4, [1] * 4, pins.PULLED_UP),
    ([1] * 4, [1] * 4, [1] * 4, pins.DRIVEN_HIGH), ([0] * 4, [0] * 4, [0] * 4, pins.DRIVEN_LOW),
    ([1, 0, 1, 1], [0] * 4, [0] * 4, pins.ACTIVE), ([1] * 4, [0] * 4, None, pins.FLOATING),
])
def test_classify(pu, pd, both, kind):
    assert pins.classify(pu, pd, both)[0] == kind


def test_ranges():
    assert pins.ranges([0, 1, 2, 5, 7, 8]) == "0-2,5,7-8" and pins.ranges([]) == "-"


def test_target_table():
    fam = targets.identify((1, V003_ID.to_bytes(4, "little")))
    assert fam.name == "ch32v00x" and fam.wire == "swio" and fam.reset_vector == 0
    assert targets.identify((1, (0x035E0601).to_bytes(4, "little"))).name == "ch32x035"
    assert targets.identify((1, b"\x00\x00\x00\x11")) is None and targets.identify(None) is None
    st = targets.v00x_nrst_from_word(OPTION_ON)
    assert st.enabled and "12 ms" in st.detail
    assert not targets.v00x_nrst_from_word(OPTION_OFF).enabled
    assert not targets.v00x_nrst_from_word(0x00F75AA5).enabled        # USER / nUSER not complements


def test_suggested_labels_are_checked_by_the_line_lookup():
    """The suggested <slot>.nrst / .power_hi are looked up by config.find_line (probe.config §1.3) in the settings as
    they would be: another channel already named so (any case) would make the line not found - said in a note."""
    ep, hst, t = bench()
    r = finder(hst, t, [], power=5).run()
    assert not any("would find no" in n for n in r.notes)        # nothing else named so: found as suggested
    ep, hst, t = bench()
    config.ProbeConfig(hst).set([config.Label(channel=9, text="CH32V00X.NRST"), config.Label(channel=4, text="ch32v00x.nrst")])
    r = finder(hst, t, [], power=5).run()
    notes = [n for n in r.notes if "would find no" in n]
    assert len(notes) == 1 and "ch32v00x.nrst" in notes[0] and "[9]" in notes[0]   # 4 itself is relabelled: no clash
