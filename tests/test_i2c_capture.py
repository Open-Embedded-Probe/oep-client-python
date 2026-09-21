from oep_client.i2c_capture import Segment, decode_i2c_address, unpack_rmt_symbols


def word(level0, duration0, level1, duration1):
    return duration0 | (level0 << 15) | (duration1 << 16) | (level1 << 31)


def test_expands_rmt_words_without_zero_duration_tail():
    assert unpack_rmt_symbols((word(0, 3, 1, 4), word(0, 2, 1, 0))) == (
        Segment(0, 3, 0), Segment(3, 7, 1), Segment(7, 9, 0),
    )


def test_decodes_address_and_nack_from_midpoint_samples():
    # Nine SCL low/high cycles, ten ticks each. 0x84 is address 0x42 write;
    # final SDA high denotes NACK.
    clock = tuple(word(0, 5, 1, 5) for _ in range(9))
    # Bits sampled at ticks 7,17,... are 1,0,0,0,0,1,0,0,1. Build a data
    # segment per complete SCL cycle, packing two level intervals per word.
    bits = (1, 0, 0, 0, 0, 1, 0, 0, 1)
    data = tuple(word(bit, 10, bit, 0) for bit in bits)
    result = decode_i2c_address(clock, data)
    assert result is not None
    assert (result.address, result.read, result.ack) == (0x42, False, False)
