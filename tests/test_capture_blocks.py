"""capture_blocks against oep-spec's shared vectors (tests/vectors/capture_formats.json, capture §1.3): every stream
reads back to the levels it was made from - raw channels as bits, reduced channels as their interval values."""
import json
import random
from pathlib import Path

import pytest

from oep_client import capture_blocks as cb, host as h

DATA = json.loads((Path(__file__).parent / "vectors/capture_formats.json").read_text())
KIND = {"sample": cb.SAMPLE, "any_high": cb.ANY_HIGH, "any_low": cb.ANY_LOW, "change_latch": cb.CHANGE_LATCH}


def expect_values(kind: int, levels: list[int], d: int, before):
    """The values §1.3 gives a channel's levels (before: the level before them, or None)."""
    out, last = [], before
    for lo in range(0, len(levels), d):
        part = levels[lo:lo + d]
        if kind == cb.SAMPLE:
            out.append(part[0])
        elif kind == cb.ANY_HIGH:
            out.append(int(1 in part))
        elif kind == cb.ANY_LOW:
            out.append(0 if 0 in part else 1)
        else:
            ch = 0
            for v in part:
                ch |= last is not None and v != last
                last = v
            out.append(part[-1] | ch << 1)
    return out


def _case(case):
    R = case["R"]
    reduced = {R + e: (KIND[k], d) for e, (k, d) in enumerate(zip(case["kinds"], case["d"]))}
    return R, reduced


@pytest.mark.parametrize("case", DATA["logic_blocks"], ids=lambda c: c["name"])
def test_logic_blocks_read_back(case):
    L = case["L"]
    R, reduced = _case(case)
    prev_end = None
    for seg in case["segments"]:
        n = len((seg["raw"] + seg["reduced"])[0])
        data = bytes.fromhex(seg["stream_hex"])
        blocks = cb.read_logic_blocks(data, n, list(range(R)), reduced, L)
        for j, levels in enumerate(seg["raw"]):
            assert "".join("".join(map(str, cb.bits_list(b.raw[j], b.samples))) for b in blocks) == levels
        for e, levels in enumerate(seg["reduced"]):
            role = R + e
            assert sum((b.values[role] for b in blocks), []) == seg["values"][e]
            kind, d = reduced[role]
            before = prev_end[e] if seg["contiguous"] and prev_end else None
            assert seg["values"][e] == expect_values(kind, [int(x) for x in levels], d, before)
            if kind == cb.CHANGE_LATCH:      # each block's start: the level before it (sample 0's after a gap)
                for b in blocks:
                    want = int(levels[b.first - 1]) if b.first else (before if before is not None else int(levels[0]))
                    assert b.starts[role] == want
        for fast in (False, True):          # the same as columns, with numpy and without
            np_ = cb._np
            if fast and np_ is None:
                continue
            cb._np = np_ if fast else None
            try:
                cols = cb.segment_columns(data, n, list(range(R)), reduced, L)
            finally:
                cb._np = np_
            for j, levels in enumerate(seg["raw"]):
                assert "".join(map(str, cb.bits_list(cols[j], n))) == levels
            for e in range(len(seg["reduced"])):
                col = cols[R + e]
                nb = cb.value_bits(col.kind)
                v = int.from_bytes(col.data, "little")
                assert [(v >> (k * nb)) & ((1 << nb) - 1) for k in range(col.n)] == seg["values"][e]
        prev_end = [int(ch[-1]) for ch in seg["reduced"]]


def test_a_cut_segment_is_a_protocol_error():
    case = DATA["logic_blocks"][0]
    seg = case["segments"][0]
    data = bytes.fromhex(seg["stream_hex"])
    R, reduced = _case(case)
    n = len((seg["raw"] + seg["reduced"])[0])
    for bad in (data[:-1], data + b"\0"):
        with pytest.raises(h.ProtocolError):
            cb.read_logic_blocks(bad, n, list(range(R)), reduced, case["L"])
        with pytest.raises(h.ProtocolError):
            cb.segment_columns(bad, n, list(range(R)), reduced, case["L"])


