# SPDX-License-Identifier: MIT
"""Reading a capture's data formats beyond 0 (oep-if-capture §1): format 1, the logic blocks (§1.3), and format 2, the
analog blocks (§1.4).

A segment of format 1 is a run of blocks of L original samples (the last one r = samples mod L). Each channel is carried
raw (every sample) or reduced over intervals of D samples, with a fixed number of bits an interval:

- raw: its bits, `bytes` with sample i at bit i mod 8 of byte i / 8 (numpy's unpackbits(..., bitorder="little") reads it);
- sample (the interval's first sample), any_high (the interval's OR), any_low (its AND): one bit an interval;
- change_latch: two bits an interval - bit 0 the interval's last level, bit 1 whether it changed (from the sample
  before) - and, at each block's start, the level its first sample is compared with.

A block's size is fixed by the configuration, so a segment's bytes follow from its samples (segment_bytes).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import NamedTuple

from . import host as h, registry as reg

try:
    import numpy as _np
except ImportError:                      # optional: the plain Python reader is the reference
    _np = None

LOGIC_CARRIAGE = reg.FIXTURE_LOGIC.enum["logic_carriage"]
RAW, SAMPLE, ANY_HIGH, ANY_LOW, CHANGE_LATCH = (LOGIC_CARRIAGE[k] for k in ("raw", "sample", "any_high", "any_low",
                                                                            "change_latch"))
REDUCTIONS = {name: v for name, v in LOGIC_CARRIAGE.items() if v != RAW}   # name -> carriage


def value_bits(kind: int) -> int:
    """An interval's bits for a reduction (§1.3)."""
    return 2 if kind == CHANGE_LATCH else 1


