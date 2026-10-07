# SPDX-License-Identifier: MIT
"""oep.fixture.logic's multirate (oep-spec interfaces/oep-if-capture.ja.md §5): every channel watched at one base rate,
each channel giving values at its own interval d, reduced by its policy. Pure codec, used by the client (capture.py)
and the virtual bench alike.

- `Multirate(role, policy, d, param)`: one role's configure TLV 0x60 (sent critical, 0xE0). A role sent none is
  policy sample, d 1 - a **D = 1 channel**, laid out by the answer's layout as §1.1 does; any other role is a
  **reduced channel**. param is sample's phase (0 <= phase < d) or any_active / edge_latch's active level (0 / 1).
- `Declared`: describe's 0x60 (policies, min_d, max_d, pow2). d = 1 sample is always accepted; min_d / max_d bound
  d >= 2 only (2 <= min_d <= max_d, oep-spec dd5a886).
- `Layout(w, pos, L, reduced)`: the answer's layout of the D = 1 channels (C may be 0) and block L, with the reduced
  channels in role order: the stream of one segment is blocks of L base samples (the grid restarts at each segment),
  each the D = 1 part (r*w bits, 0 to a byte) then the reduced part (the values bit-packed in role order, 0 to a byte);
  the last block of a short segment holds r = samples mod L base samples and only the values that exist (§5.5).
  `decode(data, samples)` and `encode(level, samples)` read and make it.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field
from typing import Callable

from . import registry as reg

_CAP = reg.FIXTURE_LOGIC
POLICY = dict(_CAP.enum["multirate_policy"])                    # sample 0, any_active 1, edge_latch 2; 3 onwards reserved
POLICY_NAME = {v: k for k, v in POLICY.items()}
SAMPLE, ANY_ACTIVE, EDGE_LATCH = POLICY["sample"], POLICY["any_active"], POLICY["edge_latch"]
TAG = _CAP.tlv["configure"]["multirate"]                       # 0x60 (sent 0xE0)
BLOCK = _CAP.tlv["configure_answer"]["block"]                  # 0x60: L(u32)
DECLARED = _CAP.tlv["describe"]["multirate"]                   # 0x60: policies min_d max_d pow2
TLV = struct.Struct("<BBII")                                   # role policy d param


@dataclass(frozen=True)
class Multirate:
    """One role's multirate TLV (§5.2). policy SAMPLE with d 1 is a D = 1 channel (as a role sent nothing)."""
    role: int
    policy: int = SAMPLE
    d: int = 1
    param: int = 0                       # sample: phase; any_active / edge_latch: the active level

    @property
    def reduced(self) -> bool:
        return not (self.policy == SAMPLE and self.d == 1)

    @property
    def bits(self) -> int:
        """The bits of one value: 2 for edge_latch (bit 0 the level, bit 1 the edge), else 1."""
        return 2 if self.policy == EDGE_LATCH else 1

    def value(self) -> bytes:
        return TLV.pack(self.role, self.policy, self.d, self.param)

    @classmethod
    def unpack(cls, v: bytes) -> "Multirate":
        return cls(*TLV.unpack(v))

    def malformed(self) -> str | None:
        """Why the probe answers rejected malformed (§5.2), or None."""
        if self.d == 0:
            return "d 0"
        if self.policy == SAMPLE and self.param >= self.d:
            return f"phase {self.param} not below d {self.d}"
        if self.policy in (ANY_ACTIVE, EDGE_LATCH) and (self.d == 1 or self.param > 1):
            return f"{POLICY_NAME[self.policy]} with d {self.d} and param {self.param} (d 2 or more, param 0 or 1)"
        return None

    def count(self, samples: int) -> int:
        """The values a segment of `samples` base samples has (§5.4: only those whose base samples are all in it)."""
        if self.policy == SAMPLE:
            return max(0, -(-(samples - self.param) // self.d))
        return samples // self.d

    def value_at(self, level: Callable[[int], int], k: int) -> int:
        """Value k from the channel's levels by base sample of its segment (level(n), n from 0; §5.4)."""
        d, a = self.d, self.param
        if self.policy == SAMPLE:
            return level(k * d + a)
        span = range(k * d, (k + 1) * d)
        if self.policy == ANY_ACTIVE:
            return a if any(level(n) == a for n in span) else 1 - a
        edge = any(n >= 1 and level(n - 1) != a and level(n) == a for n in span)   # from the previous base sample
        return level((k + 1) * d - 1) | (int(edge) << 1)

    def values(self, levels: list[int] | str) -> list[int]:
        """Every value of a segment from its levels by base sample."""
        lv = [int(x) for x in levels]
        return [self.value_at(lv.__getitem__, k) for k in range(self.count(len(lv)))]


@dataclass(frozen=True)
class Declared:
    """describe 0x60 (§5.1): the policies (bit p: policy p), the d range for d >= 2, pow2 (powers of 2 only)."""
    policies: int
    min_d: int
    max_d: int
    pow2: bool

    @classmethod
    def unpack(cls, v: bytes) -> "Declared":
        policies, lo, hi, pow2 = struct.unpack_from("<IIIB", v)
        return cls(policies, lo, hi, bool(pow2))

    def value(self) -> bytes:
        return struct.pack("<IIIB", self.policies, self.min_d, self.max_d, int(self.pow2))

    def accepts_policy(self, policy: int) -> bool:
        return policy in POLICY_NAME and bool(self.policies >> policy & 1)

    def accepts_d(self, d: int) -> bool:
        """d 1 is always accepted (a sample of d 1 is a D = 1 channel); d >= 2 within min_d .. max_d (min_d read as at
        least 2), only powers of 2 with pow2."""
        if d == 1:
            return True
        return max(2, self.min_d) <= d <= self.max_d and (not self.pow2 or d & (d - 1) == 0)

    def refuses(self, m: Multirate) -> str | None:
        """Why the probe answers rejected unsupported (§5.2), or None."""
        if not self.accepts_policy(m.policy):
            return f"policy {m.policy} not declared"
        if not self.accepts_d(m.d):
            return f"d {m.d} not declared ({self.min_d}-{self.max_d}{', powers of 2' if self.pow2 else ''})"
        return None


def check(specs: list[Multirate], declared: Declared | None) -> list[Multirate]:
    """What the client checks before sending (ValueError): malformed TLVs, a role twice, and - against the fn's
    describe - a policy or d it does not declare, or no multirate at all. -> the specs in role order."""
    if declared is None:
        raise ValueError("multirate: this fn's describe declares no multirate (oep-if-capture §5.1)")
    roles = [s.role for s in specs]
    if len(set(roles)) != len(roles):
        raise ValueError("multirate: a role given twice (oep-if-capture §5.2)")
    for s in specs:
        why = s.malformed() or declared.refuses(s)
        if why:
            raise ValueError(f"multirate role {s.role}: {why} (oep-if-capture §5.1, §5.2)")
    return sorted(specs, key=lambda s: s.role)


def _put(out: bytearray, bit: int, value: int, n: int) -> None:
    for j in range(n):
        if value >> j & 1:
            out[(bit + j) >> 3] |= 1 << ((bit + j) & 7)


def _get(data: bytes, bit: int, n: int) -> int:
    return sum(((data[(bit + j) >> 3] >> ((bit + j) & 7)) & 1) << j for j in range(n))


@dataclass
class Decoded:
    """One segment decoded: `d1[k]` the k-th D = 1 channel's level at every base sample (k in role order, pos[k] of the
    layout); `reduced[role]` that reduced channel's values in order (edge_latch: bit 0 level, bit 1 edge)."""
    d1: list[list[int]] = field(default_factory=list)
    reduced: dict[int, list[int]] = field(default_factory=dict)


@dataclass
class Layout:
    """The data form of a multirate configuration (§5.3, §5.5)."""
    w: int
    pos: list[int]
    L: int
    reduced: list[Multirate]             # role order

    def __post_init__(self):
        self.reduced = sorted((s for s in self.reduced if s.reduced), key=lambda s: s.role)
        for s in self.reduced:
            if self.L % s.d:
                raise ValueError(f"block L {self.L} is not a multiple of role {s.role}'s d {s.d} (oep-if-capture §5.3)")

    def _parts(self, r: int, samples: int, b: int) -> tuple[int, list[tuple[Multirate, int, int]]]:
        """D = 1 part's bytes and [(spec, first value, values)] of block b holding r base samples of a segment of
        `samples`."""
        d1 = (r * self.w + 7) // 8 if self.pos else 0
        out = []
        for s in self.reduced:
            per = self.L // s.d
            first = b * per
            out.append((s, first, max(0, min(first + per, s.count(samples)) - first)))
        return d1, out

    def block_bytes(self) -> int:
        """B: the bytes of a complete block (§5.1)."""
        return (self.L * self.w + 7) // 8 * bool(self.pos) + (sum(self.L // s.d * s.bits for s in self.reduced) + 7) // 8

    def segment_bytes(self, samples: int) -> int:
        full, r = divmod(samples, self.L)
        n = full * self.block_bytes()
        if r:
            d1, red = self._parts(r, samples, full)
            n += d1 + (sum(s.bits * k for s, _, k in red) + 7) // 8
        return n

    def decode(self, data: bytes, samples: int) -> Decoded:
        out = Decoded([[] for _ in self.pos], {s.role: [] for s in self.reduced})
        at = 0
        for b in range(-(-samples // self.L)):
            r = min(self.L, samples - b * self.L)
            d1, red = self._parts(r, samples, b)
            part = data[at:at + d1]
            for k, p in enumerate(self.pos):
                out.d1[k] += [(part[(i * self.w + p) >> 3] >> ((i * self.w + p) & 7)) & 1 for i in range(r)]
            at += d1
            bit = 0
            nbits = sum(s.bits * k for s, _, k in red)
            part = data[at:at + (nbits + 7) // 8]
            for s, _, k in red:
                out.reduced[s.role] += [_get(part, bit + j * s.bits, s.bits) for j in range(k)]
                bit += k * s.bits
            at += (nbits + 7) // 8
        if at > len(data):
            raise ValueError(f"multirate segment of {samples} base samples needs {at} bytes, got {len(data)}")
        return out

    def encode(self, level: Callable[[int, int], int], samples: int, d1_roles: list[int],
               blocks: range | None = None) -> bytes:
        """The stream of a segment of `samples` base samples (or of `blocks` of it), level(role, n) being role's level
        at base sample n of the segment; d1_roles[k] the role of the layout's k-th D = 1 channel."""
        out = bytearray()
        for b in blocks if blocks is not None else range(-(-samples // self.L)):
            r = min(self.L, samples - b * self.L)
            d1, red = self._parts(r, samples, b)
            part = bytearray(d1)
            for i in range(r):
                for k, p in enumerate(self.pos):
                    if level(d1_roles[k], b * self.L + i):
                        part[(i * self.w + p) >> 3] |= 1 << ((i * self.w + p) & 7)
            out += part
            nbits = sum(s.bits * k for s, _, k in red)
            part, bit = bytearray((nbits + 7) // 8), 0
            for s, first, k in red:
                lv = (lambda role: lambda n: level(role, n))(s.role)
                for j in range(k):
                    _put(part, bit, s.value_at(lv, first + j), s.bits)
                    bit += s.bits
            out += part
        return bytes(out)


def choose_block(specs: list[Multirate], unit: int = 32) -> int:
    """A block L divisible by every d: the least common multiple of `unit` and the reduced channels' d (the virtual
    bench's choice; L is the probe's, §5.3)."""
    return math.lcm(unit, *(s.d for s in specs if s.reduced))
