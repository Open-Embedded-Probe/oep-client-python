"""Capture's optional features through the virtual bench (virtual_bench.with_capture_options) and the client: named
formats (§1, format 0xFF), the front end and the external clock with format 3 (§3.8), the external data paths (§3.9)."""
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
KIND = c._CAP.enum["external_kind"]
MUX = c._CAP.enum["external_mux"]
PATH = c._CAP.enum["data_path"]
COUPLING = {"dc": 0, "ac": 1, "gnd": 2}                     # §3.8's table (no registry enum)
EDGE = {"rising": 0, "falling": 1, "both": 2}


def full():
    return bench(lambda: vb.with_capture_options(vb.p4_x035()))


def logic(n=4):
    ep, hst, lc, clock = full()
    core.plan_apply(hst, [(lc.fn, r, 30 + r) for r in range(n)])
    return ep, hst, lc, clock


def analog(roles=(0,)):
    ep, hst, _, clock = full()
    track = c.AnalogCapture(hst)
    core.plan_apply(hst, [(track.fn, r, 16 + i) for i, r in enumerate(roles)])
    return ep, hst, track, clock


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


# ---- §1: named formats -------------------------------------------------------------------------------------------
def test_a_named_format_is_used_only_when_asked_by_its_name():
    ep, hst, lc, clock = logic(3)
    cfg = lc.configure(rate=1_000_000, samples=1000, data_format=FMT["named"], format_name=vb.NAMED_FORMAT)
    assert (cfg.format, cfg.format_name) == (FMT["named"], vb.NAMED_FORMAT)
    seg, data = one_shot(lc, clock, 3)
    assert data == bytes(k & 0xFF for k in range(500))      # its owner's definition: format 4's counter
    named = opt("format_name", vb.NAMED_FORMAT.encode())
    assert refused(lc, base() + named) == T["format_name"] | c.CRITICAL          # without format 0xFF
    assert refused(lc, base() + m.tlv(c.FORMAT | c.CRITICAL, bytes([FMT["named"]]))
                   + opt("format_name", b"org.example.another")) == T["format_name"] | c.CRITICAL   # not declared
    assert detail(lc, base() + m.tlv(c.FORMAT | c.CRITICAL, bytes([FMT["named"]]))) == m.MALFORMED   # no name
    for bad in (b"", b"Org.Example", b"x" * 65, b"a_b"):
        assert detail(lc, base() + m.tlv(c.FORMAT | c.CRITICAL, bytes([FMT["named"]]))
                      + opt("format_name", bad)) == m.MALFORMED
    ep, hst, plain, clock = bench()                          # a probe without named formats: format 0xFF refused
    core.plan_apply(hst, [(plain.fn, 0, 30)])
    assert refused(plain, base() + m.tlv(c.FORMAT | c.CRITICAL, bytes([FMT["named"]])) + named) \
        in (c.FORMAT | c.CRITICAL, T["format_name"] | c.CRITICAL)


# ---- §3.8: threshold and glitch filter -----------------------------------------------------------------------------
def test_the_threshold_takes_the_nearest_value_it_can_and_says_so():
    ep, hst, lc, clock = logic()
    cfg = lc.configure(rate=1000, samples=8, options={"threshold": {0: (1234, 70)}})
    assert cfg.used["threshold_used"] == {0: (1250, 50)}     # 50 mV steps; the preset 1400 is further
    cfg = lc.configure(rate=1000, samples=8, options={"threshold": {0: (1390, 0)}})
    assert cfg.used["threshold_used"] == {0: (1400, 0)}
    for value in ((3400, 0), (1650, 300)):                  # past max_mv, past hysteresis_max_mv
        assert refused(lc, rate=1000, samples=8, options={"threshold": {0: value}}) == T["threshold"] | c.CRITICAL
    assert refused(lc, rate=1000, samples=8, options={"threshold": {1: (1650, 0)}}) == T["threshold"] | c.CRITICAL
    twice = opt("threshold", struct.pack("<BiI", 0, 1650, 0)) * 2
    assert detail(lc, base() + twice) == m.MALFORMED


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