def block_bytes(raw: int, reduced: dict[int, tuple[int, int]], r: int, width: int | None = None) -> int:
    """The bytes one block of r samples takes (§1.3): the raw part (r samples of the layout's w bits: `width`, R when
    None), then the reduced part (a start bit per change_latch channel and every interval's value). `reduced`: role ->
    (carriage, D)."""
    bits = sum(1 for k, _ in reduced.values() if k == CHANGE_LATCH)
    bits += sum(-(-r // d) * value_bits(k) for k, d in reduced.values())
    w = raw if width is None else width
    return (r * w + 7) // 8 + (bits + 7) // 8


def segment_bytes(samples: int, raw: int, reduced: dict[int, tuple[int, int]], block: int,
                  width: int | None = None) -> int:
    """A format 1 segment's bytes: whole blocks of L and the last one."""
    whole, rest = divmod(samples, block)
    return whole * block_bytes(raw, reduced, block, width) + (block_bytes(raw, reduced, rest, width) if rest else 0)


@dataclass
class LogicBlock:
    """One block of a format 1 segment: samples `first` .. `first + samples - 1` of the segment. `raw`: role -> bits;
    `values`: role -> the reduced channel's interval values (ints, change_latch's bit 0 the level, bit 1 the change);
    `starts`: role -> change_latch's level before the block's first sample."""
    first: int
    samples: int
    raw: dict[int, bytes] = field(default_factory=dict)
    values: dict[int, list[int]] = field(default_factory=dict)
    starts: dict[int, int] = field(default_factory=dict)


def _bit(data: bytes, j: int) -> int:
    return (data[j >> 3] >> (j & 7)) & 1


def _uint(data: bytes, j: int, n: int) -> int:
    return sum(_bit(data, j + k) << k for k in range(n))


def _columns(raw: bytes, r: int, wb: int) -> list[bytes]:
    """The wb-bit samples of a raw part as one bit string per column (§1 bit order)."""
    if wb == 1:
        return [raw[:(r + 7) // 8]]
    bits = format(int.from_bytes(raw, "little"), f"0{len(raw) * 8}b")[::-1]   # bit j at index j
    out = []
    for k in range(wb):
        col = bits[k:r * wb:wb]
        out.append(int(col[::-1], 2).to_bytes((r + 7) // 8, "little") if col else b"")
    return out


def read_logic_blocks(data: bytes, samples: int, raw_roles: list[int], reduced: dict[int, tuple[int, int]],
                      block: int, width: int | None = None, positions: list[int] | None = None) -> list[LogicBlock]:
    """A format 1 segment's bytes (§1.3) -> its blocks. `raw_roles`: the raw channels' roles, `reduced`: role ->
    (carriage, D) for the others (both as configured; read in role order), `block`: the answer's L, `width` /
    `positions`: the answer's layout w and pos (R and 0 .. R-1 when None). Bytes left over or too few raise
    ProtocolError."""
    raw_roles, red = sorted(raw_roles), sorted(reduced)
    R = len(raw_roles)
    w = R if width is None else width
    pos_of = list(range(R)) if positions is None else list(positions)
    latches = [role for role in red if reduced[role][0] == CHANGE_LATCH]
    out, pos = [], 0
    for first in range(0, samples, block):
        r = min(block, samples - first)
        size = block_bytes(R, reduced, r, w)
        if pos + size > len(data):
            raise h.ProtocolError(f"format 1: block at sample {first} cut short")
        rb = (r * w + 7) // 8
        b = LogicBlock(first, r)
        cols = _columns(data[pos:pos + rb], r, w) if R else []
        for j, role in enumerate(raw_roles):
            b.raw[role] = cols[pos_of[j]]
        part = data[pos + rb:pos + size]
        at = 0
        for role in latches:
            b.starts[role] = _bit(part, at)
            at += 1
        for role in red:
            kind, d = reduced[role]
            nb = value_bits(kind)
            b.values[role] = [_uint(part, at + j * nb, nb) for j in range(-(-r // d))]
            at += -(-r // d) * nb
        out.append(b)
        pos += size
    if pos != len(data):
        raise h.ProtocolError(f"format 1: {len(data) - pos} bytes past the segment's {samples} samples")
    return out


class ReducedColumn(NamedTuple):
    """A reduced channel over a whole segment (segment_columns): its carriage and D, its interval values packed as on the
    wire (`b` bits each from bit 0, interval k at bit k b; `n` intervals, the last one partial when D does not divide
    the segment) and, for change_latch, the level before the segment's first sample (`start_level`; else 0)."""
    kind: int
    d: int
    data: bytes
    n: int
    start_level: int


def segment_columns(data: bytes, samples: int, raw_roles: list[int], reduced: dict[int, tuple[int, int]],
                    block: int, width: int | None = None,
                    positions: list[int] | None = None) -> dict[int, bytes | ReducedColumn]:
    """A format 1 segment (§1.3) read as columns without blocks: role -> a raw channel's bits (bytes, sample i at bit
    i mod 8; the bits past `samples` 0) or a reduced channel's ReducedColumn. A change_latch block's start other than the
    level its interval before ended on is a broken stream (ProtocolError), as are bytes left over or too few. With numpy
    every whole block is unpacked at once; without it, the same through read_logic_blocks."""
    raw_roles, red = sorted(raw_roles), sorted(reduced)
    R = len(raw_roles)
    w = R if width is None else width
    pos_of = list(range(R)) if positions is None else list(positions)
    expect = segment_bytes(samples, R, reduced, block, w)
    if len(data) != expect:
        raise h.ProtocolError(f"format 1: {len(data)} bytes for {samples} samples ({expect} expected)")
    if _np is None:
        return _columns_plain(data, samples, raw_roles, reduced, block, w, pos_of)
    np = _np
    whole, rest = divmod(samples, block)
    full = block_bytes(R, reduced, block, w)
    rb = block * w // 8
    buf = np.frombuffer(data, np.uint8)
    blocks = buf[:whole * full].reshape(whole, full)
    tail = _columns_plain(data[whole * full:], rest, raw_roles, reduced, block, w, pos_of) if rest else None
    out: dict[int, bytes | ReducedColumn] = {}
    if R:
        bits = np.unpackbits(blocks[:, :rb], axis=1, bitorder="little").reshape(-1, w)
        for j, role in enumerate(raw_roles):
            col = bits[:, pos_of[j]]
            if rest:
                col = np.concatenate((col, np.unpackbits(np.frombuffer(tail[role], np.uint8), bitorder="little")[:rest]))
            out[role] = np.packbits(col, bitorder="little").tobytes()
    ub = np.unpackbits(blocks[:, rb:], axis=1, bitorder="little")   # (blocks, reduced part's bits)
    latches = [role for role in red if reduced[role][0] == CHANGE_LATCH]
    off = len(latches)
    for role in red:
        kind, d = reduced[role]
        nb = value_bits(kind)
        width = block // d * nb
        vb = ub[:, off:off + width]                                   # this channel's value bits, block by block
        off += width
        start = 0
        if kind == CHANGE_LATCH:
            si = latches.index(role)
            starts = ub[:, si]
            if whole:
                start = int(starts[0])
                lasts = vb[:, width - 2]                                # each block's last interval's level
                bad = np.flatnonzero(starts[1:] != lasts[:-1])
                if len(bad):
                    k = int(bad[0]) + 1
                    raise h.ProtocolError(f"format 1: role {role}'s block at sample {k * block} starts at level "
                                          f"{int(starts[k])}, its interval before ended at {int(lasts[k - 1])}")
        flat = vb.reshape(-1)
        n = whole * (block // d)
        if rest:
            t = tail[role]
            if kind == CHANGE_LATCH and whole and t.start_level != int(flat[-2]):
                raise h.ProtocolError(f"format 1: role {role}'s block at sample {whole * block} starts at level "
                                      f"{t.start_level}, its interval before ended at {int(flat[-2])}")
            if not whole:
                start = t.start_level
            flat = np.concatenate((flat, np.unpackbits(np.frombuffer(t.data, np.uint8), bitorder="little")[:t.n * nb]))
            n += t.n
        out[role] = ReducedColumn(kind, d, np.packbits(flat, bitorder="little").tobytes(), n, start)
    return out


def _columns_plain(data: bytes, samples: int, raw_roles: list[int], reduced: dict[int, tuple[int, int]],
                   block: int, width: int | None = None,
                   positions: list[int] | None = None) -> dict[int, bytes | ReducedColumn]:
    """segment_columns without numpy (and its partial last block): through read_logic_blocks."""
    raw_roles, red = sorted(raw_roles), sorted(reduced)
    levels = {role: 0 for role in raw_roles}
    vals = {role: [] for role in red}
    starts: dict[int, int] = {}
    for b in read_logic_blocks(data, samples, raw_roles, reduced, block, width, positions):
        for role in raw_roles:
            levels[role] |= int.from_bytes(b.raw[role], "little") << b.first
        for role in red:
            if role in b.starts:
                if not vals[role]:
                    starts[role] = b.starts[role]
                elif b.starts[role] != vals[role][-1] & 1:
                    raise h.ProtocolError(f"format 1: role {role}'s block at sample {b.first} starts at level "
                                          f"{b.starts[role]}, its interval before ended at {vals[role][-1] & 1}")
            vals[role] += b.values[role]
    out: dict[int, bytes | ReducedColumn] = {
        role: levels[role].to_bytes((samples + 7) // 8, "little") for role in raw_roles}
    for role in red:
        kind, d = reduced[role]
        nb = value_bits(kind)
        v = 0
        for k, x in enumerate(vals[role]):
            v |= x << (k * nb)
        n = len(vals[role])
        out[role] = ReducedColumn(kind, d, v.to_bytes((n * nb + 7) // 8, "little"), n, starts.get(role, 0))
    return out


def bits_list(bits: bytes, n: int) -> list[int]:
    """Bits as read by read_logic_blocks -> a list of n levels."""
    return [_bit(bits, i) for i in range(n)]


# ---- format 2, the analog blocks (§1.4) ----------------------------------------------------------------------------
PACKED, MINMAX, SUM = (reg.FIXTURE_ANALOG.enum["analog_carriage"][k] for k in ("packed", "minmax", "sum"))


@dataclass
class AnalogChannel:
    """One channel's part of a format 2 block (§1.4): `values` (packed, lossless), or per interval of D (the last one
    partial: its points from the block's r) `mins` / `maxs` and / or `sums` (summaries)."""
    kind: int
    d: int
    values: list[int] = field(default_factory=list)
    mins: list[int] = field(default_factory=list)
    maxs: list[int] = field(default_factory=list)
    sums: list[int] = field(default_factory=list)

    def points(self, r: int) -> list[int]:
        """The original samples each interval covers (a sum over its points is their mean)."""
        return [min(self.d, r - lo) for lo in range(0, r, self.d)]


@dataclass
class AnalogBlock:
    """One block of a format 2 segment: samples `first` .. `first + samples - 1`, role -> its part."""
    first: int
    samples: int
    channels: dict[int, AnalogChannel] = field(default_factory=dict)


def _part_bits(r: int, kind: int, d: int, b: int) -> int:
    if kind == PACKED:
        return r * b
    per = (2 * b if kind & MINMAX else 0) + (b + (d - 1).bit_length() if kind & SUM else 0)
    return -(-r // d) * per


def analog_block_bytes(r: int, order: list[int], carriage: dict[int, tuple[int, int]], b: int) -> int:
    """The bytes of one format 2 block of r samples: every channel's part, each ending on a byte (§1.4)."""
    return sum((_part_bits(r, *carriage.get(k, (PACKED, 1)), b) + 7) // 8 for k in order)


def analog_segment_bytes(samples: int, order: list[int], carriage: dict[int, tuple[int, int]], block: int,
                         b: int) -> int:
    """A format 2 segment's bytes: whole blocks of L, the last of what is left (§1.4; a fixed size)."""
    whole, rest = divmod(samples, block)
    return (whole * analog_block_bytes(block, order, carriage, b)
            + (analog_block_bytes(rest, order, carriage, b) if rest else 0))


def read_analog_blocks(data: bytes, samples: int, order: list[int], carriage: dict[int, tuple[int, int]],
                       block: int, b: int) -> list[AnalogBlock]:
    """A format 2 segment's bytes (§1.4) -> its blocks. `order`: the layout's order, `carriage`: role -> (kind, d) as
    configured (the others packed), `block`: the answer's L, `b`: the layout's b. Bytes left over, or too few, raise
    ProtocolError."""
    if len(data) != analog_segment_bytes(samples, order, carriage, block, b):
        raise h.ProtocolError(f"format 2: {len(data)} bytes for {samples} samples "
                              f"({analog_segment_bytes(samples, order, carriage, block, b)} expected)")
    out, pos = [], 0
    for first in range(0, samples, block):
        r = min(block, samples - first)
        blk = AnalogBlock(first, r)
        for k in order:
            kind, d = carriage.get(k, (PACKED, 1))
            nbytes = (_part_bits(r, kind, d, b) + 7) // 8
            acc, at = int.from_bytes(data[pos:pos + nbytes], "little"), 0
            pos += nbytes

            def take(bits: int) -> int:
                nonlocal at
                v = acc >> at & ((1 << bits) - 1)
                at += bits
                return v
            c = AnalogChannel(kind, d)
            if kind == PACKED:
                c.values = [take(b) for _ in range(r)]
            else:
                for _ in range(-(-r // d)):
                    if kind & MINMAX:
                        c.mins.append(take(b))
                        c.maxs.append(take(b))
                    if kind & SUM:
                        c.sums.append(take(b + (d - 1).bit_length()))
            blk.channels[k] = c
        out.append(blk)
    return out


# ---- format 3, the timed states (§3.8) -----------------------------------------------------------------------------
ESCAPE = 0xFFFFFFFF                      # a delta that only moves time on: its state is no sample


def read_state_timed(data: bytes, samples: int, w: int) -> list[tuple[int, int]]:
    """A format 3 segment's bytes -> (ticks from start, state) for each of its `samples` samples: the first delta
    counts from start, the others from the sample before, an escape (0xFFFFFFFF) moves time on without a sample.
    Bytes left over, or too few, raise ProtocolError."""
    size = 4 + (w + 7) // 8
    out, pos, ticks = [], 0, 0
    while len(out) < samples:
        if pos + size > len(data):
            raise h.ProtocolError(f"format 3: {len(out)} of {samples} samples in {len(data)} bytes")
        delta = int.from_bytes(data[pos:pos + 4], "little")
        ticks += delta
        if delta != ESCAPE:
            out.append((ticks, int.from_bytes(data[pos + 4:pos + size], "little") & ((1 << w) - 1)))
        pos += size
    if pos != len(data):
        raise h.ProtocolError(f"format 3: {len(data) - pos} bytes past the segment's {samples} samples")
    return out


def state_timed_length(data: bytes, samples: int, w: int) -> int:
    """The bytes `samples` samples of format 3 take at the start of `data` (escapes included)."""
    size, pos, n = 4 + (w + 7) // 8, 0, 0
    while n < samples:
        if pos + size > len(data):
            raise h.ProtocolError("format 3: the segment's bytes end inside its entries")
        n += int.from_bytes(data[pos:pos + 4], "little") != ESCAPE
        pos += size
    return pos
