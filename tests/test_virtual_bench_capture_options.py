"""Capture's optional features through the virtual bench (virtual_bench.with_capture_options) and the client: the
glitch filter and the external clock with format 3 (§3.8)."""
import json
import struct
from fractions import Fraction
from pathlib import Path

import pytest

from oep_client import capture as c, capture_blocks as cb, core, host as h, message as m, virtual_bench as vb
from oep_client import virtual_bench_capture as vbc
from test_virtual_bench_capture import bench

DATA = json.loads((Path(__file__).parent / "vectors/capture_formats.json").read_text())
FMT = c.DATA_FORMAT
T = c.OPTION_TLV
EDGE = {"rising": 0, "falling": 1, "both": 2}


def full():
    return bench(lambda: vb.with_capture_options(vb.p4_x035()))


def logic(n=4):
    ep, hst, lc, clock = full()
    core.plan_apply(hst, [(lc.fn, r, 30 + r) for r in range(n)])
    return ep, hst, lc, clock


def one_shot(track, clock, ms=1000):
    track.start()
    clock.t += ms
    (seg,) = track.wait()
    return seg, track.read_segment(seg)


def refused(track, payload=None, **kw):
    """The tag a configure is refused unsupported with."""
    with pytest.raises(h.Unsupported) as e:
        if payload is None:
            track.configure(**kw)
        else:
            track._call(track.CONFIGURE, payload)
    return e.value.result.payload[0]


def detail(track, payload):
    with pytest.raises(h.Rejected) as e:
        track._call(track.CONFIGURE, payload)
    return e.value.result.detail


def base(samples=64, rate=1000):
    return (m.tlv(c.MODE, bytes([c.ONE_SHOT])) + m.tlv(c.RATE, struct.pack("<I", rate))
            + m.tlv(c.SAMPLES, struct.pack("<I", samples)))


def opt(name, value):
    return m.tlv(T[name] | c.CRITICAL, value)


# ---- §3.8: glitch filter -----------------------------------------------------------------------------------------
def test_the_glitch_filter_hides_short_pulses_from_the_data_and_the_trigger():
    ep, hst, lc, clock = logic(3)
    cfg = lc.configure(rate=1_000_000, samples=64, options={"glitch_filter": {0: 2500}})
    assert cfg.used["glitch_used"] == {0: 2500}             # 3 samples at 1 MHz
    seg, data = one_shot(lc, clock, 3)
    assert lc.channel(data, 0, 64) == [0] * 64              # 1-sample pulses: gone
    assert lc.channel(data, 1, 64) == [0] * 64              # 2-sample pulses: gone
    assert lc.channel(data, 2, 64) == [(max(0, i - 2) >> 2) & 1 for i in range(64)]   # held 4: seen, 2 samples late
    assert lc.configure(rate=1_000_000, samples=64, options={"glitch_filter": {0: 1234}}).used["glitch_used"] == {0: 1230}
    lc.configure(rate=1_000_000, samples=64, trigger=(c.EDGE, 0, 2), options={"glitch_filter": {0: 2500}})
    lc.start()
    clock.t += 10
    assert lc.status().state == c.STATE["waiting"]          # the filtered line never moves
    lc.stop()
    assert refused(lc, rate=1000, samples=8, options={"glitch_filter": {0: 20_000}}) == T["glitch_filter"] | c.CRITICAL
    assert refused(lc, rate=1000, samples=8, options={"glitch_filter": {1: 100}}) == T["glitch_filter"] | c.CRITICAL   # group 0 only
    twice = opt("glitch_filter", struct.pack("<BI", 0, 100)) * 2
    assert detail(lc, base() + twice) == m.MALFORMED