# ---- §3.8: the analog front end ------------------------------------------------------------------------------------
def test_coupling_and_offset_and_the_scale_they_give():
    ep, hst, track, clock = analog((0, 1))
    cfg = track.configure(rate=1000, samples=16, options={"coupling": {0: COUPLING["ac"], 1: COUPLING["gnd"]},
                                                          "offset": {1: -333}})
    assert cfg.used["coupling_used"] == {0: COUPLING["ac"], 1: COUPLING["gnd"]}
    assert cfg.used["offset_used"] == {1: -330}              # 10 mV steps
    assert cfg.zero[1] == round(330_000_000 / cfg.scale_nv[1])   # (value - zero) x scale: the input with the offset
    seg, data = one_shot(track, clock)
    assert track.values(data, 0, 16) == [vbc.analog_value(0, i) for i in range(16)]   # its waves have no DC
    assert set(track.values(data, 1, 16)) == {cfg.zero[1]}   # grounded: 0 V at the pin
    for options in ({"coupling": {0: 3}}, {"offset": {0: 2000}}):
        name = next(iter(options))
        assert refused(track, rate=1000, samples=16, options=options) == T[name] | c.CRITICAL
    with pytest.raises(h.Unavailable):
        track.configure(rate=1000, samples=16, options={"offset": {3: 0}})       # not in the plan: cause 6


def test_bandwidth_takes_the_nearest_limit():
    ep, hst, track, clock = analog((0,))
    for asked, used in ((15_000, 20_000), (1_000, 2_000), (0, 0)):
        assert track.configure(rate=1000, samples=8, options={"bandwidth": {0: asked}}).used["bandwidth_used"] == {0: used}


@pytest.mark.parametrize("bits, ratio, b, slot", [(8, 1, 8, 16), (16, 1, 16, 16), (12, 4, 13, 16), (16, 16, 18, 32)])
def test_resolution_and_oversample_set_b(bits, ratio, b, slot):
    ep, hst, track, clock = analog((0,))
    cfg = track.configure(rate=1000, samples=16, options={"resolution": bits, "oversample": ratio})
    assert (cfg.bits, cfg.slot, cfg.used["resolution_used"], cfg.used["oversample_used"]) == (b, slot, bits, ratio)
    seg, data = one_shot(track, clock)
    want = [vbc.analog_value(0, i) for i in range(16)]
    want = [v << (bits - 12) if bits >= 12 else v >> (12 - bits) for v in want]
    assert track.values(data, 0, 16) == [v << (b - bits) for v in want]


def test_resolution_and_oversample_bound_the_rate():
    ep, hst, track, clock = analog((0,))
    assert track.configure(rate=83_333, samples=8, options={"resolution": 16}).rate <= 20_833
    assert track.configure(rate=83_333, samples=8, options={"oversample": 4}).rate <= Fraction(83_333, 4)
    for options in ({"resolution": 10}, {"oversample": 3}):
        assert refused(track, rate=1000, samples=8, options=options) == T[next(iter(options))] | c.CRITICAL


def test_impedance_is_declared_only():
    ep, hst, track, clock = analog((0,))
    raw = [t for t in ep.decl[track.fn] if t[0] == c._ANA.tlv["describe"]["impedance"]]   # no configure TLV of its own
    assert len(raw) == 4 and struct.unpack("<BII", raw[0][1]) == (0, 1_000_000, 0xFFFFFFFF)


# ---- §3.9: external data paths -------------------------------------------------------------------------------------
def mark(generation, fn, fmt):
    return b"OEPX" + struct.pack("<IHB5x", generation, fn, fmt)


def test_the_mark_is_the_shared_vectors():
    v = DATA["external"][0]
    assert mark(v["generation"], v["fn"], v["format"]).hex() == v["bytes_hex"]


def test_tcp_carries_the_mark_and_the_data_and_read_is_refused():
    ep, hst, lc, clock = logic(3)
    lc.configure(rate=1_000_000, samples=1000, options={"data_path": (PATH["external"], MUX["single"], KIND["tcp"])})
    lc.start()
    clock.t += 3
    (seg,) = lc.wait()
    sink = ep.external_paths[KIND["tcp"]]
    assert sink.stream[:16] == mark(lc.generation, lc.fn, 0)
    data = bytes(sink.stream[16:])
    assert len(data) == 500 and lc.status().write_pos == 500   # write_pos: what went out, the mark not counted
    assert lc.channel(data, 0, 1000) == [i & 1 for i in range(1000)]
    with pytest.raises(h.Unavailable):
        lc.read(seg.position, 16, seg.generation)            # its data is not on the OEP route


