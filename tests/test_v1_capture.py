"""oep_client.v1.capture: the configure answer and the §3.0 logic layout (oep-spec logic-capture.ja.md)."""

import struct
from fractions import Fraction

from oep_client.v1 import capture as c


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
    s = c.Segment.unpack(struct.pack("<IIIIIB", 0, 0, 200192, 123, 0xFFFFFFFF, 0))
    assert s.trigger_index is None and s.samples == 200192
