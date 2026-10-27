"""An analog stimulus a probe's settings allow: a line the probe drives and an ADC channel that follows it, both named by
their labels (core §13.8, host guide §5.3) - the drive `<port>.stimulus`, the ADC input `<port>.stimulus_in` - and
checked on the wiring of the day (the line low, then high: the input near its range's bottom, then clear of it). Nothing
is chosen that the labels do not name: a channel wired to another bench's target (its power, its UART) is not the
tests' to drive or to turn into an ADC input, whatever it would do.

    s = stimulus.find(hst)          # None: no labelled pair, or it does not follow
    if s: s.drive_to(gpio, True)    # the line up: the ADC channel s.analog rises from s.low to s.high

What a stimulus is for: an analog trigger crossing a level between s.low and s.high, a value near each end. Not for the
values' accuracy (no reference). Every plan is released at the end, so the line goes back to its idle (the settings')
whatever happened.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from . import capture, catalog, core, registry as reg, settings
from .fixture import Gpio

IDLE = "channel.{}.idle"
DRIVE_NAME, INPUT_NAME = "stimulus", "stimulus_in"   # the lines' names in the labels (`<port>.<name>`)
LINE = reg.FIXTURE_GPIO.enum["role"]["line"]
ROLE_CHANNELS = catalog.ROLE_CHANNELS
RATE, SAMPLES = 20000, 200          # 10 ms a look
SETTLE_S = 0.3                      # a target powered up, its lines settled


@dataclass(frozen=True)
class Stimulus:
    drive: int                      # the channel the gpio fixture moves (its idle an output)
    analog: int                     # the ADC channel that follows it
    rest_high: bool                 # the idle's level (the line goes back to it)
    low: int                        # the analog values seen with the line low ...
    high: int                       # ... and high (the means, raw)
    low_max: int                    # the highest value with the line low
    high_min: int                   # the lowest with it high

    def level(self) -> int:
        """A level a crossing passes through for sure: halfway between the two ends seen."""
        return (self.low_max + self.high_min) // 2

    def drive_to(self, gpio: Gpio, high: bool) -> None:
        gpio.set([(self.drive, Gpio.OUTPUT_HIGH if high else Gpio.OUTPUT_LOW)])


def idle_outputs(hst) -> list[tuple[int, bool]]:
    """The channels whose idle is an output now, as (channel, high). A probe without the settings ops: none."""
    if not core.offers(hst, 0, settings.OP["get"]):
        return []
    st = settings.Settings(hst)
    cat = st.catalog()
    out = []
    for v in st.get()[1]:
        it = cat.item(v.fn, v.number)
        if it is None or v.fn != 0 or it.name != IDLE:
            continue
        mode = cat.to_json(it, v.data)
        if mode in ("high", "low"):
            out.append((v.key, mode == "high"))
    return out


def labelled(hst, name: str) -> list[int]:
    """The channels whose label names line `name` of any port (`<port>.<name>`, ASCII case ignored)."""
    out = []
    for ch, text in settings.labels(hst):
        pl = settings.port_line(text)
        if pl and settings.fold_name(pl[1]) == settings.fold_name(name):
            out.append(ch)
    return out


def analog_channels(hst, fn: int) -> list[int]:
    """The channels the analog capture's role 0 allows (describe role_channels)."""
    out: list[int] = []
    for tag, v in core.describe(hst, fn):
        if tag == ROLE_CHANNELS and len(v) >= 3 and v[0] == 0:
            out += catalog.bitmap_to_channels(int.from_bytes(v[1:3], "little"), v[3:])
    return out


def judge(low: list[int], high: list[int], full: int) -> bool:
    """The channel follows the line: the two looks apart (every value with the line high above every one with it low)
    by at least half the converter's range."""
    return bool(low and high) and min(high) > max(low) and sum(high) / len(high) - sum(low) / len(low) >= full / 2


def _look(ana: capture.AnalogCapture) -> list[int]:
    ana.configure(rate=RATE, samples=SAMPLES)
    ana.start()
    seg = ana.wait(timeout=5)[0]
    return ana.values(ana.read_segment(seg), 0, seg.samples)


def find(hst, exclude=(), full: int = 4095) -> Stimulus | None:
    """The labelled (drive, analog) pair that follows on today's wiring, or None. Needs the lock; releases every plan it
    made. `exclude`: channels not to read (a track's of the same run). `full`: the converter's top value (12 bits). The
    drive's rest level is its idle when it has an output one, else low."""
    allowed = set(labelled(hst, DRIVE_NAME))
    inputs = [ch for ch in labelled(hst, INPUT_NAME) if ch not in allowed and ch not in set(exclude)]
    if not allowed or not inputs:
        return None
    idle = dict(idle_outputs(hst))
    drives = [(ch, idle.get(ch, False)) for ch in sorted(allowed)]
    gpio = Gpio(hst)
    ana = capture.AnalogCapture(hst)
    adc = set(analog_channels(hst, ana.fn))
    try:
        for drive, rest_high in drives:
            for ch in inputs:
                if ch not in adc:
                    continue
                try:
                    core.plan_apply(hst, [(gpio.fn, LINE, drive), (ana.fn, 0, ch)])
                except Exception:              # noqa: BLE001 - a channel something else holds: not this one
                    continue
                looks = []
                for high in (False, True):
                    gpio.set([(drive, Gpio.OUTPUT_HIGH if high else Gpio.OUTPUT_LOW)])
                    time.sleep(SETTLE_S)
                    looks.append(_look(ana))
                gpio.set([(drive, Gpio.OUTPUT_HIGH if rest_high else Gpio.OUTPUT_LOW)])
                low, high = looks
                if judge(low, high, full):
                    return Stimulus(drive, ch, rest_high, sum(low) // len(low), sum(high) // len(high), max(low),
                                    min(high))
        return None
    finally:
        core.plan_release(hst, [gpio.fn, ana.fn])
