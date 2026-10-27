"""An analog stimulus on the wiring a probe has today, found rather than written down (the bench's wiring changes): a
line the probe already drives - a channel whose idle is an output in its settings (core §13.8: a target's power,
`<port>.power_hi`) - and a free ADC channel that follows it. Driving such a line low and high again is what the
probe's own settings do at every boot; nothing else is driven. Each ADC channel the analog capture allows (role 0) is
read with the line low and high: one that reads near its range's bottom, then high and clear of it, is the stimulus.

    s = stimulus.find(hst)          # None: nothing on this probe's wiring
    if s: s.drive(gpio, high=True)  # the line up: the ADC channel s.analog rises from s.low to s.high

What a stimulus is for: an analog trigger crossing a level between s.low and s.high, a value near each end. Not for the
values' accuracy (a target's output is no reference). Every plan is released at the end, so the line goes back to its
idle (the settings') whatever happened.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from . import capture, catalog, core, registry as reg, settings
from .fixture import Gpio

IDLE = "channel.{}.idle"
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
    """The first (drive, analog) pair on today's wiring, or None. Needs the lock; releases every plan it made. `exclude`:
    channels not to read (a wire's, a track's of the same run). `full`: the converter's top value (12 bits)."""
    drives = idle_outputs(hst)
    if not drives:
        return None
    gpio = Gpio(hst)
    ana = capture.AnalogCapture(hst)
    skip = set(exclude) | {ch for ch, _ in drives}
    try:
        for drive, rest_high in drives:
            for ch in analog_channels(hst, ana.fn):
                if ch in skip:
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
