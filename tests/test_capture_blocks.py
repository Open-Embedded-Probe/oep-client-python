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
    assert c.CaptureDrops.unpack(b"short") is None
