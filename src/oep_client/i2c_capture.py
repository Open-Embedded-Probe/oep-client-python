"""MCU-independent decoding of raw two-channel RMT I2C observations."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Segment:
    start: int
    end: int
    level: int


@dataclass(frozen=True)
class I2cObservation:
    address: int
    read: bool
    ack: bool
    sample_ticks: tuple[int, ...]


def unpack_rmt_symbols(words: tuple[int, ...]) -> tuple[Segment, ...]:
    """Expand ESP-IDF RMT words into a tick-relative level timeline."""
    cursor = 0
    segments = []
    for word in words:
        for shift, level_shift in ((0, 15), (16, 31)):
            duration = (word >> shift) & 0x7fff
            if not duration:
                continue
            level = (word >> level_shift) & 1
            segments.append(Segment(cursor, cursor + duration, level))
            cursor += duration
    if not segments:
        return ()
    return tuple(segments)


def _level_at(segments: tuple[Segment, ...], tick: int) -> int | None:
    for segment in segments:
        if segment.start <= tick < segment.end:
            return segment.level
    return None


def _rising_sample_ticks(clock: tuple[Segment, ...]) -> tuple[int, ...]:
    samples = []
    for previous, current in zip(clock, clock[1:]):
        if previous.level == 0 and current.level == 1:
            # Sampling in the middle of SCL high avoids the SDA setup edge.
            samples.append(current.start + (current.end - current.start) // 2)
    return tuple(samples)


def decode_i2c_address(clock_words: tuple[int, ...], data_words: tuple[int, ...]) \
        -> I2cObservation | None:
    """Decode the first complete address+ACK field, or return ``None``.

    Both streams must come from one explicit capture start. The streams are
    independently timestamped by RMT, so this intentionally reports no result
    if any midpoint is absent rather than inventing a synchronized value.
    """
    clock = unpack_rmt_symbols(clock_words)
    data = unpack_rmt_symbols(data_words)
    samples = _rising_sample_ticks(clock)
    if len(samples) < 9:
        return None
    bits = tuple(_level_at(data, tick) for tick in samples[:9])
    if any(bit is None for bit in bits):
        return None
    address_byte = sum(int(bits[index]) << (7 - index) for index in range(8))
    return I2cObservation(address_byte >> 1, bool(address_byte & 1),
                          not bool(bits[8]), samples[:9])
