# SPDX-License-Identifier: MIT
"""Reading a capture's data formats beyond 0 (oep-if-capture §1): format 1, the logic blocks (§1.3).

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

from . import host as h

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


def bits_list(bits: bytes, n: int) -> list[int]:
    """Bits as read by read_logic_blocks -> a list of n levels."""
    return [_bit(bits, i) for i in range(n)]


def worst_bytes(R: int, event_d: list[int], block: int) -> int:
    """The most bytes one block of L samples takes (§1.3): 2 ceil(E/8) + 4 + L R / 8 + sum min(L/8, (L/D) 4)."""
    E = len(event_d)
    return 2 * ((E + 7) // 8) + 4 + block * R // 8 + sum(min(block // 8, block // d * EVENT_BYTES) for d in event_d)