def test_block_bytes_fixed_by_the_setting():
    # §1.3: 4 raw, 12 sample at D 32, L 4096: 2048 + 12 x 128 bits; change_latch adds its start bit and 2 bits an interval
    assert cb.block_bytes(4, {k: (cb.SAMPLE, 32) for k in range(4, 16)}, 4096) == 2048 + 192
    assert cb.block_bytes(0, {0: (cb.CHANGE_LATCH, 8)}, 64) == (1 + 16 + 7) // 8
    assert cb.segment_bytes(72, 1, {1: (cb.SAMPLE, 16)}, 32) == 2 * cb.block_bytes(1, {1: (cb.SAMPLE, 16)}, 32) + \
        cb.block_bytes(1, {1: (cb.SAMPLE, 16)}, 8)


def test_a_change_latch_start_that_does_not_follow_is_broken():
    """A block's change_latch start other than the level the interval before ended on: a broken stream (WireSkein keeps
    one start a segment), with numpy and without."""
    from oep_client.virtual_bench_capture import logic_block
    L, lv = 16, [0] * 8 + [1] * 8 + [1] * 16
    blocks = [logic_block([], [lv[f:f + L]], [cb.CHANGE_LATCH], [8], [lv[f - 1] if f else lv[0]]) for f in (0, 16)]
    good = b"".join(blocks)
    cb.segment_columns(good, 32, [], {0: (cb.CHANGE_LATCH, 8)}, L)
    bad = blocks[0] + bytes([blocks[1][0] ^ 1]) + blocks[1][1:]          # the second block's start bit flipped
    np_ = cb._np
    for fast in (False, True):
        if fast and np_ is None:
            continue
        cb._np = np_ if fast else None
        try:
            with pytest.raises(h.ProtocolError):
                cb.segment_columns(bad, 32, [], {0: (cb.CHANGE_LATCH, 8)}, L)
        finally:
            cb._np = np_


def test_segment_columns_random():
    """Random segments (R 0-3, reduced 0-6 of every carriage, D 2-64 that divide L, L 64-1024, a short last block):
    columns with numpy and without, and blocks, all give the values §1.3 says (virtual bench's encoder)."""
    from oep_client.virtual_bench_capture import logic_block
    rng = random.Random(2)
    for _ in range(60):
        L = rng.choice([64, 256, 1024])
        R, E = rng.randint(0, 3), rng.randint(0, 6)
        R = R or (E == 0)
        kinds = [rng.choice(list(KIND.values())) for _ in range(E)]
        ds = [rng.choice([d for d in (2, 4, 8, 16, 32, 64) if L % d == 0]) for _ in range(E)]
        samples = rng.randint(1, 5 * L)

        def line(p):
            out, v = [], rng.randint(0, 1)
            for _ in range(samples):
                v ^= rng.random() < p
                out.append(int(v))
            return out
        raw = [line(.5) for _ in range(R)]
        red = [line(rng.choice([.001, .05, .5])) for _ in range(E)]
        data = b"".join(logic_block([x[f:f + L] for x in raw], [x[f:f + L] for x in red], kinds, ds,
                                    [red[e][f - 1] if f else red[e][0] for e in range(E)])
                        for f in range(0, samples, L))
        reduced = {R + e: (kinds[e], ds[e]) for e in range(E)}
        np_ = cb._np
        for fast in (False, True):
            if fast and np_ is None:
                continue
            cb._np = np_ if fast else None
            try:
                cols = cb.segment_columns(data, samples, list(range(R)), reduced, L)
            finally:
                cb._np = np_
            for k in range(R):
                assert cb.bits_list(cols[k], samples) == raw[k]
            for e in range(E):
                col = cols[R + e]
                nb = cb.value_bits(kinds[e])
                v = int.from_bytes(col.data, "little")
                got = [(v >> (k * nb)) & ((1 << nb) - 1) for k in range(col.n)]
                assert got == expect_values(kinds[e], red[e], ds[e], None)
                assert col.start_level == (red[e][0] if kinds[e] == cb.CHANGE_LATCH else 0)


