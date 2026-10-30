"""oep pins against the virtual bench endpoint: a probe whose pins the host chooses (an ESP32-P4 shape) with a CH32V003-like
target outside it (the endpoint's gpio_world hook): power from channel 5, SWIO 19, NRST 4 (a weak pull-up), an idle-high
UART on 22 / 23, an output the app toggles on 21, a board pull-up on 7 and a line held low on 3."""

import random

import pytest

from oep_client import catalog, endpoint, settings, virtual_bench, host, pins, targets
from oep_client.fixture import Gpio

PINS = [p for p in range(30) if p not in (24, 25)]
WIRE, DM, GPIO = 1, 2, 4
TARGET_PINS = {4, 6, 19, 21, 22, 23}          # the target's pins: an unpowered target sinks the probe's pull-up
V003_ID = 0x00310510
OPTION_ON = 0x08F75AA5                        # RDPR a5, nRDPR 5a, USER f7 (RST_MODE 10), nUSER 08
OPTION_OFF = 0x00FF5AA5                       # USER ff: RST_MODE 11, PD7 is a GPIO


def profile() -> virtual_bench.VirtualProbe:
    return virtual_bench.VirtualProbe("p4-pins", 1024, virtual_bench.with_settings([
        virtual_bench._core("3.0.0", "esp32p4", "30eda0ea068b", 30,
                   virtual_bench._transports([(virtual_bench.TRANSPORT["usb_serial_jtag"], 0xFF)])),
        virtual_bench.Offered(WIRE, 0, "oep.wire.swio", (catalog.u32(catalog.FEATURES, 1), catalog.role_channels(1, PINS), catalog.role_channels(3, PINS),
                                                catalog.u8(virtual_bench.MAX_CONNECTIONS, 1))),
        virtual_bench.Offered(DM, 0, "oep.target.riscv-dm", (catalog.u32(catalog.FEATURES, 0b1111),
                                                    catalog.u16(catalog.MAX_LENGTH, virtual_bench.block_max_length(1024)))),
        virtual_bench._console(3),
        virtual_bench._gpio(GPIO, PINS),
    ], storage=True), own_channels=(24, 25))


