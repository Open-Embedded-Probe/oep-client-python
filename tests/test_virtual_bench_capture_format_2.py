"""Capture §1.4 data format 2 (analog blocks): oep-spec's shared vectors, and through the virtual bench and the client,
end to end."""
import json
import struct
from pathlib import Path

import pytest

from oep_client import capture as c, capture_blocks as cb, core, host as h, message as m
from oep_client.virtual_bench_capture import analog_part, analog_value
from test_virtual_bench_capture import bench
from test_virtual_bench_capture_formats import streamed_segments

DATA = json.loads((Path(__file__).parent / "vectors/capture_formats.json").read_text())
ANALOG_BLOCKS = c.DATA_FORMAT["analog_blocks"]
KIND = {"packed": cb.PACKED, "minmax": cb.MINMAX, "sum": cb.SUM}
BOTH = cb.MINMAX | cb.SUM


@pytest.mark.parametrize("case", DATA["analog_blocks"], ids=lambda c: c["name"])
def test_the_shared_vectors_are_made_and_read_back(case):
    b, L = case["b"], case["L"]
    kinds = [sum(KIND[k] for k in names) for names in case["kinds"]]
    order = list(range(len(kinds)))
    carriage = {k: (kind, d) for k, (kind, d) in enumerate(zip(kinds, case["d"])) if kind != cb.PACKED}
    n = len(case["channels"][0])
    made = b"".join(analog_part(ch[lo:lo + L], kind, d, b)
                    for lo in range(0, n, L) for ch, kind, d in zip(case["channels"], kinds, case["d"]))
    assert made.hex() == case["stream_hex"]
    blocks = cb.read_analog_blocks(bytes.fromhex(case["stream_hex"]), n, order, carriage, L, b)
    for k, ch in enumerate(case["channels"]):
        assert_part(blocks, k, lambda i, ch=ch: ch[i])
    with pytest.raises(h.ProtocolError):
        cb.read_analog_blocks(bytes.fromhex(case["stream_hex"]) + b"\0", n, order, carriage, L, b)


def assert_part(blocks, role, value):
    """Every block's part of `role` against the values it was made from (`value(i)`, i in the segment)."""
    for blk in blocks:
        part = blk.channels[role]
        vals = [value(blk.first + i) for i in range(blk.samples)]
        if part.kind == cb.PACKED:
            assert part.values == vals
            continue
        cuts = [vals[lo:lo + part.d] for lo in range(0, blk.samples, part.d)]
        assert part.points(blk.samples) == [len(x) for x in cuts]
        if part.kind & cb.MINMAX:
            assert (part.mins, part.maxs) == ([min(x) for x in cuts], [max(x) for x in cuts])
        if part.kind & cb.SUM:
            assert part.sums == [sum(x) for x in cuts]


def analog(roles):
    ep, hst, _, clock = bench()
    track = c.AnalogCapture(hst)
    core.plan_apply(hst, [(track.fn, r, 16 + i) for i, r in enumerate(roles)])
    return ep, hst, track, clock


def body(**more):
    out = (m.tlv(c.MODE, bytes([c.ONE_SHOT])) + m.tlv(c.RATE, struct.pack("<I", 1000))
           + m.tlv(c.SAMPLES, struct.pack("<I", 64)))
    return out + b"".join(more.values())


def fmt():
    return m.tlv(c.FORMAT | c.CRITICAL, bytes([ANALOG_BLOCKS]))


def carriage(role, kind, d):
    return m.tlv(c.CARRIAGE | c.CRITICAL, struct.pack("<BBI", role, kind, d))


def test_one_shot_blocks_hold_each_channels_part_in_the_layouts_order():
    ep, hst, track, clock = analog([0, 1, 3])
    chosen = {0: (BOTH, 16), 1: (cb.PACKED, 1), 3: (cb.MINMAX, 8)}
    cfg = track.configure(rate=1000, samples=200, data_format=ANALOG_BLOCKS, carriage=chosen, block_max=64)
    assert (cfg.format, cfg.block, cfg.order, cfg.bits) == (ANALOG_BLOCKS, 64, [0, 1, 3], 12)
    track.start()
    clock.t += 1000
    (seg,) = track.wait()
    data = track.read_segment(seg)
    # 64 samples: role 0 4 x (24 + 16) bits, role 1 64 x 12, role 3 8 x 24 -> 20 + 96 + 24 bytes; the last block 8 samples
    assert len(data) == 3 * 140 + (5 + 12 + 3)
    blocks = track.analog_blocks(data, seg.samples)
    assert [b.samples for b in blocks] == [64, 64, 64, 8]
    for role in chosen:
        assert_part(blocks, role, lambda i, role=role: analog_value(role, i))