def test_named_extensions_and_capture_drops():
    """core §2.2 named extensions read in order, the first of a name kept; the firmware's capture-drops data."""
    import struct
    from oep_client import capture as c, message as m
    drops = struct.pack("<IIIIBBIII", 12288, 0, 1, 0, 1, 4, 49140, 49140, 0)
    name = c.CAPTURE_DROPS.encode()
    other = b"com.example.x"
    tail = m.Tail.parse(m.tlv(m.TAG_NAMED_EXTENSION, bytes([len(name)]) + name + drops)
                        + m.tlv(m.TAG_NAMED_EXTENSION, bytes([len(other)]) + other + b"\x01", critical=True)
                        + m.tlv(m.TAG_NAMED_EXTENSION, bytes([len(name)]) + name + b"later")
                        + m.tlv(m.TAG_NAMED_EXTENSION, b"\x09ab"))                     # its name past its value
    named = tail.named()
    assert list(named) == [c.CAPTURE_DROPS, "com.example.x"] and named["com.example.x"] == b"\x01"
    d = c.CaptureDrops.unpack(named[c.CAPTURE_DROPS])
    assert (d.no_stage_bytes, d.dma_overruns, d.zero_copy, d.stages, d.psram_total) == (12288, 1, True, 4, 0)
    assert d.buffer_peak is None                                            # the form before buffer_peak
    d = c.CaptureDrops.unpack(struct.pack("<IIIIBBIIII", 0, 0, 0, 0, 1, 8, 1, 2, 3, 3585344))
    assert (d.stages, d.psram_total, d.buffer_peak) == (8, 3, 3585344) and d.spill_peak is None
    d = c.CaptureDrops.unpack(struct.pack("<IIIIBBIIIIII", 0, 0, 0, 0, 1, 8, 1, 2, 3, 4928, 5 << 20, 8 << 20))
    assert (d.buffer_peak, d.spill_peak, d.spill_bytes) == (4928, 5 << 20, 8 << 20)   # the spill behind the ring
    assert d.dropped_bytes is None
    d = c.CaptureDrops.unpack(struct.pack("<IIIIBBIIIIIIQ", 0, 0, 0, 0, 0, 0, 1, 2, 3, 0, 0, 0, 7 << 30))
    assert d.zero_copy is False and d.dropped_bytes == 7 << 30   # every byte dropped, the copied way's too
    assert c.CaptureDrops.unpack(b"short") is None


def test_configure_answer_judge_latency():
    """§3.3 judge_latency_ns (0x15, optional): read into Config; absent, None; a value of another length not used."""
    import struct
    from oep_client import capture as c, message as m
    rate = m.tlv(c.ACTUAL_RATE, struct.pack("<II", 5000000, 1))
    cfg = c._config(rate + m.tlv(c.JUDGE_LATENCY, struct.pack("<I", 100819200)), analog=False)
    assert cfg.judge_latency_ns == 100819200
    assert c._config(rate, analog=False).judge_latency_ns is None
    assert c._config(rate + m.tlv(c.JUDGE_LATENCY, b"\x01\x02"), analog=True).judge_latency_ns is None


def test_group_trigger_caps():
    """capture-group describe's trigger_caps (§4.4): read as TriggerCaps from the virtual bench (what it declares, by
    name too); a value of another length is not used (None); trigger_out_latency 0xFFFFFFFF: none."""
    import struct
    from oep_client import capture as c, virtual_bench_trigger as vbt
    from test_virtual_bench_capture import bench
    ep, hst, lc, clock = bench()
    caps = c.CaptureGroup(hst).trigger_caps()
    assert caps is not None and (caps.max_stages, caps.max_terms, caps.max_conds) == (vbt.MAX_STAGES, vbt.MAX_TERMS, vbt.MAX_CONDS)
    assert caps.cross_track and caps.trigger_out_latency_ns is None
    assert set(caps.kinds()) == set(vbt.TYPES) and all(caps.takes(c.COND[k]) for k in vbt.TYPES)
    p4 = c.TriggerCaps.unpack(struct.pack("<BBBBIBI", 1, 1, 1, 0, 0b11110, 0, 0xFFFFFFFF))   # the P4's
    assert p4.kinds() == ["level", "edge", "cross_up", "cross_down"] and not p4.takes(c.COND["above"])
    assert c.TriggerCaps.unpack(b"\x01\x01\x01") is None