def test_tcp_mux_1_puts_each_tracks_bytes_in_chunks_of_its_fn():
    ep, hst, lc, clock = logic(2)
    track = c.AnalogCapture(hst)
    core.plan_apply(hst, [(lc.fn, 0, 30), (lc.fn, 1, 31), (track.fn, 0, 16)])
    chunks = {"data_path": (PATH["external"], MUX["chunks"], KIND["tcp"])}
    lc.configure(rate=1000, samples=16, options=chunks)
    track.configure(rate=1000, samples=16, options=chunks)
    lc.start()
    track.start()
    clock.t += 100
    lc.wait()
    track.wait()
    stream, got = bytes(ep.external_paths[KIND["tcp"]].stream), {}
    at = 0
    while at < len(stream):
        fn, n = struct.unpack_from("<HH", stream, at)
        got[fn] = got.get(fn, b"") + stream[at + 4:at + 4 + n]
        at += 4 + n
    assert got[lc.fn][:16] == mark(lc.generation, lc.fn, 0) and len(got[lc.fn]) == 16 + 4
    assert got[track.fn][:16] == mark(track.generation, track.fn, 0) and len(got[track.fn]) == 16 + 32


def test_udp_packets_carry_their_position_and_streaming_needs_no_subscription():
    ep, hst, lc, clock = logic(8)
    lc.configure(rate=1_000_000, mode=c.STREAMING, options={"data_path": (PATH["external"], MUX["single"], KIND["udp"])})
    lc.start()                                               # not subscribed: its data does not go as notifications
    clock.t += 5
    lc.stop()
    assert not [f for f in ep.pushes() if f[0] == m.ROLE_DATA]
    packets = ep.external_paths[KIND["udp"]].packets
    assert packets[0][16:] == mark(lc.generation, lc.fn, 0)
    at = 0
    for p in packets[1:]:
        position, generation, fn, _ = struct.unpack_from("<QIHH", p)
        assert (position, generation, fn) == (at, lc.generation, lc.fn) and len(p) <= 1400
        at += len(p) - 16
    assert at == lc.status().write_pos > 0


def test_a_path_too_narrow_is_refused_unless_overflow_1():
    ep, hst, lc, clock = logic(16)
    tcp = (PATH["external"], MUX["single"], KIND["tcp"])
    assert refused(lc, rate=20_000_000, samples=1000, options={"data_path": tcp}) == T["data_path"] | c.CRITICAL
    lc.configure(rate=20_000_000, samples=1000, options={"data_path": tcp, "overflow": 1})
    assert refused(lc, rate=1000, samples=8, options={"overflow": 1}) == T["overflow"]   # path 1's only (sent plain)
    for path in ((PATH["external"], MUX["single"], KIND["usb_iso_in"]), (PATH["external"], MUX["chunks"], KIND["udp"])):
        assert refused(lc, rate=1000, samples=8, options={"data_path": path}) == T["data_path"] | c.CRITICAL


def test_a_path_that_goes_stops_the_capture_in_error():
    ep, hst, lc, clock = logic(2)
    lc.configure(rate=1000, mode=c.STREAMING, options={"data_path": (PATH["external"], MUX["single"], KIND["tcp"])})
    lc.start()
    clock.t += 5
    ep.external_disconnect(KIND["tcp"])
    st = lc.status()
    assert (st.state, st.error) == (c.STATE["error"], c._CAP.enum["error"]["peripheral"])


def test_a_host_skips_answer_tlvs_it_does_not_know():
    rate = m.tlv(c.ACTUAL_RATE, struct.pack("<II", 1000, 1))
    unknown = max(c._CAP.tlv["configure_answer"].values()) + 1
    cfg = c._config(m.tlv(unknown, b"\x01\x02\x03") + rate, analog=False)
    assert cfg.rate == 1000 and not cfg.used