class World:
    """The lines outside the probe, as the virtual bench's gpio reads them (endpoint.gpio_world)."""

    def __init__(self, ep, powered_by=5, active=True):
        self.ep, self.powered_by, self.active, self.reads = ep, powered_by, active, 0

    def powered(self) -> bool:
        return self.powered_by is None or self.ep.gpio_modes.get(self.powered_by) == Gpio.OUTPUT_HIGH

    def in_reset(self) -> bool:
        return self.ep.gpio_modes.get(4) in (Gpio.OPEN_DRAIN_LOW, Gpio.INPUT_PULLDOWN)

    def __call__(self, ch: int, mode: int) -> int:
        pull = {Gpio.INPUT_PULLUP: 1, Gpio.INPUT_PULLDOWN: 0}.get(mode, 0)
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
    ep.targets[(WIRE, (0, 0xFFFF))].present = False     # the virtual bench's default target pair: nothing there
    ep.targets[(WIRE, (19, 0xFFFF))] = endpoint.VirtualTarget(target_id=V003_ID, reset_line=4,
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


def test_multiple_candidates_are_all_preserved_without_attach_or_save(monkeypatch):
    ep, hst, t = bench(powered_by=None)
    ep.targets[(WIRE, (18, 0xFFFF))] = endpoint.VirtualTarget(target_id=V003_ID)
    f = finder(hst, t, [], save=True)
    monkeypatch.setattr(f.wire, 'attach', lambda *a, **kw: pytest.fail('attached ambiguous candidate'))
    report = f.run(steps=('classify', 'scan', 'identify', 'reset', 'labels'))
    assert set(report.found) == {(18, 0xFFFF), (19, 0xFFFF)}
    assert report.resolution == 'ambiguous' and f.pins is None
    assert not report.saved and not report.profile and not ep.plan
    assert report.target_id is None


def test_a_scan_goes_the_nearest_first_and_says_when_the_time_ran_out(monkeypatch):
    ep, hst, t = bench(powered_by=None)
    f = finder(hst, t, [], budget_s=30)
    f.report.wire = 'rvswd'
    f.kinds = {c: pins.Channel(c, pins.FLOATING, '1', '0') for c in range(30)}
    monkeypatch.setattr(pins, 'role_channels', lambda *a: list(range(30)))
    tried = []

    def scan(pairs, *a):
        tried.extend(pairs)
        t.sleep(0.1 * len(pairs))                        # about 0.1 s a pair, as rvswd on a P4
        return []
    monkeypatch.setattr(f.wire, 'scan', scan)
    assert f.scan() == []
    gaps = [abs(d - c) for d, c in tried]
    assert gaps == sorted(gaps) and gaps[0] == 1         # side by side first, then further apart
    assert len(tried) < 30 * 29 and f.report.resolution == 'not-finished' and f.pins is None
    assert any("time ran out" in n for n in f.report.notes)


def test_a_scan_stops_at_the_first_answer_unless_every_pair_is_asked_for(monkeypatch):
    from oep_client import riscv
    for all_pairs, resolution in ((False, 'unique-in-partial'), (True, 'unique-candidate')):
        ep, hst, t = bench(powered_by=None)
        f = finder(hst, t, [], budget_s=600, all_pairs=all_pairs)
        f.report.wire = 'rvswd'
        f.kinds = {c: pins.Channel(c, pins.FLOATING, '1', '0') for c in range(30)}
        monkeypatch.setattr(pins, 'role_channels', lambda *a: list(range(30)))
        monkeypatch.setattr(f.wire, 'scan', lambda pairs, *a: [riscv.Found(1, p, 2) for p in pairs if p == (15, 14)])
        f.scan()
        assert f.pins == (15, 14) and f.report.resolution == resolution


def test_finds_the_swio_pin_and_the_reset_line_without_being_told():
    ep, hst, t = bench()
    lines = []
    r = finder(hst, t, lines, power=5, name="v003").run()
    k = kinds(r)
    assert k[pins.DRIVEN_HIGH] == [7, 22, 23] and k[pins.DRIVEN_LOW] == [3] and k[pins.ACTIVE] == [21]
    assert {4, 19} <= set(k[pins.FLOATING])                   # a weak pull-up reads as floating (host guide §19.1)
    assert not hasattr(pins, "PULLED_UP") and pins.KINDS == (pins.FLOATING, pins.DRIVEN_HIGH, pins.DRIVEN_LOW,
                                                              pins.ACTIVE)
    assert set(r.follows_power) == TARGET_PINS
    assert r.stopped == [4]                                   # held low, the app stopped
    assert r.found == [(19, 0xFFFF)] and not {3, 5, 7, 21, 22, 23} & set(r.scanned)
    assert r.family == "ch32v00x" and r.target_id.startswith("00310510") and r.nrst_enabled is True
    assert r.reset_channel == 4 and r.reset_dpc == 0
    assert "wire swio, pins 19" in r.profile and not r.saved
    assert any("label" in line and "channel.GPIO4.label=v003.nrst" in line for line in lines)   # the reset line as a label (host guide §5.3)
    assert any(".label=v003.power_hi" in line for line in lines) and any(".idle=high" in line for line in lines)
    assert r.elapsed_s < 60


def test_safety_never_drives_a_driven_or_active_channel_and_releases_every_plan():
    ep, hst, t = bench()
    finder(hst, t, [], power=5).run()
    drives = {Gpio.OUTPUT_LOW, Gpio.OUTPUT_HIGH, Gpio.OPEN_DRAIN_LOW}
    assert not [(c, m) for c, m in ep.gpio_log if c in (3, 7, 21, 22, 23) and m in drives]
    driven = {c for c, m in ep.gpio_log if m in (Gpio.OUTPUT_LOW, Gpio.OUTPUT_HIGH)}
    assert 5 in driven and not driven & {3, 4, 7, 19, 21, 22, 23}   # the map drives floating channels only: never a
                                                                    # driven / active one, the reset line or the SWIO
    assert not ep.plan                                        # every plan released
    assert not ep.conns                                       # every connection closed
    assert not settings.labels(hst)                           # nothing written


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


def test_save_writes_the_lines_as_labels():
    """The probe keeps no wiring (core §13): --save writes the labels only; with a profile name
    `<name>.swio` on the debug pin, `<name>.nrst` / `<name>.power_hi` (host guide §5.3)."""
    ep, hst, t = bench()
    r = finder(hst, t, [], power=5, save=True, name="v003").run()
    assert r.saved and "wire swio" in r.profile
    labels = settings.labels(hst)
    assert set(labels) == {(19, "v003.swio"), (4, "v003.nrst"), (5, "v003.power_hi")}
    assert settings.find_line(labels, "nrst", "v003") == 4


def test_steps_are_optional():
    ep, hst, t = bench()
    r = finder(hst, t, [], power=5).run(steps=("classify",))
    assert r.channels and r.found == [] and r.reset_channel is None and not ep.plan
    with pytest.raises(SystemExit):
        finder(hst, t, [], power=5).run(steps=("bogus",))


@pytest.mark.parametrize("pu, pd, kind", [
    ([1] * 4, [0] * 4, pins.FLOATING), ([1] * 4, [1] * 4, pins.DRIVEN_HIGH), ([0] * 4, [0] * 4, pins.DRIVEN_LOW),
    ([1, 0, 1, 1], [0] * 4, pins.ACTIVE), ([1] * 4, [0, 1, 0, 0], pins.ACTIVE), ([0] * 4, [1] * 4, pins.ACTIVE),
])
def test_classify(pu, pd, kind):
    """Host guide §19.1: reads under pull-up, then pull-down - no both-pulls mode; a weak pull-up is floating."""
    assert pins.classify(pu, pd)[0] == kind


def test_classify_never_uses_a_both_pulls_mode():
    """gpio mode 7 is gone (oep-if-fixture gpio: modes 0-6): the classify reads never ask for it."""
    ep, hst, t = bench()
    finder(hst, t, [], power=5).run(steps=("classify",))
    assert ep.gpio_log and all(m <= 6 for _, m in ep.gpio_log)
    assert not hasattr(Gpio, "INPUT_PULLUP_PULLDOWN")


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
    """The suggested nrst / power_hi are looked up by settings.find_line (host guide §5.3) in the settings as they would
    be: another channel already named so (any case) would make the line not found - said in a note."""
    ep, hst, t = bench()
    r = finder(hst, t, [], power=5, name="v003").run()
    assert not any("would find no" in n for n in r.notes)        # nothing else named so: found as suggested
    ep, hst, t = bench()
    settings.Settings(hst).set_named(settings.CORE_CATEGORY, {"channel.GPIO9.label": "V003.NRST", "channel.GPIO4.label": "x"})
    r = finder(hst, t, [], power=5, name="v003").run()
    notes = [n for n in r.notes if "would find no" in n]
    assert len(notes) == 1 and "nrst" in notes[0] and "[9]" in notes[0]   # 4 itself is relabelled: no clash


class Pads(dict):
    """The target's memory with INDR worked out from the probe's lines: pad -> channel (a CH32V00x's GPIO block)."""

    def __init__(self, ep, wired: dict[str, int], coupled: dict[str, int] | None = None, **words):
        super().__init__(**words)
        self.ep, self.wired, self.coupled = ep, wired, coupled or {}

    def level(self, pad: str, ch: int) -> int:
        modes = self.ep.gpio_modes
        if modes.get(ch) in (Gpio.OUTPUT_HIGH, Gpio.OUTPUT_LOW):
            return int(modes[ch] == Gpio.OUTPUT_HIGH)
        # nothing drives the line: it keeps what a neighbour's edge coupled into it
        return int(pad in self.coupled and modes.get(self.coupled[pad]) == Gpio.OUTPUT_HIGH)

    def get(self, address, default=0):
        for port in "ACD":
            if address == targets.gpio_base(port) + targets.GPIO_INDR:
                return sum(self.level(pad, ch) << int(pad[2:]) for pad, ch in self.wired.items() if pad[1] == port)
        return super().get(address, default)


def test_map_names_the_pad_each_channel_reaches_and_puts_the_target_back():
    ep, hst, t = bench(powered_by=None)
    tg = ep.targets[(WIRE, (19, 0xFFFF))]
    tg.mem = Pads(ep, {"PC3": 6, "PD4": 9, "PA2": 11, "PC5": 11}, **{})
    tg.mem.update({targets.V00X_OPTION: OPTION_ON, targets.RCC_APB2PCENR: 0x21,
                   targets.gpio_base("C"): 0x88884444})
    r = finder(hst, t, []).run()
    assert r.wiring == {"PA2": [11], "PC3": [6], "PC5": [11], "PD4": [9]}
    assert tg.mem.get(targets.RCC_APB2PCENR) == 0x21                   # the clocks as they were
    assert tg.mem.get(targets.gpio_base("C")) == 0x88884444            # the pads' configuration as it was
    assert not tg.halted and not ep.plan and not ep.conns


def test_explicit_pairs_are_tried_whatever_classify_read():
    ep, hst, t = bench(powered_by=None)
    r = finder(hst, t, [], pairs=[(19, 0xFFFF)]).run(("scan", "identify"))
    assert r.found == [(19, 0xFFFF)] and r.family == "ch32v00x"


def test_map_holds_the_others_low_when_a_pad_seems_to_follow_two_channels():
    ep, hst, t = bench(powered_by=None)
    tg = ep.targets[(WIRE, (19, 0xFFFF))]
    tg.mem = Pads(ep, {"PC3": 9, "PC4": 10}, coupled={"PC3": 10})   # 10's edges couple into the floating PC3 line
    tg.mem.update({targets.V00X_OPTION: OPTION_ON})
    lines = []
    r = finder(hst, t, lines).run()
    assert r.wiring == {"PC3": [9], "PC4": [10]}
    assert any("only 9 moves it" in line for line in lines)


def test_a_driven_low_channel_is_tried_as_swdio_after_the_floating_pairs_beside_it(monkeypatch):
    from oep_client import riscv
    ep, hst, t = bench(powered_by=None)
    f = finder(hst, t, [], budget_s=600)
    f.report.wire = 'rvswd'
    f.kinds = {c: pins.Channel(c, pins.FLOATING, '1', '0') for c in range(30)}
    f.kinds[15] = pins.Channel(15, pins.DRIVEN_LOW, '0', '0')
    f.kinds[7] = pins.Channel(7, pins.DRIVEN_HIGH, '1', '1')
    monkeypatch.setattr(pins, 'role_channels', lambda *a: list(range(30)))
    tried = []
    monkeypatch.setattr(f.wire, 'scan', lambda pairs, *a: tried.extend(pairs) or
                        [riscv.Found(1, p, 2) for p in pairs if p == (15, 14)])
    f.scan()
    assert f.pins == (15, 14) and any("held its debug line" in n for n in f.report.notes)
    assert not [pr for pr in tried if 7 in pr or pr[1] == 15]    # never driven-high; driven-low only as SWDIO
    assert tried.index((15, 14)) > tried.index((13, 14))         # after the floating pairs one apart