def test_a_trigger_is_judged_on_the_original_samples():
    ep, hst, track, clock = analog([0])
    cfg = track.configure(rate=1000, samples=64, trigger=(c.CROSS_DOWN, 0, 2000), pretrigger=8,
                          data_format=ANALOG_BLOCKS, carriage={0: (BOTH, 64)}, block_max=64)
    track.start()
    clock.t += 1000
    (seg,) = track.wait()
    # role 0 is a square of period 64 (high the first 32): the fall is at sample 32, inside one interval of 64
    assert seg.trigger_index == 8
    (blk,) = track.analog_blocks(track.read_segment(seg), seg.samples)
    assert (blk.channels[0].mins, blk.channels[0].maxs) == ([0], [4095])


def test_repeat_and_streaming_begin_their_blocks_at_each_segments_sample_0():
    ep, hst, track, clock = analog([1])
    track.configure(rate=1000, mode=c.REPEAT, samples=100, segments=3, data_format=ANALOG_BLOCKS,
                    carriage={1: (cb.SUM, 16)}, block_max=32)
    track.start()
    clock.t += 1000
    segs = track.segments()
    assert len(segs) == 3
    for seg in segs:
        first = seg.serial * 100
        assert_part(track.analog_blocks(track.read_segment(seg), seg.samples), 1,
                    lambda i, first=first: analog_value(1, first + i))
    ep, hst, track, clock = analog([0, 1])
    cfg = track.configure(rate=10_000, mode=c.STREAMING, data_format=ANALOG_BLOCKS, carriage={0: (BOTH, 8)}, block_max=256)
    track.subscribe()
    track.start()
    got = {}
    for _ in range(5):
        clock.t += 300
        streamed_segments(ep, got)
    clock.t += 7
    track.stop()
    streamed_segments(ep, got)
    first = 0
    for position in sorted(got):
        data, samples = got[position]
        assert len(data) == track.config.segment_bytes(samples)
        blocks = track.analog_blocks(data, samples)
        for role in (0, 1):
            assert_part(blocks, role, lambda i, role=role, first=first: analog_value(role, first + i))
        first += samples
    assert first == int(1507 * cfg.rate / 1000)            # all 1.507 s of it, the last block short


@pytest.mark.parametrize("tail, why", [
    (carriage(0, 0, 4), "a kind with no bit"),
    (carriage(0, cb.PACKED | cb.MINMAX, 4), "packed with another"),
    (carriage(0, cb.MINMAX, 0), "d 0"),
    (carriage(0, cb.MINMAX, 4) + carriage(0, cb.SUM, 4), "a role twice"),
])
def test_carriages_the_definition_excludes_are_malformed(tail, why):
    ep, hst, track, clock = analog([0])
    with pytest.raises(h.Rejected) as e:
        track._call(track.CONFIGURE, body(f=fmt(), t=tail))
    assert e.value.result.detail == m.MALFORMED, why
    with pytest.raises(h.Rejected) as e:                    # carriage without format 1 / 2
        track._call(track.CONFIGURE, body(t=carriage(0, cb.MINMAX, 4)))
    assert e.value.result.detail == m.MALFORMED


def test_what_it_cannot_do_is_refused_with_the_tag_as_received():
    ep, hst, track, clock = analog([0])
    for tail, tag in ((carriage(0, cb.MINMAX, 1 << 20), c.CARRIAGE | c.CRITICAL),   # past the declared max_d
                      (carriage(0, 8, 4), c.CARRIAGE | c.CRITICAL),                 # a bit it does not declare
                      (carriage(0, cb.MINMAX, 3) + m.tlv(c.BLOCK_MAX, struct.pack("<I", 16)), c.BLOCK_MAX)):
        with pytest.raises(h.Unsupported) as e:
            track._call(track.CONFIGURE, body(f=fmt(), t=tail))
        assert e.value.result.payload == bytes([tag])
    with pytest.raises(h.Unavailable):                      # a role not in the plan: cause 6
        track._call(track.CONFIGURE, body(f=fmt(), t=carriage(2, cb.MINMAX, 4)))
    ep, hst, lc, clock = bench()                            # format 2 is the analog's
    core.plan_apply(hst, [(lc.fn, 0, 30)])
    with pytest.raises(h.Unsupported) as e:
        lc._call(lc.CONFIGURE, body(f=fmt()))
    assert e.value.result.payload == bytes([c.FORMAT | c.CRITICAL])
