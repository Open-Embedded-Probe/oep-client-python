"""oep_client.capture: the configure answer and the §3.0 logic layout (oep-spec logic-capture.ja.md)."""

import struct
from fractions import Fraction

from oep_client import capture as c


def answer(*items):
    return b"".join(bytes([t, len(v)]) + v for t, v in items)


def test_configure_answer_is_read_into_actual_values():
    cfg = c._config(answer((c.ACTUAL_RATE, struct.pack("<II", 7000000, 1)), (c.LAYOUT, bytes([4, 3, 0, 1, 2])),
                           (c.ACTUAL_SAMPLES, struct.pack("<I", 1000)), (c.IGNORED, bytes([c.TRIGGER]))), analog=False)
    assert cfg.rate == Fraction(7000000) and cfg.width == 4 and cfg.positions == [0, 1, 2]
    assert cfg.samples == 1000 and cfg.bytes == 500 and cfg.ignored == [c.TRIGGER]


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
    s = c.Segment.unpack(struct.pack("<IQIQIIB", 0, 0, 200192, 123000, 50, 0xFFFFFFFF, 0))
    assert s.trigger_index is None and s.samples == 200192


class FakeLink:
    """pushes arrive in batches, one batch per pump()"""

    def __init__(self, batches):
        self.pushes, self.batches = __import__("collections").deque(), list(batches)
        self.events = __import__("collections").deque()

    def pump(self, timeout=0.0, until_one=False):
        if self.batches:
            self.pushes.extend(self.batches.pop(0))
        return 0


def push(fn, seq, position, data):
    return bytes([0x06]) + struct.pack("<HHQ", fn, seq, position) + data   # core header + position(u64)


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


def test_read_spans_frames_without_the_header_leaking_into_the_data():
    """read() splits a long read into frame-sized ones; each answer is position(u64) flags(u8) data."""
    from test_target_parts import ScriptedHost, ok
    stream = bytes(range(256)) * 12                                   # 3072 bytes: more than one 1024-byte frame
    def read(p):
        pos, n = struct.unpack("<QI", p[:12])
        return ok(struct.pack("<QB", pos, 0) + stream[pos:pos + n])
    hst = ScriptedHost({(21, c.LogicCapture.READ): read})
    hst._fns[c.LogicCapture.NAME] = 21
    hst._revisions[21] = 1
    lc = c.LogicCapture(hst)
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
