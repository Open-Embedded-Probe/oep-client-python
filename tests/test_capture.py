"""oep_client.capture: the configure answer and the §3.0 logic layout (oep-spec logic-capture.ja.md)."""

import struct
from fractions import Fraction

from oep_client import capture as c, message as m, registry as reg


def answer(*items):
    return b"".join(m.tlv(t, v) for t, v in items)


def test_configure_answer_is_read_into_actual_values():
    """oep-if-capture: the answer's TLVs are the actual values; an unknown one (here 0x7E) is skipped (core §2.3)."""
    cfg = c._config(answer((c.ACTUAL_RATE, struct.pack("<II", 7000000, 1)), (c.LAYOUT, bytes([4, 3, 0, 1, 2])),
                           (c.ACTUAL_SAMPLES, struct.pack("<I", 1000)), (0x7E, b"\x01\x02")), analog=False)
    assert cfg.rate == Fraction(7000000) and cfg.width == 4 and cfg.positions == [0, 1, 2]
    assert cfg.samples == 1000 and cfg.bytes == 500


def test_configure_answer_has_no_timing_or_rate_accuracy():
    """The configure answer has no timing (0x54) or rate_accuracy (0x5A), and there is no ignored TLV (core §2.3): the
    client keeps no jitter / measured rate / ignored list."""
    for i in (reg.FIXTURE_LOGIC, reg.FIXTURE_ANALOG):
        assert not {"timing", "rate_accuracy"} & set(i.tlv["configure_answer"])
        assert not {0x54, 0x5A} & set(i.tlv["configure_answer"].values())
    cfg = c._config(answer((0x54, bytes(8)), (0x5A, bytes(8)), (c.ACTUAL_RATE, struct.pack("<II", 1000, 1))),
                    analog=True)                                       # unknown to this client now: skipped
    assert cfg.rate == Fraction(1000)
    for gone in ("jitter_ns", "jitter_max_ns", "rate_measured", "rate_ppm", "ignored"):
        assert not hasattr(cfg, gone)
    assert not hasattr(c, "IGNORED") and not hasattr(m, "TAG_IGNORED")


def logic(width, positions):
    lc = c.LogicCapture.__new__(c.LogicCapture)
    lc.config = c.Config(rate=Fraction(1), width=width, positions=positions)
    return lc


def test_three_channels_in_four_bit_samples_with_undefined_bits():
    # samples: (ch0, ch1, ch2) = (1,0,1), (0,1,1); the fourth bit of each sample is garbage and must be ignored
    byte = 0b1_101 | (0b1_110 << 4)
    lc = logic(4, [0, 1, 2])
    assert [lc.channel(bytes([byte]), k, 2) for k in range(3)] == [[1, 0], [0, 1], [1, 1]]


def test_a_byte_per_sample_probe_with_the_channel_on_bit_5():
    lc = logic(8, [5])
    assert lc.channel(bytes([0xDF, 0x20, 0xFF, 0x00]), 0) == [0, 1, 1, 0]


def test_one_channel_packs_eight_samples_per_byte_lsb_first():
    lc = logic(1, [0])
    assert lc.channel(bytes([0b00000101]), 0) == [1, 0, 1, 0, 0, 0, 0, 0]


def test_segment_without_trigger():
    s = c.Segment.unpack(struct.pack("<IQIQIIBI", 0, 0, 200192, 123000, 50, 0xFFFFFFFF, 0, 3))
    assert s.trigger_index is None and s.samples == 200192 and s.generation == 3 and c.SEGMENT_BYTES == 37


class FakeLink:
    """pushes arrive in batches, one batch per pump()"""

    def __init__(self, batches):
        self.pushes, self.batches = __import__("collections").deque(), list(batches)
        self.events = __import__("collections").deque()

    def pump(self, timeout=0.0, until_one=False):
        if self.batches:
            self.pushes.extend(self.batches.pop(0))
        return 0


def push(fn, seq, position, data, generation=None):
    """A data frame (core §11.2): role fn seq position(u64) len(u16) data [TLV generation]."""
    tail = b"" if generation is None else struct.pack("<BHI", c.DATA_GENERATION, 4, generation)
    return bytes([0x06]) + struct.pack("<HHQH", fn, seq, position, len(data)) + data + tail