# ---- §3.8: external clock and format 3 ---------------------------------------------------------------------------
def test_the_clock_line_is_no_channel_and_samples_count_its_edges():
    ep, hst, lc, clock = logic(4)
    cfg = lc.configure(rate=1000, samples=10, options={"clock": (0, EDGE["rising"], None, 0)})
    assert cfg.external_clock and cfg.rate == 0              # actual_rate 0 / 1: no time
    assert cfg.positions == [0, 1, 2]                        # roles 1, 2, 3: the clock's role 0 left out
    seg, data = one_shot(lc, clock, 20)
    assert [lc.channel(data, k, 10) for k in range(3)] == [[(i >> k) & 1 for i in range(10)] for k in range(3)]
    for options, tag in (({"clock": (0, 3, None, 0)}, T["clock"]),            # an edge not defined
                         ({"clock": (0, 0, 0, 1)}, T["clock"])):              # the clock as its own qualifier
        assert refused(lc, rate=1000, samples=10, options=options) == tag | c.CRITICAL
    assert refused(lc, rate=60_000_000, samples=10, options={"clock": (0, 0, None, 0)}) == c.RATE   # past max_hz
    with pytest.raises(h.Unavailable):
        lc.configure(rate=1000, samples=10, options={"clock": (9, 0, None, 0)})   # not in the plan: cause 6


def test_a_qualifier_keeps_the_edges_its_line_allows():
    ep, hst, lc, clock = logic(4)
    lc.configure(rate=1000, samples=16, options={"clock": (0, EDGE["rising"], 2, 1)}, data_format=FMT["state_timed"])
    seg, data = one_shot(lc, clock, 100)
    rows = lc.state_timed(data, 16)
    # role 2 is channel 1: only the edges whose bit 1 is 1 (2, 3, 6, 7, ...); edge e at (e + 1) ms
    edges = [e for e in range(64) if e >> 1 & 1][:16]
    assert [t for t, _ in rows] == [(e + 1) * 100_000 for e in edges]
    assert [s for _, s in rows] == [e & 0b1111 for e in edges]   # w 4 (3 channels); the qualifier's bit always 1


@pytest.mark.parametrize("edge, phase", [("rising", 100_000), ("falling", 50_000), ("both", 50_000)])
def test_format_3_times_each_sample_in_timebase_ticks(edge, phase):
    ep, hst, lc, clock = logic(3)
    cfg = lc.configure(rate=1000, samples=10, data_format=FMT["state_timed"], options={"clock": (0, EDGE[edge], None, 0)})
    assert (cfg.format, cfg.timebase) == (FMT["state_timed"], vbc.TIMEBASE_HZ)
    seg, data = one_shot(lc, clock, 20)
    step = 50_000 if edge == "both" else 100_000             # 1 kHz: an edge every ms, both edges every 0.5 ms
    rows = lc.state_timed(data, 10)
    assert [t for t, _ in rows] == [phase + i * step for i in range(10)]
    assert [s for _, s in rows] == [i & 0b11 for i in range(10)]   # w 2: the 2 channels besides the clock
    assert refused(lc, rate=1000, samples=10, data_format=FMT["state_timed"]) == c.FORMAT | c.CRITICAL   # no clock


def test_format_3_escapes_a_clock_that_stops_for_long():
    case = DATA["state_timed"][0]                            # the shared vector first
    rows = cb.read_state_timed(bytes.fromhex(case["stream_hex"]), case["samples"], case["w"])
    entries = [e for e in case["entries"] if e["delta"] != cb.ESCAPE]
    assert [s for _, s in rows] == [e["state"] for e in entries]
    ep, hst, lc, clock = logic(2)
    lc.configure(rate=1000, samples=3, data_format=FMT["state_timed"], options={"clock": (0, 0, None, 0)})
    ep.captures[lc.fn].clock_hz = Fraction(1, 50)            # TEST HOOK: an edge every 50 s
    lc.configure(rate=1000, samples=3, data_format=FMT["state_timed"], options={"clock": (0, 0, None, 0)})
    lc.start()
    for _ in range(160):                                     # 160 s, the lease kept
        clock.t += 1000
        hst.keepalive()
    (seg,) = lc.wait()
    data = lc.read_segment(seg)
    tick = 50 * vbc.TIMEBASE_HZ                              # past 0xFFFFFFFF: one escape before each sample
    assert [t for t, _ in lc.state_timed(data, 3)] == [tick * (i + 1) for i in range(3)]
    assert len(data) == 6 * (4 + 1)


def test_a_host_skips_answer_tlvs_it_does_not_know():
    rate = m.tlv(c.ACTUAL_RATE, struct.pack("<II", 1000, 1))
    unknown = max(c._CAP.tlv["configure_answer"].values()) + 1
    cfg = c._config(m.tlv(unknown, b"\x01\x02\x03") + rate, analog=False)
    assert cfg.rate == 1000 and not cfg.used
