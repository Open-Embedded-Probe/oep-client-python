"""capture_blocks against oep-spec's shared vectors (tests/vectors/capture_formats.json, capture §1.3): every stream
reads back to the levels it was made from - raw and fallback channels as bits, events channels as their ticks."""
import json
from pathlib import Path

import pytest

from oep_client import capture_blocks as cb, host as h

DATA = json.loads((Path(__file__).parent / "vectors/capture_formats.json").read_text())


def expect_ticks(levels: str, first: int, r: int, d: int, before: int):
    out, last = [], before
    for t in range(-(-r // d)):
        c = 0
        for i in range(t * d, min((t + 1) * d, r)):
            v = int(levels[first + i])
            c += v != last
            last = v
        if c:
            out.append((t, last, min(c, cb.COUNT_SATURATED)))
    return out


FAST = [False] + ([True] if cb._np is not None else [])


@pytest.mark.parametrize("fast", FAST)
@pytest.mark.parametrize("case", DATA["logic_blocks"], ids=lambda c: c["name"])
def test_logic_blocks_read_back(case, fast):
    L, R, ds = case["L"], case["R"], case["events_d"]
    raw_roles = list(range(R))
    event_d = {R + e: d for e, d in enumerate(ds)}
    prev_end = None
    for seg in case["segments"]:
        n = len((seg["raw"] + seg["events"])[0])
        blocks = cb.read_logic_blocks(bytes.fromhex(seg["stream_hex"]), n, raw_roles, event_d, L, use_numpy=fast)
        for j, levels in enumerate(seg["raw"]):
            got = "".join("".join(map(str, cb.bits_list(b.raw[j], b.samples))) for b in blocks)
            assert got == levels
        for e, levels in enumerate(seg["events"]):
            role = R + e
            before = prev_end[e] if seg["contiguous"] and prev_end else int(levels[0])
            for b in blocks:
                part = levels[b.first:b.first + b.samples]
                assert b.start_levels[role] == before              # fallback or not (§1.3 start_level)
                if role in b.fallback:
                    assert "".join(map(str, cb.bits_list(b.raw[role], b.samples))) == part
                else:
                    c = b.events[role]
                    assert c.start_level == before
                    assert list(zip(c.ticks, c.levels, c.counts)) == expect_ticks(levels, b.first, b.samples, ds[e], before)
                before = int(part[-1])
        prev_end = [int(ch[-1]) for ch in seg["events"]]


@pytest.mark.parametrize("fast", FAST)
def test_a_cut_segment_is_a_protocol_error(fast):
    case = DATA["logic_blocks"][0]
    seg = case["segments"][0]
    data = bytes.fromhex(seg["stream_hex"])
    n = len((seg["raw"] + seg["events"])[0])
    args = (n, list(range(case["R"])), {case["R"] + e: d for e, d in enumerate(case["events_d"])}, case["L"])
    with pytest.raises(h.ProtocolError):
        cb.read_logic_blocks(data[:-1], *args, use_numpy=fast)
    with pytest.raises(h.ProtocolError):
        cb.read_logic_blocks(data + b"\0", *args, use_numpy=fast)


def test_worst_bytes_matches_the_spec_example():
    # §1.3: 16 channels, 4 raw, 12 events at D 64, L 4096 -> 8 + 2048 + 3072
    assert cb.worst_bytes(4, [64] * 12, 4096) == 5128


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


def test_segment_columns_match_the_lines():
    """segment_columns (format 1 as columns, no blocks): random segments (R 0-3, E 0-9, D 1/2/8/64, L 64-1024, a short
    last block, quiet and busy lines so some blocks fall back) read back as the lines they were made from - a raw
    channel's bits, an events channel's ticks counted from the segment's first sample (changes in each, the level at
    its end; a fallback block's counted from its samples) and its level before the segment. Malformed: cut short, a
    byte past the end."""
    import random
    pytest.importorskip("numpy")                  # optional (segment_columns needs it; read_logic_blocks does not)
    from oep_client import host as h
    from oep_client.virtual_bench_capture import logic_block
    rng = random.Random(2)
    for _ in range(60):
        L = rng.choice([64, 256, 1024])
        R, E = rng.randint(0, 3), rng.randint(0, 9)
        R = R or (E == 0)
        ds = [d if L % d == 0 else 1 for d in (rng.choice([1, 2, 8, 64]) for _ in range(E))]
        samples = rng.randint(1, 5 * L)

        def line(p):
            out, v = [], rng.randint(0, 1)
            for _ in range(samples):
                v ^= rng.random() < p
                out.append(int(v))
            return out
        raw = [line(.5) for _ in range(R)]
        evs = [line(rng.choice([.001, .05, .5])) for _ in range(E)]
        before = [rng.randint(0, 1) for _ in range(E)]
        data = b"".join(logic_block([x[f:f + L] for x in raw], [x[f:f + L] for x in evs], ds,
                                    [evs[e][f - 1] if f else before[e] for e in range(E)], L)
                        for f in range(0, samples, L))
        event_d = {R + e: ds[e] for e in range(E)}
        cols = cb.segment_columns(data, samples, list(range(R)), event_d, L)
        for k in range(R):
            assert cb.bits_list(cols[k], samples) == raw[k]
        for e in range(E):
            d, v, prev = ds[e], evs[e], before[e]
            want = []
            for q in range(-(-samples // d)):
                part = v[q * d:(q + 1) * d]
                n = sum(a != b for a, b in zip([prev] + part[:-1], part))
                if n:
                    want.append((q, part[-1], n))
                prev = part[-1]
            col = cols[R + e]
            got = list(zip(col.quanta.tolist(), col.levels.tolist(), col.counts.tolist()))
            assert got == want and col.start_level == before[e]
        with pytest.raises(h.ProtocolError):
            cb.segment_columns(data[:-1], samples, list(range(R)), event_d, L)
        with pytest.raises(h.ProtocolError):
            cb.segment_columns(data + b"\0", samples, list(range(R)), event_d, L)


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
    p4 = c.TriggerCaps.unpack(struct.pack("<BBBBIBI", 1, 1, 1, 0, 0b11110, 0, 0xFFFFFFFF))   # the P4's (ed51b03)
    assert p4.kinds() == ["level", "edge", "cross_up", "cross_down"] and not p4.takes(c.COND["above"])
    assert c.TriggerCaps.unpack(b"\x01\x01\x01") is None