def test_stream_follows_positions_and_counts_gaps_and_lost_frames():
    cap = c.LogicCapture.__new__(c.LogicCapture)
    cap.fn = 3
    other = push(4, 0, 0, b"zz")                              # another interface's push stays on the link
    link = FakeLink([[push(3, 0, 100, b"ab"), other], [push(3, 1, 102, b"cd")],
                     [push(3, 3, 110, b"ef")]])               # seq 2 lost, and 104..109 dropped by the probe
    got = cap.stream(link, nbytes=6)
    assert bytes(got.data) == b"abcdef" and got.start == 100 and got.frames == 3
    assert got.gaps == [(4, 6)] and got.seq_lost == 1 and list(link.pushes) == [other]


def test_stream_positions_past_4_gib_follow_on_without_a_gap():
    cap = c.LogicCapture.__new__(c.LogicCapture)
    cap.fn = 1
    link = FakeLink([[push(1, 0, 0xFFFFFFFE, b"ab"), push(1, 1, 0x100000000, b"cd")]])   # u64: no wrap at 2^32
    got = cap.stream(link, nbytes=4)
    assert bytes(got.data) == b"abcd" and got.gaps == [] and got.seq_lost == 0


def test_stream_does_not_count_the_fns_events_as_lost():
    cap = c.LogicCapture.__new__(c.LogicCapture)
    cap.fn = 2
    link = FakeLink([[push(2, 0, 0, b"ab")], [push(2, 2, 2, b"cd")]])
    link.events = __import__("collections").deque([bytes([0x05]) + struct.pack("<HHB", 2, 1, 1)])   # seq 1 = an event
    got = cap.stream(link, nbytes=4)
    assert got.seq_lost == 0 and len(link.events) == 1


def test_stream_drops_pushes_of_another_generation():
    """oep-if-capture §3.4: a streaming data frame carries its generation; one left over from the start before is not
    this capture's (the frame's len keeps the TLV apart from the data)."""
    cap = c.LogicCapture.__new__(c.LogicCapture)
    cap.fn, cap.generation = 2, 5
    link = FakeLink([[push(2, 0, 90, b"old", generation=4), push(2, 1, 0, b"ab", generation=5)],
                     [push(2, 2, 2, b"cd", generation=5) + b"\x55\x01\x00\x00"]])    # an unknown TLV after it: skipped
    got = cap.stream(link, nbytes=4)
    assert bytes(got.data) == b"abcd" and got.start == 0 and got.stale == 1 and got.gaps == [] and got.seq_lost == 0
    assert c.unpack_push(push(9, 3, 7, b"x", 2)) == (9, 3, 7, b"x", 2)


def test_read_spans_frames_without_the_header_leaking_into_the_data():
    """read() splits a long read into frame-sized ones, each naming the generation; an answer is position(u64) flags(u8)
    len(u32) data [TLV]."""
    from test_target_parts import ScriptedHost, ok
    stream = bytes(range(256)) * 12                                   # 3072 bytes: more than one 1024-byte frame
    def read(p):
        g, pos, n = struct.unpack("<IQI", p[:16])
        assert g == 7
        data = stream[pos:pos + n]
        return ok(struct.pack("<QBI", pos, 0, len(data)) + data + b"\x44\x01\x00\x00")   # a TLV after the data
    hst = ScriptedHost({(21, c.LogicCapture.READ): read})
    hst._fns[c.LogicCapture.NAME] = 21
    hst._revisions[21] = 1
    lc = c.LogicCapture(hst)
    lc.generation = 7
    assert lc.read(100, 2500) == stream[100:2600]


def test_sigrok_file_takes_sixteen_channels_in_two_bytes(tmp_path):
    import zipfile
    lc = logic(16, list(range(16)))
    lc.config.rate = Fraction(20_000_000)
    samples = [0x8001, 0x0102]
    path = tmp_path / "x.sr"
    lc.to_sr(str(path), b"".join(v.to_bytes(2, "little") for v in samples), 2)
    with zipfile.ZipFile(path) as z:
        meta = z.read("metadata").decode()
        assert "unitsize=2" in meta and "total probes=16" in meta and "samplerate=20000000 Hz" in meta
        assert z.read("logic-1-1") == bytes([0x01, 0x80, 0x02, 0x01])
