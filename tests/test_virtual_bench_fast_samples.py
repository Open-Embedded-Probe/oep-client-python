"""The virtual bench's logic samples made at once with numpy are the ones value() gives, sample by sample: the counter,
the logic waves, every w, any first sample and length; and a second of 1 MS/s on 2 lines takes well under a second."""

import random
import time
from fractions import Fraction

import pytest

from oep_client import virtual_bench_capture as vbc

pytest.importorskip("numpy")

WAVES = ["quiet", "quiet:0", "square:6", "square:7", "uart:115200", "uart:9600:Hi", "uart:1000000:OEP\n"]


def track(width, channels, rate, waves):
    cap = vbc.VirtualCapture({0, 3}, {width}, 1, 1 << 30)
    cap.width, cap.channels, cap.rate, cap.format = width, channels, Fraction(rate), vbc.FORMAT["raw"] \
        if "raw" in vbc.FORMAT else 0
    cap.logic_roles = tuple(range(channels))
    cap.waves = {k: vbc.logic_wave(w) for k, w in waves.items()}
    return cap


def slow(cap, first, n):
    return vbc.pack_samples([sum(cap.value(k, first + i) << k for k in range(cap.width)) for i in range(n)], cap.width)


def test_at_once_is_value_sample_by_sample():
    rnd = random.Random(5)
    for _ in range(200):
        width = rnd.choice([1, 2, 3, 4, 5, 8, 12, 16])
        channels = rnd.randint(1, width)
        rate = rnd.choice([1_000_000, 3_000_000, Fraction(80_000_000, 3), 48_000])
        waves = {k: rnd.choice(WAVES) for k in range(channels) if rnd.random() < 0.6}
        cap = track(width, channels, rate, waves)
        first, n = rnd.randrange(0, 1 << 24), rnd.randint(1, 300)
        assert cap._levels(first, n) is not None
        assert cap._pack(first, n) == slow(cap, first, n), (width, channels, rate, waves, first, n)


def test_a_second_of_1_ms_s_on_2_lines_is_fast():
    cap = track(2, 2, 1_000_000, {1: "uart:115200"})
    t = time.perf_counter()
    for at in range(0, 1_000_000, vbc.BLOCKS_A_TICK):
        cap._pack(at, vbc.BLOCKS_A_TICK)
    assert time.perf_counter() - t < 0.5


def test_format_1_blocks_at_once_are_the_same():
    rnd = random.Random(6)
    for _ in range(150):
        channels = rnd.randint(1, 4)
        cap = track(8, channels, 1_000_000, {k: rnd.choice(WAVES) for k in range(channels) if rnd.random() < 0.6})
        cap.block = rnd.choice([64, 256])
        cap.carriages = {k: (rnd.choice([v for name, v in vbc.CARRIAGE.items() if name != "raw"]), rnd.choice([2, 4, 8]))
                         for k in range(channels) if rnd.random() < 0.5}
        first, n = rnd.randrange(1, 1 << 20), rnd.randint(1, 700)
        contiguous = rnd.random() < 0.5
        fast = cap._blocks(first, n, contiguous=contiguous)
        levels = cap._levels
        cap._levels = lambda first, n: None
        assert fast == cap._blocks(first, n, contiguous=contiguous)
        cap._levels = levels
