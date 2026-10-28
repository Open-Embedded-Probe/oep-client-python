# SPDX-License-Identifier: MIT
"""Reading a capture's data formats beyond 0 (oep-if-capture §1): format 1, the logic blocks (§1.3), and format 2, the
analog blocks (§1.4).

A segment of format 1 is a run of blocks of L original samples (the last one r = samples mod L). Each block gives:

- every raw channel, and every events channel the probe carried raw in this block (fallback), as its bits: `bytes`
  with sample i at bit i mod 8 of byte i / 8 (numpy's unpackbits(..., bitorder="little") reads it) - lossless;
- every other events channel as its ticks: the block's start level and, for each tick of D samples where the level
  changed, (tick, the level after it, the changes in it - 127 meaning at least 127).

A fallback block is handed over as raw bits, not turned into events: the reader decides how to keep it.

With numpy installed the blocks are cut in Python from their headers only and the raw parts and events are read by
numpy (ticks, levels and counts are then arrays); without it, the same result in plain Python.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import NamedTuple

from . import host as h, registry as reg

try:
    import numpy as _np
except ImportError:                      # optional: the plain Python reader is the reference
    _np = None
EVENT_DTYPE = None if _np is None else _np.dtype([("tick", "<u2"), ("ch", "u1"), ("state", "u1")])

COUNT_SATURATED = 127                    # state bits 0-6 (§1.3): at least this many changes
EVENT_BYTES = 4                          # tick(u16) ch(u8) state(u8)


@dataclass
class EventChannel:
    """An events channel in one block (§1.3): the level before the block's first sample (`start_level`), and for each
    tick that changed, its number, the level after it and the changes in it (bit 0-6 of state; 127 saturated)."""
    d: int
    start_level: int
    ticks: list[int] = field(default_factory=list)
    levels: list[int] = field(default_factory=list)
    counts: list[int] = field(default_factory=list)


@dataclass
class LogicBlock:
    """One block of a format 1 segment: samples `first` .. `first + samples - 1` of the segment. `raw`: role -> bits
    for the raw channels and this block's fallback channels; `fallback`: the events channels carried raw here;
    `events`: role -> EventChannel for the others; `start_levels`: role -> the level before the block's first sample
    for every events channel, fallback ones included (§1.3 start_level: a segment's seam is counted from it)."""
    first: int
    samples: int
    raw: dict[int, bytes] = field(default_factory=dict)
    fallback: set[int] = field(default_factory=set)
    events: dict[int, EventChannel] = field(default_factory=dict)
    start_levels: dict[int, int] = field(default_factory=dict)


def _bit(data: bytes, j: int) -> int:
    return (data[j >> 3] >> (j & 7)) & 1


def _columns(raw: bytes, r: int, wb: int) -> list[bytes]:
    """The w_b-bit samples of a raw part as one bit string per column (§1 bit order)."""
    if wb == 1:
        return [raw[:(r + 7) // 8]]
    bits = format(int.from_bytes(raw, "little"), f"0{len(raw) * 8}b")[::-1]   # bit j at index j
    out = []
    for k in range(wb):
        col = bits[k:r * wb:wb]
        out.append(int(col[::-1], 2).to_bytes((r + 7) // 8, "little") if col else b"")
    return out


def _columns_np(raw: bytes, r: int, wb: int) -> list[bytes]:
    bits = _np.unpackbits(_np.frombuffer(raw, _np.uint8), bitorder="little")[:r * wb]
    packed = _np.packbits(bits.reshape(r, wb).T.reshape(-1), bitorder="little")   # columns one after another
    if r % 8:                                       # a partial last block: each column on its own byte boundary
        return [_np.packbits(bits.reshape(r, wb)[:, k], bitorder="little").tobytes() for k in range(wb)]
    step = r // 8
    return [packed[k * step:(k + 1) * step].tobytes() for k in range(wb)]


def read_logic_blocks(data: bytes, samples: int, raw_roles: list[int], event_d: dict[int, int],
                      block: int, *, use_numpy: bool | None = None) -> list[LogicBlock]:
    """A format 1 segment's bytes (§1.3) -> its blocks. `raw_roles`: the raw channels' roles, `event_d`: the events
    channels' role -> D (both as configured; read in role order), `block`: the answer's L. Bytes left over, or too
    few, raise ProtocolError. `use_numpy`: None uses it when installed."""
    fast = (_np is not None) if use_numpy is None else use_numpy
    raw_roles, ev = sorted(raw_roles), sorted(event_d)
    R, E = len(raw_roles), len(ev)
    nb = (E + 7) // 8
    out, pos, pending = [], 0, []
    for first in range(0, samples, block):
        r = min(block, samples - first)
        if pos + 2 * nb + 4 > len(data):
            raise h.ProtocolError(f"format 1: block at sample {first} cut short (header)")
        fb = [_bit(data[pos:pos + nb], e) for e in range(E)]
        start = [_bit(data[pos + nb:pos + 2 * nb], e) for e in range(E)]
        n = int.from_bytes(data[pos + 2 * nb:pos + 2 * nb + 4], "little")
        pos += 2 * nb + 4
        cols = raw_roles + [ev[e] for e in range(E) if fb[e]]
        wb = len(cols)
        rb = (r * wb + 7) // 8
        if pos + rb + EVENT_BYTES * n > len(data):
            raise h.ProtocolError(f"format 1: block at sample {first} cut short ({n} events)")
        b = LogicBlock(first, r, fallback={ev[e] for e in range(E) if fb[e]},
                       start_levels={ev[e]: start[e] for e in range(E)})
        for role, bits in zip(cols, (_columns_np if fast and wb > 1 else _columns)(data[pos:pos + rb], r, wb)):
            b.raw[role] = bits
        pos += rb
        for e in range(E):
            if not fb[e]:
                b.events[ev[e]] = EventChannel(event_d[ev[e]], start[e])
        if fast:
            pending.append((b, fb, pos, n, r))                      # its events: read below, all blocks at once
            pos += EVENT_BYTES * n
            out.append(b)
            continue
        for i in range(n):
            tick = int.from_bytes(data[pos:pos + 2], "little")
            ch, state = data[pos + 2], data[pos + 3]
            pos += EVENT_BYTES
            if ch >= E or fb[ch]:
                raise h.ProtocolError(f"format 1: an event of channel {ch} (events {E}, fallback {fb})")
            c = b.events[ev[ch]]
            if tick >= -(-r // c.d):
                raise h.ProtocolError(f"format 1: tick {tick} past the block's {r} samples (D {c.d})")
            c.ticks.append(tick)
            c.levels.append(state >> 7)
            c.counts.append(state & COUNT_SATURATED)
        out.append(b)
    if pos != len(data):
        raise h.ProtocolError(f"format 1: {len(data) - pos} bytes past the segment's {samples} samples")
    if pending:
        _events_np(data, pending, ev, E, event_d)
    return out


def _events_np(data: bytes, pending: list, ev: list[int], E: int, event_d: dict[int, int]) -> None:
    """Every block's events in one numpy read, split into views per block and channel (in channel order, §1.3)."""
    rows = _np.concatenate([_np.frombuffer(data, EVENT_DTYPE, count=n, offset=at) for _, _, at, n, _ in pending])
    if not len(rows):
        return
    counts = _np.array([n for _, _, _, n, _ in pending])
    block_of = _np.repeat(_np.arange(len(pending)), counts)
    key = block_of * 256 + rows["ch"]
    if (_np.diff(key) < 0).any():
        raise h.ProtocolError("format 1: events not in channel order (§1.3)")
    if int(rows["ch"].max()) >= E:
        raise h.ProtocolError(f"format 1: an event of a channel past {E}")
    d = _np.array([event_d[role] for role in ev])[rows["ch"]]
    r = _np.array([r for _, _, _, _, r in pending])[block_of]
    if ((rows["tick"].astype(_np.int64) * d) >= r).any():
        raise h.ProtocolError("format 1: a tick past its block (§1.3)")
    ticks, levels, changes = rows["tick"], rows["state"] >> 7, rows["state"] & COUNT_SATURATED
    bounds = _np.searchsorted(key, (_np.arange(len(pending))[:, None] * 256 + _np.arange(E + 1)[None, :]).ravel())
    bounds = bounds.reshape(len(pending), E + 1).tolist()
    for k, (b, fb, _, _, _) in enumerate(pending):
        at = bounds[k]
        for e, role in enumerate(ev):
            lo, hi = at[e], at[e + 1]
            if fb[e]:
                if hi != lo:
                    raise h.ProtocolError(f"format 1: events of channel {e}, carried raw in this block")
                continue
            c = b.events[role]
            c.ticks, c.levels, c.counts = ticks[lo:hi], levels[lo:hi], changes[lo:hi]


class EventColumn(NamedTuple):
    """An events channel over a whole segment (segment_columns): for each tick of D samples, counted from the segment's
    first sample, where the level changed - the tick (`quanta`), the level after it, the changes in it (127 meaning at
    least 127 where the probe sent events; a fallback block's are counted from its samples) - and the level before the
    segment's first sample (`start_level`). numpy arrays."""
    quanta: object
    levels: object
    counts: object
    start_level: int


def segment_columns(data: bytes, samples: int, raw_roles: list[int], event_d: dict[int, int],
                    block: int) -> dict[int, bytes | EventColumn]:
    """A format 1 segment (§1.3) read as columns without blocks: role -> a raw channel's bits (bytes, sample i at bit
    i mod 8; the bits past `samples` 0) or an events channel's EventColumn. A fallback block's samples of an events
    channel are turned into its ticks the way the probe counts them (the changes in each tick, the level at its end).
    The same checks as read_logic_blocks (ProtocolError). Needs numpy: the headers are followed in Python, the raw parts
    grouped by (fallback mask, r) and unpacked at once, every event row of the segment read at once - several times
    read_logic_blocks' speed (wireskein's spool: 60 blocks 54 -> 10 ms), for a host that keeps lines, not blocks."""
    if _np is None:
        raise RuntimeError("segment_columns needs numpy (read_logic_blocks reads without it)")
    np = _np
    raw_roles, ev = sorted(raw_roles), sorted(event_d)
    R, E = len(raw_roles), len(ev)
    nb = (E + 7) // 8
    hdr = 2 * nb + 4
    B = -(-samples // block)
    fbm = np.zeros(B, np.int64)           # fallback mask (bit e)
    stm = np.zeros(B, np.int64)           # start levels (bit e)
    nev = np.zeros(B, np.int64)
    praw = np.zeros(B, np.int64)
    pev = np.zeros(B, np.int64)
    pos, size = 0, len(data)
    fb_of: dict[int, int] = {}
    for k in range(B):                    # the headers: where each block's raw part and events are
        r = min(block, samples - k * block)
        if pos + hdr > size:
            raise h.ProtocolError(f"format 1: block at sample {k * block} cut short (header)")
        fb = int.from_bytes(data[pos:pos + nb], "little")
        n = int.from_bytes(data[pos + 2 * nb:pos + hdr], "little")
        wb = fb_of.get(fb)
        if wb is None:
            wb = fb_of[fb] = R + bin(fb).count("1")
        fbm[k], stm[k], nev[k] = fb, int.from_bytes(data[pos + nb:pos + 2 * nb], "little"), n
        praw[k] = pos + hdr
        pos += hdr + (r * wb + 7) // 8
        pev[k] = pos
        pos += EVENT_BYTES * n
        if pos > size:
            raise h.ProtocolError(f"format 1: block at sample {k * block} cut short ({n} events)")
    if pos != size:
        raise h.ProtocolError(f"format 1: {size - pos} bytes past the segment's {samples} samples")
    buf = np.frombuffer(data, np.uint8)
    rs = np.minimum(block, samples - np.arange(B) * block)
    first = np.arange(B) * block
    # raw columns: blocks grouped by (fallback mask, r); column c of a group is a (blocks, r) bit matrix
    bits = {role: np.zeros((B, block), np.uint8) for role in raw_roles}
    fbits: dict[int, list] = {}           # events role -> [(block indices, bit rows)] carried raw
    for fb, wb in fb_of.items():
        cols = raw_roles + [ev[e] for e in range(E) if fb >> e & 1]
        for r in np.unique(rs[fbm == fb]):
            r = int(r)
            ks = np.flatnonzero((fbm == fb) & (rs == r))
            rb = (r * wb + 7) // 8
            u = np.unpackbits(buf[praw[ks][:, None] + np.arange(rb)], axis=1, bitorder="little")[:, :r * wb]
            u = u.reshape(len(ks), r, wb)
            for c, role in enumerate(cols):
                if role in bits:
                    bits[role][ks, :r] = u[:, :, c]
                else:
                    fbits.setdefault(role, []).append((ks, u[:, :, c]))
    out: dict[int, bytes | EventColumn] = {
        role: np.packbits(bits[role].reshape(-1)[:samples], bitorder="little").tobytes() for role in raw_roles}
    # events: every block's rows at once
    tot = int(nev.sum())
    bid = np.repeat(np.arange(B), nev)
    at = np.repeat(pev - EVENT_BYTES * (np.cumsum(nev) - nev), nev) + EVENT_BYTES * np.arange(tot)
    rows = buf[at[:, None] + np.arange(EVENT_BYTES)] if tot else np.zeros((0, EVENT_BYTES), np.uint8)
    tick = rows[:, 0].astype(np.int64) | rows[:, 1].astype(np.int64) << 8
    ch, state = rows[:, 2].astype(np.int64), rows[:, 3]
    if tot:
        if (np.diff(bid * 256 + ch) < 0).any():
            raise h.ProtocolError("format 1: events not in channel order (§1.3)")
        if int(ch.max()) >= E:
            raise h.ProtocolError(f"format 1: an event of a channel past {E}")
        if ((fbm[bid] >> ch) & 1).any():
            raise h.ProtocolError("format 1: events of a channel carried raw in its block")
        if (tick * np.array([event_d[role] for role in ev])[ch] >= rs[bid]).any():
            raise h.ProtocolError("format 1: a tick past its block (§1.3)")
    for e, role in enumerate(ev):
        d = event_d[role]
        sel = ch == e
        q = first[bid[sel]] // d + tick[sel]
        lv = (state[sel] >> 7).astype(np.uint8)
        ct = (state[sel] & COUNT_SATURATED).astype(np.int64)
        if role in fbits:                 # counted from the samples, the way the probe counts (§1.3)
            qs, ls, cs = [q], [lv], [ct]
            for ks, u in fbits[role]:
                r = u.shape[1]
                prev = np.concatenate((((stm[ks] >> e) & 1).astype(np.uint8)[:, None], u[:, :-1]), axis=1)
                nt = -(-r // d)
                pad = np.zeros((len(ks), nt * d), np.int64)
                pad[:, :r] = u != prev
                per = pad.reshape(len(ks), nt, d).sum(-1)
                bi, t = np.nonzero(per)
                qs.append(first[ks][bi] // d + t)
                ls.append(u[bi, np.minimum((t + 1) * d, r) - 1])
                cs.append(per[bi, t])
            q, lv, ct = np.concatenate(qs), np.concatenate(ls), np.concatenate(cs)
            o = np.argsort(q, kind="stable")
            q, lv, ct = q[o], lv[o], ct[o]
        out[role] = EventColumn(q, lv, ct, int(stm[0] >> e & 1) if B else 0)
    return out


def bits_list(bits: bytes, n: int) -> list[int]:
    """Bits as read by read_logic_blocks -> a list of n levels."""
    return [_bit(bits, i) for i in range(n)]


def worst_bytes(R: int, event_d: list[int], block: int) -> int:
    """The most bytes one block of L samples takes (§1.3): 2 ceil(E/8) + 4 + L R / 8 + sum min(L/8, (L/D) 4)."""
    E = len(event_d)
    return 2 * ((E + 7) // 8) + 4 + block * R // 8 + sum(min(block // 8, block // d * EVENT_BYTES) for d in event_d)


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
