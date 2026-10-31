"""Acquire: one capture from a probe, put together - the request in, the parts with their losses, times and the
probe's facts out.

    from oep_client import acquire
    res = acquire.capture_at("oep://30eda0ea068b", acquire.Request([("SDA", "GPIO47"), ("SCL", "x035.scl")], 20_000_000,
                                                                   200_000))
    res = acquire.capture(hst, req)          # on a session the caller holds (with ProbeLog, the target's control)
    for part in res.parts:                   # a timeline cut by its gaps, or one part per triggered segment
        part.tick0, part.meta["start_ns"], [c.name for c in part.channels]
    res.lost                                 # what was lost while capturing: any makes the capture a failure

Logic channels go to oep.fixture.logic, analog ones to oep.fixture.analog; with both, oep.fixture.capture-group
starts them together, a trigger on either track as the group's trigger source (capture §4.4). A line is named as a
script keeps it - the probe's channel name (fn 0's channel_name, core §7.5) or a port's line label (S.N, host guide
§5.3) - and looked up on the session (a channel number holds for one boot only); a track's channels are its plan roles
0..C-1 in the order given. The plans are released afterwards.

Modes: "oneshot" (the probe's memory), "repeat" (`segments` acquisitions read and released as they come: with a
trigger one part each, layout "segments"; without, a timeline), "stream" (for `seconds`, sent as taken,
put together on disk while it runs: a timeline, cut where data was lost).

Times: tick 0 of a part is its first logic sample, `start_ns` on the probe's clock; an analog channel starts at
t0_ticks = (its segment's start_ns - that) plus its skew_ns, in ticks. These are the probe's estimates; each track's
uncertainty is kept. ADC values are kept raw with the probe's linear conversion; the input range, reference, Vrefint
reading and factory calibration go along in `probe`, never applied.

Channels come as the probe gave them: a logic line as its samples (`Bits`, one bit each, sample i at bit i % 8 of
byte i // 8) or as its changes (`Changes`, capture format 1 events: per event a varint - its quantum minus the
previous event's, minus one - then a byte, bit 7 the level after and bits 0-6 the count of changes in the quantum,
127 meaning at least 127), an analog one as its raw values (`Analog`). numpy is used to put streams and format 1
blocks together.
"""

from __future__ import annotations

import array
import math
import struct
import sys
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

__all__ = ["Analog", "Bits", "Changes", "OnDisk", "Part", "Request", "Result", "capture", "capture_at"]

TRIGGERS = ("high", "low", "rise", "fall", "both")
ANALOG_TRIGGERS = ("high", "low", "rise", "fall")


@dataclass
class Request:
    channels: list[tuple[str, str]]          # logic: (name to record, the line): the probe's channel name (fn 0's
                                             # channel_name, GPIO47) or a port's line label (S.N, x035.swdio), in order
    rate: int                                # logic samples per second asked for (the probe may answer another)
    samples: int
    trigger: tuple | None = None             # (logic channel, one of TRIGGERS) or (analog channel, rise / fall / high /
                                             # low, volts); None: start at once
    pretrigger: int | None = None            # samples kept before the trigger
    arm: str = "pretrigger"                  # when the trigger is looked for: "pretrigger" (once it is there) or "now"
    timeout: float = 10.0                    # seconds to wait for the capture to finish
    analog: list[tuple[str, str, int | None]] = field(default_factory=list)   # (name, the line as above, frontend)
    analog_rate: int | None = None           # analog samples per second (default: `rate`)
    analog_samples: int | None = None        # default: as long as the logic capture
    resolution: dict[str, int] = field(default_factory=dict)   # logic lines as their changes (format 1): name -> D
    mode: str = "oneshot"                    # "oneshot", "stream" (for `seconds`) or "repeat"
    seconds: float | None = None             # stream: how long (as LogicCapture.stream(seconds=...))
    segments: int | None = None              # repeat: how many acquisitions
    on_loss: str = "stop"                    # stream: "stop" at the first loss, or "continue" to the end
    spool_dir: str | None = None             # stream: where its channels are put together on disk while it runs

    def __post_init__(self):
        names = [n for n, _ in self.channels] + [n for n, *_ in self.analog]
        if not names or len(set(names)) != len(names):
            raise ValueError(f"channel names must be given and differ: {names}")
        if self.trigger is not None:
            name, kind, *volts = self.trigger
            if name in [n for n, *_ in self.analog]:
                if kind not in ANALOG_TRIGGERS or len(volts) != 1:
                    raise ValueError(f"trigger {name}:{kind}: an analog channel takes {'/'.join(ANALOG_TRIGGERS)} and "
                                     f"the volts it crosses or stays beyond ({name}:fall:2.9)")
            elif name not in [n for n, _ in self.channels] or kind not in TRIGGERS or volts:
                raise ValueError(f"trigger {name}:{kind}: the channel must be one captured, the kind one of "
                                 f"{', '.join(TRIGGERS)} (volts only for an analog channel)")
        if self.mode not in ("oneshot", "stream", "repeat") or self.on_loss not in ("stop", "continue"):
            raise ValueError(f"mode {self.mode!r} / on_loss {self.on_loss!r}: oneshot, stream or repeat / stop or continue")
        if self.mode == "repeat":
            if not (self.segments and self.segments >= 1) or not self.samples:
                raise ValueError("repeat needs samples and segments (how many acquisitions, 1 or more)")
            if self.analog:
                raise ValueError("repeat: logic channels only")
        elif self.segments is not None:
            raise ValueError("segments is for repeat only")
        if self.mode == "stream" and not (self.seconds and self.seconds > 0):
            raise ValueError("a stream needs seconds (how long)")
        if self.arm not in ("pretrigger", "now"):
            raise ValueError(f"arm {self.arm!r}: pretrigger or now")
        logic = {n for n, _ in self.channels}
        for name, d in self.resolution.items():
            if name not in logic or d < 1:
                raise ValueError(f"{name}/{d}: a resolution is for a logic channel captured, 1 or more samples")


class OnDisk:
    """Channel data kept in a file instead of memory (a long stream's bits). len() is its size; slicing reads that
    range; copy_to() writes it out a chunk at a time."""

    CHUNK = 1 << 22

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def __len__(self) -> int:
        return self.path.stat().st_size

    def __getitem__(self, key):
        if not isinstance(key, slice) or key.step not in (None, 1):
            raise TypeError("OnDisk takes a plain slice")
        start, stop, _ = key.indices(len(self))
        with open(self.path, "rb") as f:
            f.seek(start)
            return f.read(max(0, stop - start))

    def copy_to(self, out, chunk_bytes: int | None = None) -> None:
        with open(self.path, "rb") as f:
            while chunk := f.read(chunk_bytes or self.CHUNK):
                out.write(chunk)


@dataclass
class Bits:
    """A logic line as its samples: n samples at ticks phase + k * step, one bit each."""
    name: str
    bits: bytes | OnDisk
    n: int
    step: int = 1
    phase: int = 0
    acquisition: dict = field(default_factory=dict)      # how it was taken: {"pin": channel number}

    def __post_init__(self):
        if self.step < 1 or not 0 <= self.phase:
            raise ValueError(f"{self.name}: step must be >= 1 and phase >= 0")
        if len(self.bits) != (self.n + 7) // 8:
            raise ValueError(f"{self.name}: {len(self.bits)} bytes for {self.n} samples, want {(self.n + 7) // 8}")

    @property
    def end(self) -> int:
        return self.phase + self.n * self.step


@dataclass
class Changes:
    """A logic line as its changes at a resolution of `step` ticks (capture format 1): quantum k is ticks
    [phase + k * step, phase + (k + 1) * step), n quanta; start_level the level before quantum 0. `data` holds
    `count` events (the module's description); step 1 is lossless."""
    name: str
    data: bytes
    n: int
    step: int = 1
    phase: int = 0
    start_level: int = 0
    count: int = 0
    acquisition: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.step < 1 or not 0 <= self.phase or self.start_level not in (0, 1) or self.n < 0:
            raise ValueError(f"{self.name}: step must be >= 1, phase >= 0, start_level 0 or 1")

    @property
    def end(self) -> int:
        return self.phase + self.n * self.step

    def decode(self) -> tuple[list[int], list[int], list[int]]:
        """-> (quanta, levels after, counts), one entry per event."""
        ks, levels, counts = [], [], []
        k, i, d = -1, 0, self.data
        while i < len(d):
            gap, shift = 0, 0
            while True:
                b = d[i]
                i += 1
                gap |= (b & 0x7F) << shift
                shift += 7
                if b < 0x80:
                    break
            k += gap + 1
            ks.append(k)
            levels.append(d[i] >> 7)
            counts.append(d[i] & 0x7F)
            i += 1
        if len(ks) != self.count:
            raise ValueError(f"{self.name}: {len(ks)} events in the data, {self.count} counted")
        return ks, levels, counts


def changes(name: str, quanta, levels, counts, n: int, step: int = 1, phase: int = 0, start_level: int = 0,
            **acquisition) -> Changes:
    """(quantum, level after, changes in it) per event, quanta increasing and below n -> Changes. A count of 127 or
    more is kept as 127 (at least 127); otherwise the level follows from the one before and the count's parity, and
    at step 1 each event is one change."""
    out = bytearray()
    prev, level = -1, start_level
    quanta, levels, counts = list(quanta), list(levels), list(counts)
    for k, lv, c in zip(quanta, levels, counts):
        if not prev < k < n or lv not in (0, 1) or c < 1 or (step == 1 and c != 1):
            raise ValueError(f"{name}: event (quantum {k}, level {lv}, count {c}) out of order or impossible")
        c = min(c, 127)
        if c < 127 and lv != level ^ (c & 1):
            raise ValueError(f"{name}: quantum {k} ends at {lv} after {c} changes from {level}")
        gap = k - prev - 1
        while True:
            out.append((gap & 0x7F) | (0x80 if gap > 0x7F else 0))
            gap >>= 7
            if not gap:
                break
        out.append((lv << 7) | c)
        prev, level = k, lv
    return Changes(name, bytes(out), n, step, phase, start_level, len(quanta), acquisition)


_TYPECODE = {8: "B", 16: "H", 32: "I"}


@dataclass
class Analog:
    """An analog line's raw values (`width` bits, little endian), volts = (raw - zero) * scale_nv * 1e-9 when zero /
    scale_nv are known. Sample k is at tick t0_ticks + k * tick_hz / rate_hz."""
    name: str
    data: bytes
    n: int
    rate_hz: Fraction
    t0_ticks: Fraction = Fraction(0)
    width: int = 16
    value_bits: int | None = None
    zero: float | None = None
    scale_nv: float | None = None
    unit: str = "V"
    acquisition: dict = field(default_factory=dict)      # pin, frontend, reference, vrefint, ...

    def __post_init__(self):
        self.rate_hz, self.t0_ticks = Fraction(self.rate_hz), Fraction(self.t0_ticks)
        if self.width not in _TYPECODE or len(self.data) != self.n * self.width // 8:
            raise ValueError(f"{self.name}: {len(self.data)} bytes for {self.n} values of {self.width} bits")

    def end(self, tick: Fraction) -> int:
        return math.ceil(self.t0_ticks + self.n * tick / self.rate_hz)

    def values(self) -> list[int]:
        a = array.array(_TYPECODE[self.width])
        a.frombytes(self.data)
        if sys.byteorder == "big":
            a.byteswap()
        return a.tolist()


def analog_raw(name: str, values, rate_hz, width: int = 16, t0_ticks=0, value_bits: int | None = None,
               zero: float | None = None, scale_nv: float | None = None, unit: str = "V", **acquisition) -> Analog:
    a = array.array(_TYPECODE[width], values)
    if sys.byteorder == "big":
        a.byteswap()
    data = a.tobytes()
    return Analog(name, data, len(data) // (width // 8), rate_hz, t0_ticks, width, value_bits, zero, scale_nv, unit,
                  acquisition)


@dataclass
class Part:
    """A block of ticks with no gap inside: its channels from its tick 0, its own facts (start_ns,
    start_uncertainty_ns, trigger_tick, time_base_slipped, ...), and `tick0`, where it starts after the first part's
    tick 0 (a timeline; None in segments when not known)."""
    channels: list
    meta: dict = field(default_factory=dict)
    tick0: int | None = None


@dataclass
class Result:
    tick_hz: Fraction
    channels: list                               # one part: its channels (parts is None)
    meta: dict = field(default_factory=dict)     # the first part's facts, "probe": the probe's (model, firmware,
                                                 # address, boot_id, clock, calibration, link, ...)
    parts: list[Part] | None = None             # a capture in parts
    lost: list[dict] = field(default_factory=list)   # {"after_part", "reason", "stopped", "ticks"?}: any is a failure
    layout: str = "timeline"
    temp_dir: str | None = None                  # where OnDisk data lives; the caller removes it once copied

    @property
    def complete(self) -> bool:
        return not self.lost

    @property
    def all_parts(self) -> list[Part]:
        """The parts, one part made of `channels` and `meta` when the capture came in one."""
        if self.parts is not None:
            return self.parts
        return [Part(self.channels, {k: v for k, v in self.meta.items() if k != "probe"}, 0)]


# ---------------- packing ----------------

_TO_ASCII01 = bytes(0x31 if b & 1 else 0x30 for b in range(256))


def pack(samples: bytes | bytearray | memoryview) -> bytes:
    """One sample per byte (its bit 0 is the level) -> one bit per sample."""
    n = len(samples)
    if n == 0:
        return b""
    text = bytes(samples).translate(_TO_ASCII01)[::-1]
    return int(text, 2).to_bytes((n + 7) // 8, "little")


def _bit(data: bytes, b: int) -> bytes:
    return data.translate(bytes((x >> b) & 1 for x in range(256)))


def from_interleaved(data: bytes | bytearray | memoryview, names: list[str], width: int = 8,
                     positions: list[int] | None = None, n: int | None = None, step: int = 1,
                     phase: int = 0) -> list[Bits]:
    """Lines from a probe's sample stream (capture §1.1): each sample is `width` bits, sample i starts at stream bit
    i*width, line k is bit positions[k] of it (default k); stream bit j is bit j % 8 of byte j // 8."""
    data = bytes(data)
    if not 1 <= width <= 128:
        raise ValueError(f"width {width}: must be 1-128")
    positions = list(range(len(names))) if positions is None else list(positions)
    if len(positions) != len(names) or any(not 0 <= q < width for q in positions):
        raise ValueError(f"positions {positions} do not fit {len(names)} channels of a {width}-bit sample")
    total = len(data) * 8 // width
    n = total if n is None else n
    if n > total:
        raise ValueError(f"{n} samples of {width} bits need {(n * width + 7) // 8} bytes, got {len(data)}")
    out = []
    for name, q in zip(names, positions):
        col = bytearray(n)
        for j in range(min(8, n)):
            b = j * width + q
            col[j::8] = _bit(data[b // 8::width], b % 8)[:(n - j + 7) // 8]
        out.append(Bits(name, pack(col), n, step, phase))
    return out


TRIGGER = {"high": (1, 1), "low": (1, 0), "rise": (2, 0), "fall": (2, 1), "both": (2, 2)}   # (type, value), oep-if-capture
TAG_CHIP, TAG_MODEL, TAG_FIRMWARE, TAG_UNIT_ID = 0x4C, 0x41, 0x40, 0x42     # core describe
# UART link rates tried by default (port_speed): fastest first. oep-client refuses a rate that breaks and drops back
# to the boot speed on repeated broken frames, so trying never ends slower than the boot speed; capture_at's
# fast=[] keeps the boot speed, fast=[RATE, ...] tries those.
FAST = [1_500_000, 921_600, 500_000]
PER_SESSION = 2          # candidates tried per open: each costs up to ~1 s, plus a wait when it fails (core §3.5)
FLOWS = [("in", 0)]      # a capture reads back: verify probe -> host only, at the link's largest in-flight n


def _raise_default(link, hst, port: str | None) -> tuple[list[int], bool]:
    """The default raise, left to the client: it orders and skips the candidates by its record of this port and
    probe, tries at most PER_SESSION, retries the slowest when all failed, and steps down to a slower one in use.
    -> (rates asked, whether the record shaped them)."""
    if not port:
        return [], False
    link.raise_speed(hst, list(FAST), record=True, flows=FLOWS, max_tries=PER_SESSION)
    rep = hst.link.speed
    return list(FAST), bool(rep is not None and (rep.skipped or rep.retried))


def capture_at(address: str, req: Request, fast: list[int] | None = None, owner: str = "oep_client.acquire") -> Result:
    """Open the probe at `address` (an OEP address or a short form, as link.open_host takes it), take its lease, run
    one capture and let it all go. fast: the UART link rates to try (None: the client's record of this port and
    probe; []: keep the boot speed)."""
    from . import link
    return _run(address, req, fast, None, owner, link)


def capture(hst, req: Request) -> Result:
    """One capture on a session the caller holds (opened, with its lease; ProbeLog and the target's control can share
    it): the link is left as it is, and the plans this capture applied are released after."""
    from . import link
    return _run(getattr(getattr(hst, "address", None), "place", None) or "the probe", req, [], hst, None, link)


def _run(target: str, req: Request, fast, hst, owner, link) -> Result:
    from . import capture as oc
    from . import core, message
    from . import host as oh
    if req.mode == "stream" and req.trigger:
        raise ValueError("a stream is started at once: no trigger")
    try:
        res = _capture(link, core, oc, oh, message, target, req, None, None, fast, hst,
                       owner or "oep_client.acquire")
        if isinstance(res, (_Streamed, _GroupStreamed)):   # put together after the session closed: it may take a while
            res = res.result()
        return res
    except oh.OepError as e:        # the probe refused or failed: say what, not where
        hint = ""
        if "(storage)" in str(e):     # capture §2.2: data lost inside a segment stops the track with error 2
            hint = " - the probe lost data inside a segment: try a lower rate or fewer channels"
        raise RuntimeError(f"probe {target}: {e}{hint}") from e


def _trigger_source(oc, req: Request, cap, an, names: list[str], acfg):
    """--trigger as the group's trigger source (capture §4.4): one stage of one condition - a logic channel's edge or
    level, or an analog channel crossing / staying beyond a voltage (in its ADC values by the answer's scale)."""
    name, kind, *volts = req.trigger
    if name in names:
        role = names.index(name)
        cond = (oc.Cond.edge(cap, role, {"rise": 0, "fall": 1, "both": 2}[kind]) if kind in ("rise", "fall", "both")
                else oc.Cond.level(cap, role, 1 if kind == "high" else 0))
    else:
        role = [n for n, *_ in req.analog].index(name)
        zero, scale = acfg.zero.get(role), acfg.scale_nv.get(role)
        if zero is None or not scale:
            raise ValueError(f"trigger {name}: the probe gives no volt conversion for it")
        value = round(volts[0] * 1e9 / scale + zero)
        top = (1 << acfg.bits) - 1 if acfg.bits else None
        if value < 0 or top is not None and value > top:
            raise ValueError(f"trigger {name}:{kind}:{volts[0]}: outside the input's range")
        make = {"rise": oc.Cond.cross_up, "fall": oc.Cond.cross_down, "high": oc.Cond.above, "low": oc.Cond.below}
        if scale < 0:                            # an inverting frontend: up in volts is down in values
            make = {"rise": oc.Cond.cross_down, "fall": oc.Cond.cross_up, "high": oc.Cond.below, "low": oc.Cond.above}
        cond = make[kind](an, role, value)
    return oc.TriggerSource([oc.Stage([[cond]])], arm=0 if req.arm == "now" else 1)


def _check_caps(group, source, req: Request, target: str, probe: dict) -> None:
    """The group's trigger_caps (capture §4.4, a declaration: bind has the last word) kept in meta.probe, and a
    condition type it does not declare refused before bind, saying which it takes."""
    try:
        caps = group.trigger_caps() if hasattr(group, "trigger_caps") else None
    except Exception:              # noqa: BLE001 - a declaration is a bonus: bind still answers
        caps = None
    if caps is None:
        return
    probe["trigger_caps"] = {"stages": caps.max_stages, "terms": caps.max_terms, "conds": caps.max_conds,
                             "cross_track": caps.cross_track, "kinds": caps.kinds()}
    kinds = {c.kind for st in source.stages for term in st.terms for c in term}
    if not all(caps.takes(k) for k in kinds):
        what = ":".join(str(x) for x in req.trigger[:2]) + (f":{req.trigger[2]}V" if len(req.trigger) > 2 else "")
        raise RuntimeError(f"probe {target} cannot start its tracks on {what} together: its capture group triggers on "
                           f"{', '.join(caps.kinds()) or 'nothing'} (trigger_caps, capture §4.4)")


MODE_TAG = 0x01        # capture configure's mode TLV (capture §3.3)
RATE_TAG = 0x02        # ... its rate
FORMAT_TAG = 0x08      # ... and its format (format 1: NAME=ID/D)


def _what(tag, oc) -> str:
    """A configure item for people. The probe names the TLV it refused with its
    tag as sent (bit 7 is the critical mark)."""
    base = tag & 0x7F if isinstance(tag, int) else tag
    return {oc.TRIGGER: "a trigger", oc.PRETRIGGER: "a pretrigger",
            RATE_TAG: "this rate (outside the probe's rate range for this track: ask a lower --rate)",
            MODE_TAG: "this mode (--mode: the probe does not take it on this track)",
            FORMAT_TAG: "this data format (NAME=ID/D: the probe keeps a line as its changes only in some modes)",
            getattr(oc, "TRIGGER_OPTS", None): "these trigger options (--arm now)"}.get(
        base, f"configure item 0x{base:02x}" if isinstance(base, int) else f"configure item {tag}")


def _waiting(wait, cap, req):
    """wait(), with a timeout that names a trigger which never came (the probe still waiting, state 2)."""
    try:
        return wait()
    except TimeoutError:
        if req.trigger and cap is not None:
            try:
                st = cap.status()
                waiting = getattr(st, "state", None) == oc_state("waiting") if hasattr(st, "state") else \
                    st[0] == oc_state("waiting")
            except Exception:           # the timeout is the news; a failed status read must not hide it
                waiting = False
            if waiting:
                raise TimeoutError(f"no trigger ({req.trigger[0]}:{req.trigger[1]}) within {req.timeout:g} s "
                                   f"(--timeout)") from None
        raise


def oc_state(name: str) -> int:
    from oep_client.capture import STATE
    return STATE[name]


def _configure(track, oc, oh, target: str, what: str, mode=None, **kw):
    """configure; a refusal (unsupported, naming the TLV) in one line. Every probe knows the configure TLVs of OEP
    v1 (capture §3.3), so one it cannot honour is refused, not ignored."""
    try:
        return track.configure(mode=oc.ONE_SHOT if mode is None else mode, **kw)
    except oh.Unsupported as e:
        raise RuntimeError(f"probe {target} cannot capture {what} with {_what(e.tag, oc)}"
                           f"{_most_said(track, oc, oh, e, kw) if mode is None else ''}") from e
    except oh.Rejected as e:
        if "data_format" not in kw:
            raise
        raise RuntimeError(f"probe {target} cannot capture {what} with lines kept as their changes (NAME=ID/D, "
                           f"capture format 1) this way: {e} - the probe has not the room for it here; without "
                           f"/D the lines go as samples") from e


def _most_said(track, oc, oh, e, kw: dict) -> str:
    """For a refused pretrigger: the most this setting takes, found by query (nothing set), as words; else ""."""
    if e.tag is None or e.tag & 0x7F != oc.PRETRIGGER or not kw.get("pretrigger"):
        return ""
    most = _most_pretrigger(track, oc, oh, kw)
    return f" (at most {most} samples before the trigger here, of {kw.get('samples')})" if most is not None else ""


def _most_pretrigger(track, oc, oh, kw: dict) -> int | None:
    """The largest pretrigger the probe takes with these settings, by query and bisection (about log2 of it queries;
    capture §3.3: the same rules as configure, nothing set). None: no query, or not even a small one."""
    from oep_client import message
    lo, hi = 0, int(kw["pretrigger"]) - 1
    if hi < 1 or not isinstance(_query(track, oc, oh, message, {**kw, "pretrigger": 1}), oc.Config):
        return None
    lo = 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        r = _query(track, oc, oh, message, {**kw, "pretrigger": mid})
        if r is None:
            return None
        if isinstance(r, Exception):
            hi = mid - 1
        else:
            lo = mid
    return lo


def _query(track, oc, oh, message, kw: dict):
    """configure's query (capture §3.3: the same rules, nothing set) -> the answer, the refusal (the exception), or None
    when the probe has no query (OEP v1: optional)."""
    try:
        return track.configure(mode=oc.ONE_SHOT, query=True, **kw)
    except oh.Rejected as e:
        if not isinstance(e, oh.Unsupported) and e.result.detail == message.UNKNOWN_OPERATION:
            return None
        return e


def _clock(hst):
    """The probe's clock against this host's (core §7.7): the reading with the shortest round trip of a few, or None
    when it could not be read (the capture goes on without it)."""
    try:
        return hst.clock_best(4)
    except Exception:                  # noqa: BLE001 - the clock is a bonus to the capture
        return None


def _clock_info(before, after) -> dict:
    """meta.probe.clock: each reading as (host monotonic ns at the midpoint, the probe's uptime_ns, +- ns), and the
    probe clock's rate against the host's when both readings are of one boot (ppm: positive = the probe runs fast)."""
    out = {}
    for name, r in (("before", before), ("after", after)):
        if r is not None:
            out[name] = {"host_ns": int(r.host_ns), "uptime_ns": int(r.uptime_ns), "boot_id": int(r.boot_id),
                         "uncertainty_ns": int(r.uncertainty_ns)}
    if before is not None and after is not None and before.boot_id == after.boot_id:
        host_d, probe_d = after.host_ns - before.host_ns, after.uptime_ns - before.uptime_ns
        if host_d > 0:
            out["rate_ppm"] = (probe_d / host_d - 1) * 1e6
            out["rate_ppm_uncertainty"] = (before.uncertainty_ns + after.uncertainty_ns) / host_d * 1e6
    return out


def _describe(core, hst, fn: int) -> list[tuple[int, bytes]]:
    try:
        return core.describe(hst, fn)
    except Exception:       # noqa: BLE001 - what the probe tells about itself is a bonus
        return []


def _channels(core, hst, lines: list[str]) -> list[int]:
    """The probe's channel numbers (valid for this boot) of lines named as kept in scripts: a channel name (fn 0's
    channel_name, core §7.5; GPIO47) or a port's line label (S.N, host guide §5.3; x035.swdio). ASCII case aside."""
    if not lines:
        return []
    from . import registry, settings
    tag = registry.CORE.tlv["describe"]["channel_name"]
    names = {}
    for t, v in _describe(core, hst, 0):
        if t == tag and len(v) >= 2:
            names.setdefault(settings.fold_name(v[2:].decode("utf-8", "replace")), []).append(
                struct.unpack_from("<H", v)[0])
    labels = None
    out = []
    for line in lines:
        port_line = settings.port_line(line)
        if port_line is not None:
            if labels is None:
                labels = settings.labels(hst)
            ch = settings.line_from_labels(labels, port_line[1], port_line[0])
            if ch is None:
                raise ValueError(f"{line}: no channel carries that label (host guide §5.3)")
        else:
            found = names.get(settings.fold_name(line), [])
            if len(found) != 1:
                known = ", ".join(sorted({k for k in names}))[:200]
                raise ValueError(f"{line}: {'no channel' if not found else 'several channels'} of that name "
                                 f"(the probe's channel names: {known or 'none'}; or a line label S.N)")
            ch = found[0]
        out.append(ch)
    return out


def _probe_info(core, hst, boot_id) -> dict:
    info = {}
    for tag, value in _describe(core, hst, 0):
        key = {TAG_CHIP: "chip", TAG_MODEL: "model", TAG_FIRMWARE: "firmware", TAG_UNIT_ID: "unit_id"}.get(tag)
        if key:
            info[key] = value.decode("utf-8", "replace").rstrip("\0")
    if boot_id is not None:
        info["boot_id"] = boot_id       # start_ns of captures with the same boot_id share one clock
    address = getattr(hst, "address", None)
    if address is not None:             # the probe itself, without the route (host guide §5.3)
        info["address"] = address.place
    elif info.get("unit_id"):
        info["address"] = f"oep://{info['unit_id']}"
    return info


def _frontends(core, hst, fn: int) -> dict[int, dict]:
    """The analog describe's frontends (capture §3.5): number -> its measurable range. The value is 9 bytes; one of
    another length is broken and not used (core §2.3). The volts are the input pin's, any attenuation before the ADC
    already in the scale (capture §1.2), so none is read here."""
    from oep_client import registry
    want = registry.FIXTURE_ANALOG.tlv["describe"]["frontend"]
    out = {}
    for tag, v in _describe(core, hst, fn):
        if tag == want and len(v) == 9:
            fe, lo, hi = struct.unpack("<Bii", v)
            out[fe] = {"frontend": fe, "range_min_mv": lo, "range_max_mv": hi}
    return out


def _raise(link, hst, rates: list[int]) -> list[int]:
    """Raise the link of an open host to the first of `rates` that holds, verifying the read-back direction (FLOWS);
    the client keeps the boot speed, or goes back to it, whenever the probe, the adapter or the line cannot.
    -> the rates asked of the client."""
    if rates:
        link.raise_speed(hst, rates, flows=FLOWS)
    return rates


def _link_info(hst, rates: list[int], open_s: float, read_bytes: int, read_s: float) -> dict:
    """How the link went, for meta.probe.link: what was asked, the rate in force, each trial with its flows, what
    the record skipped, steps down while in use, and the read."""
    out = {"open_s": round(open_s, 3), "read_bytes": read_bytes, "read_s": round(read_s, 3)}
    lk = getattr(hst, "link", None)
    reader = getattr(lk, "reader", None)
    if isinstance(reader, str):
        out["reader"] = reader                            # usb-async / usb-sync (a one-read host's pauses drop data)
    rep = getattr(lk, "speed", None)
    if rates:
        out["asked"] = rates
    if rep is not None:
        out["rate"] = rep.rate
        out["raised"] = bool(rep.chosen)
        if not rep.supported:
            out["why"] = rep.why
        out["trials"] = [{"rate": t.rate, "result": "committed" if t.committed else t.why,
                          "flows": [{"flow": f.flow, "n": f.n, "frames": f.frames, "broken": f.broken, "lost": f.lost,
                                     "kb_s": round(f.kb_s, 1), "passed": f.passed} for f in t.flows]}
                         for t in rep.trials]
        if rep.skipped:
            out["skipped"] = list(rep.skipped)            # the record says these failed on this port and probe
        if rep.retried:
            out["retried"] = rep.retried                  # every rate recorded failed: the slowest tried once anyway
        if rep.capped:
            out["capped"] = list(rep.capped)              # left out by the limit of tries per capture
        if rep.step_downs:                                # in use (or in probation): from a rate to a slower one
            out["step_downs"] = [{"from": d.rate, "to": d.to, "why": d.why, "probation": d.probation}
                                 for d in rep.step_downs]
        if rep.stepped_down or rep.lost:
            out["stepped_down"] = rep.down_why or "lost"  # went down while in use
    return out


def _capture(link, core, oc, oh, message, target: str, req: Request, ids: list[int], aids: list[int],
             fast: list[int] | None = None, hst=None, owner: str = "oep_client.acquire") -> Result:
    """One capture. hst None: open `target`, take the lease, raise a UART link's speed, and close it all after;
    else on that open session as it is (the caller holds it, its link left as it is)."""
    import time
    names = [n for n, _ in req.channels]
    t_open = time.monotonic()
    remembered, own = False, hst is None
    if own:
        hst = link.open_host(target)
    try:
        boot_id = None
        record = False
        if own:
            boot_id = core.take(hst, 30_000, owner=owner).boot_id
            # the link's memory is per port and probe (host guide): the adapter belongs to the port, the rest of the
            # link to the probe, so either changing starts over
            record = fast is None                      # the default: the client's record of this port and probe
            if fast is None:
                port = getattr(getattr(hst, "link", None), "port_path", None)
                fast, remembered = _raise_default(link, hst, port)
            else:
                fast = _raise(link, hst, fast)
        else:
            fast = []
        open_s = time.monotonic() - t_open
        clock_before = _clock(hst)
        if boot_id is None and clock_before is not None:
            boot_id = clock_before.boot_id
        probe = _probe_info(core, hst, boot_id)
        ids, aids = _channels(core, hst, [c for _, c in req.channels]), _channels(core, hst, [c for _, c, _ in req.analog])
        cap = oc.LogicCapture(hst) if ids else None
        an = oc.AnalogCapture(hst) if aids else None
        plan = [(cap.fn, k, ch) for k, ch in enumerate(ids)] if cap else []
        plan += [(an.fn, k, ch) for k, ch in enumerate(aids)] if an else []
        core.plan_apply(hst, plan)
        fns = sorted({fn for fn, _, _ in plan})
        try:
            cfg = acfg = None
            grouped = bool(req.trigger and an)        # a trigger with analog: the group's trigger source (§4.4)
            if cap:
                trigger = None
                if req.trigger and grouped:                # the group's event starts it (capture §4.4)
                    trigger = (oc.LINKED, 0, 0)
                elif req.trigger:
                    kind, value = TRIGGER[req.trigger[1]]
                    trigger = (kind, names.index(req.trigger[0]), value)
                blocks = {}
                if req.resolution:          # format 1: these lines as their changes, the others raw (§1.3)
                    blocks = dict(data_format=oc.DATA_FORMAT["logic_blocks"], roles=list(range(len(ids))),
                                  events={names.index(n): d for n, d in req.resolution.items()})
                if req.arm == "now" and trigger and not grouped:   # arm 0 (§3.3): optional, so critical; 1 the default
                    if not hasattr(oc, "TriggerOpts"):
                        raise ValueError("--arm now needs an oep-client-python with trigger_opts")
                    blocks["trigger_opts"] = oc.TriggerOpts(arm=0)
                if req.mode == "stream":
                    cfg = _configure(cap, oc, oh, target, "logic", oc.STREAMING, rate=req.rate, **blocks)
                    if an:                     # logic and analog: both streamed in one group (capture §4.1)
                        arate = req.analog_rate or req.rate
                        fes = {k: fe for k, (_, _, fe) in enumerate(req.analog) if fe is not None}
                        acfg = _configure(an, oc, oh, target, "analog", oc.STREAMING, rate=arate,
                                          **({"frontends": fes} if fes else {}))
                        return _group_stream_result(cap, an, hst, cfg, acfg, req, names, ids, aids, probe, fast,
                                                    open_s, clock_before, oc, an.calibration(),
                                                    _frontends(core, hst, an.fn))
                    return _stream_result(cap, hst, cfg, req, names, ids, probe, fast, open_s, clock_before, oc)
                if req.mode == "repeat":       # with a trigger, one per segment (rearm 1, capture §2.1 / §3.3)
                    if trigger:
                        blocks["trigger_opts"] = oc.TriggerOpts(arm=0 if req.arm == "now" else 1, rearm=1)
                    cfg = _configure(cap, oc, oh, target, "logic in repeat", oc.REPEAT, rate=req.rate, samples=req.samples,
                                     segments=req.segments, trigger=trigger, pretrigger=req.pretrigger, **blocks)
                    return _repeat_result(cap, hst, cfg, req, names, ids, probe, fast, open_s, clock_before, oc, oh)
                lkw = dict(rate=req.rate, samples=req.samples, trigger=trigger, pretrigger=req.pretrigger, **blocks)
                if grouped:          # both tracks asked first (query), set only when neither refuses (below)
                    lq = _query(cap, oc, oh, message, lkw)
                    lrate = Fraction(lq.rate) if getattr(lq, "rate", None) else Fraction(req.rate)
                    lsamples = getattr(lq, "samples", None) or req.samples
                else:
                    cfg = _configure(cap, oc, oh, target, "logic", **lkw)
                    lrate, lsamples = Fraction(cfg.rate), getattr(cfg, "samples", None) or req.samples
            if an and req.mode == "stream":           # analog alone (Request refuses it beside logic, for now)
                arate = req.analog_rate or req.rate
                fes = {k: fe for k, (_, _, fe) in enumerate(req.analog) if fe is not None}
                acfg = _configure(an, oc, oh, target, "analog", oc.STREAMING, rate=arate,
                                  **({"frontends": fes} if fes else {}))
                return _analog_stream_result(an, hst, acfg, req, aids, probe, fast, open_s, clock_before, oc,
                                             an.calibration(), _frontends(core, hst, an.fn))
            if an:
                arate = req.analog_rate or req.rate
                asamples = req.analog_samples
                fes = {k: fe for k, (_, _, fe) in enumerate(req.analog) if fe is not None}
                if asamples is None and cap:
                    # as long as the logic capture the probe answered (it may hold fewer samples than asked), at
                    # the rate it will really use (channels share the ADC, so it may answer less than asked)
                    real = Fraction(arate)
                    try:
                        q = an.configure(mode=oc.ONE_SHOT, rate=arate, samples=1, query=True,
                                         **({"frontends": fes} if fes else {}))
                        real = Fraction(q.rate) if q.rate else real
                    except oh.Rejected as e:       # query is optional (OEP v1): absent = unknown_operation
                        if e.result.detail != message.UNKNOWN_OPERATION:
                            raise
                    asamples = max(1, round(Fraction(lsamples) / lrate * real))
                elif asamples is None:
                    asamples = req.samples
                linked = {}
                if grouped:                             # its own pretrigger, in its own samples (§4.4)
                    pre = req.pretrigger or 0
                    if cap and pre:
                        real = Fraction(an.configure(mode=oc.ONE_SHOT, rate=arate, samples=1, query=True,
                                                     **({"frontends": fes} if fes else {})).rate)
                        pre = round(Fraction(pre) * real / lrate)
                    linked = {"trigger": (oc.LINKED, 0, 0), **({"pretrigger": pre} if pre else {})}
                what = "analog"
                if linked.get("pretrigger") and cap:    # say where the number came from when it is refused
                    what = (f"analog (its pretrigger {linked['pretrigger']} of {asamples} samples is the logic's "
                            f"{req.pretrigger} at {float(lrate):g} Hz, counted at the analog rate; a longer capture "
                            f"or a lower logic rate gives it more)")
                akw = dict(rate=arate, samples=asamples, **({"frontends": fes} if fes else {}), **linked)
                if grouped:          # every refusal at once, before either track is set (capture §3.3 query)
                    asked = ([("logic", lq)] if cap else []) + [(what, _query(an, oc, oh, message, akw))]
                    refused = [(w, r) for w, r in asked if isinstance(r, Exception)]
                    if refused:
                        kws = {"logic": (cap, lkw), what: (an, akw)}
                        raise RuntimeError(f"probe {target} cannot capture these together (each track asked before "
                                           f"either was set): " + "; ".join(
                                               f"{w} with {_what(r.tag, oc)}{_most_said(*kws[w][:1], oc, oh, r, kws[w][1])}"
                                               if isinstance(r, oh.Unsupported) else f"{w}: {r}"
                                               for w, r in refused)) from refused[0][1]
                    if cap:
                        cfg = _configure(cap, oc, oh, target, "logic", **lkw)
                acfg = _configure(an, oc, oh, target, what, **akw)
            group = None
            if an and (cap or grouped):
                group = oc.CaptureGroup(hst)
                tracks = [t for t in (cap, an) if t]
                source = _trigger_source(oc, req, cap, an, names, acfg) if grouped else None
                judge = (cap if req.trigger[0] in names else an) if grouped else None
                latency = getattr(judge.config, "judge_latency_ns", None) if judge is not None else None
                if latency is not None:                 # how long the judging track takes to know the event (§3.3)
                    probe["judge_latency_ns"] = latency
                if source is not None:
                    _check_caps(group, source, req, target, probe)
                try:
                    group.bind(tracks, source)
                except (oh.Unsupported, oh.Rejected) as e:
                    if source is None:
                        raise
                    why = ""
                    if latency is not None:
                        others = ", ".join(f"{'logic' if t is cap else 'analog'} {float(t.config.rate):g} Hz"
                                           for t in tracks if t is not judge)
                        why = (f"; the {'logic' if judge is cap else 'analog'} track takes up to {latency / 1e6:.3g} ms "
                               f"to know the event, and the others ({others}) must hold that long besides their "
                               f"pretrigger - a lower rate or fewer channels on them, or the trigger on the other track")
                    raise RuntimeError(f"probe {target} cannot start its tracks on a trigger together (capture-group "
                                       f"trigger source, capture §4.4: {e}){why}") from e
                try:
                    group_start = group.start(tracks)
                    st = _waiting(lambda: group.wait(req.timeout), group, req)
                    seg = _segment(cap) if cap else None
                    aseg = _segment(an)
                finally:
                    group.bind([])
                probe["group_start_ns"] = group_start
                if st.trigger is not None:              # (fn judging, ns, +-ns) once the event happened
                    probe["trigger_ns"], probe["trigger_uncertainty_ns"] = st.trigger[1], st.trigger[2]
            else:
                track = cap or an
                track.start()
                segments = _waiting(lambda: track.wait(req.timeout), cap, req)
                if not segments:
                    raise RuntimeError("the probe finished without a segment")
                seg = segments[0] if cap else None
                aseg = segments[0] if an else None
            t_read = time.monotonic()
            data = cap.read_segment(seg) if cap else None
            adata = an.read_segment(aseg) if an else None
            read_s = time.monotonic() - t_read
            probe["link"] = _link_info(hst, fast, open_s, len(data or b"") + len(adata or b""), read_s)
            if remembered:
                probe["link"]["remembered"] = True
            if "rate" not in probe["link"] and getattr(getattr(hst, "link", None), "base_baud", None):
                probe["link"].update(rate=hst.link.base_baud, raised=False)   # nothing tried: the boot speed
            if "rate" not in probe["link"] and record and not getattr(getattr(hst, "link", None), "port_path", None):
                probe["link"].update(rate=0, why="the link is not a serial port this host opened")
            calib = an.calibration() if an else None
            clock_after = _clock(hst)
            clock = _clock_info(clock_before, clock_after)
            if clock:
                probe["clock"] = clock
            frontends = _frontends(core, hst, an.fn) if an else {}
        finally:
            core.plan_release(hst, fns)
    finally:
        if own:
            try:
                hst.end()
            finally:
                close = getattr(getattr(hst, "link", None), "close", None)
                if close is not None:
                    close()      # let the port go (a serial port is opened exclusively: the next open must succeed)

    meta = {}
    if cap:
        meta["probe_channels"] = dict(zip(names, ids))
        _times(meta, seg)
        if seg.slipped:
            meta["time_base_slipped"] = True
        if seg.trigger_index is not None:              # tick 0 is the segment's (base) sample 0
            meta["trigger_tick"] = seg.trigger_index
        tick = Fraction(cfg.rate)
        if getattr(cfg, "event_d", None):
            chans = _from_blocks(cap, data, names, seg.samples, cfg)
            probe["data"] = _budget(oc, cfg, len(data), seg.samples, len(names))
        else:
            chans = from_interleaved(data, names, cfg.width, cfg.positions, seg.samples)
        for c, ch in zip(chans, ids):
            c.acquisition = {"pin": ch}
        t_ref = seg.start_ns
    else:
        tick = Fraction(acfg.rate)
        chans = []
        _times(meta, aseg)
        t_ref = aseg.start_ns
    gens = {k: t.generation for k, t in (("logic", cap), ("analog", an)) if t is not None and t.generation is not None}
    if gens:
        probe["generation"] = gens
    if an:
        if calib is not None and (calib.factory or calib.vrefint):
            probe["calibration"] = [{"frontend": fe, "scheme": scheme, "raw": bytes(raw).hex()}
                                    for fe, scheme, raw in calib.factory]
        t0 = Fraction(aseg.start_ns - t_ref) * tick / 10**9 if t_ref is not None else Fraction(0)
        chans += _analog_chans(an, acfg, req, aids, adata, aseg.samples, t0, tick, frontends, calib,
                               aseg.start_uncertainty_ns, aseg.trigger_index)
    if probe:
        meta["probe"] = probe
    return Result(tick, chans, meta)


def _analog_values(an, acfg, data: bytes, k: int, samples: int):
    """Channel k's raw values (capture §1.2): at once with numpy for whole-byte slots (a stream's millions), else the
    client's reader."""
    if acfg.slot not in (8, 16, 32):
        return an.values(data, k, samples)
    import numpy as np
    width = acfg.slot // 8
    frames = np.frombuffer(data, f"<u{width}", count=samples * len(acfg.order)).reshape(samples, len(acfg.order))
    v = (frames[:, acfg.order.index(k)] >> acfg.offset) & ((1 << acfg.bits) - 1)
    return v.astype(f"<u{width}").tobytes()


def _analog_chans(an, acfg, req: Request, aids, data: bytes, samples: int, t0: Fraction, tick: Fraction, frontends,
                  calib, uncertainty_ns, trigger_index) -> list:
    """An analog track's data -> its channels: raw values at its own rate, the first sample at tick t0 (plus each
    channel's skew), with what was used to take them (pin, frontend, reference, Vrefint, the start's uncertainty)."""
    out = []
    for k, ((name, _, _), ch) in enumerate(zip(req.analog, aids)):
        values = _analog_values(an, acfg, data, k, samples)
        acq = {"pin": ch}
        fe = acfg.frontend.get(k)
        if fe is not None:
            acq["frontend"] = frontends.get(fe, {"frontend": fe})
        if acfg.reference:
            source, mv, measured = acfg.reference
            acq["reference"] = {"source": source, "mv": mv, "measured": bool(measured)}
        if calib is not None and calib.vrefint:
            acq["vrefint_raw"], acq["vrefint_ns"] = calib.vrefint
        if calib is not None and calib.vrefint_nominal_mv is not None:
            acq["vrefint_nominal_mv"] = calib.vrefint_nominal_mv
        acq["start_uncertainty_ns"] = uncertainty_ns
        if trigger_index is not None:
            acq["trigger_index"] = trigger_index
        width = {8: 8, 16: 16, 32: 32}[acfg.slot]
        out.append(analog_raw(name, values, Fraction(acfg.rate), width=width,
                                         t0_ticks=t0 + Fraction(acfg.skew_ns.get(k, 0)) * tick / 10**9,
                                         value_bits=acfg.bits or None, zero=acfg.zero.get(k),
                                         scale_nv=acfg.scale_nv.get(k), **acq))
    return out


def _from_blocks(cap, data: bytes, names: list[str], samples: int, cfg) -> list:
    """A format 1 segment's bytes -> the file's channels, in the order asked: a raw channel as bits, an events channel
    as `events` at its D (_segment_columns)."""
    out = []
    for name, col in zip(names, _segment_columns(cap, data, samples, len(names), cfg)):
        if isinstance(col, bytes):
            out.append(Bits(name, col[:(samples + 7) // 8], samples))
        else:
            d = col[4]
            out.append(_events_np(name, *col[:4], -(-samples // d), d))
    return out


def _block_columns(blocks, roles: int, cfg) -> list:
    """A format 1 segment's blocks -> per role, a raw channel's bits (L is a multiple of 8, so the blocks' bytes join
    as they are; the last block's tail 0, §1.3), or an events channel's (quanta of the segment, levels after, counts,
    start level, D) as numpy arrays (L is a multiple of D). A block the probe carried raw (fallback) is counted here
    from its samples and the block's start level, the same way the probe counts (§1.3)."""
    import numpy as np
    out = []
    for role in range(roles):
        d = cfg.event_d.get(role)
        if d is None:
            out.append(b"".join(b.raw[role] for b in blocks))
            continue
        qs, ls, cs = [], [], []
        for b in blocks:
            q0 = b.first // d
            if role in b.fallback:
                lv = np.unpackbits(np.frombuffer(b.raw[role], np.uint8), bitorder="little")[:b.samples]
                change = np.empty(b.samples, np.uint8)
                change[0] = lv[0] != b.start_levels[role]       # the level before the block (§1.3)
                np.not_equal(lv[1:], lv[:-1], out=change[1:])
                per = np.add.reduceat(change.astype(np.int64), np.arange(0, b.samples, d))
                t = np.flatnonzero(per)
                qs.append(q0 + t)
                ls.append(lv[np.minimum((t + 1) * d, b.samples) - 1])
                cs.append(per[t])
            else:
                e = b.events[role]
                qs.append(q0 + np.asarray(e.ticks, np.int64))
                ls.append(np.asarray(e.levels, np.uint8))
                cs.append(np.asarray(e.counts, np.int64))
        start = int(blocks[0].start_levels[role]) if blocks else 0
        out.append((np.concatenate(qs).astype(np.int64) if qs else np.zeros(0, np.int64),
                    np.concatenate(ls).astype(np.uint8) if ls else np.zeros(0, np.uint8),
                    np.concatenate(cs).astype(np.int64) if cs else np.zeros(0, np.int64), start, d))
    return out


def _segment_columns(cap, data: bytes, samples: int, roles: int, cfg) -> list:
    """_block_columns of a format 1 segment's bytes; read by columns when the client can (no block objects)."""
    columns = getattr(cap, "columns", None)
    if columns is None:
        return _block_columns(cap.blocks(data, samples), roles, cfg)
    got = columns(data, samples)
    return [got[r] if r not in cfg.event_d else (*got[r], cfg.event_d[r]) for r in range(roles)]


def _events_np(name: str, quanta, levels, counts, start_level: int, n: int, step: int, **acquisition):
    """changes() over numpy arrays, at once: the same checks and the same bytes (the Changes encoding)."""
    import numpy as np
    q = np.asarray(quanta, np.int64)
    lv = np.asarray(levels, np.int64)
    c = np.asarray(counts, np.int64)
    if len(q):
        before = np.concatenate(([start_level], lv[:-1]))
        sat = np.minimum(c, 127)
        bad = (np.diff(q, prepend=-1) <= 0) | (q >= n) | ((lv != 0) & (lv != 1)) | (c < 1) | \
            ((sat < 127) & (lv != before ^ (sat & 1)))
        if step == 1:
            bad |= c != 1
        if bad.any():
            k = int(np.flatnonzero(bad)[0])
            raise ValueError(f"{name}: event (quantum {q[k]}, level {lv[k]}, count {c[k]}) out of order or impossible")
        gap = np.diff(q, prepend=-1) - 1
        nb = np.ones(len(q), np.int64)
        for j in range(1, 10):
            nb += gap >= (1 << (7 * j))
        at = np.concatenate(([0], np.cumsum(nb + 1)[:-1]))
        out = np.empty(int((nb + 1).sum()), np.uint8)
        for j in range(int(nb.max())):
            m = nb > j
            out[at[m] + j] = ((gap[m] >> (7 * j)) & 0x7F) | np.where(nb[m] > j + 1, 0x80, 0)
        out[at + nb] = (lv << 7) | sat
        data = out.tobytes()
    else:
        data = b""
    return Changes(name, data, n, step, 0, start_level, len(q), acquisition)


REPEAT_POLL_S = 0.002   # how often repeat looks for finished segments


def _repeat_result(cap, hst, cfg, req: Request, names, ids, probe, fast, open_s, clock_before, oc, oh) -> Result:
    """Repeat (capture §2.1 mode 2): req.segments acquisitions of req.samples, each read and released as soon as it
    is finished (the probe takes the next only into a free segment: none is lost, a pause is a gap in time). With a
    trigger each starts on its own (rearm 1): parts of layout "segments", each its trigger_tick and start_ns, tick0
    from the first one's time. Without, they run on: a timeline, the next part right after unless the probe paused
    (flags gap). A wait past req.timeout for the next one ends it: a trigger that does not come is named."""
    import time
    gap, slipped = oc._CAP.enum["segment_flag"]["gap"], oc._CAP.enum["segment_flag"]["slipped"]
    rate = Fraction(cfg.rate)
    if cfg.segments:
        probe["repeat"] = {"ring": cfg.segments}        # the probe's segments: it takes into a free one only
    parts, nxt, first_ns, prev, received = [], 0, None, None, 0
    t0 = time.monotonic()
    cap.start()
    try:
        last = kept = time.monotonic()
        while len(parts) < req.segments:
            now = time.monotonic()
            if now - kept >= 1.0 and hst.session is not None:
                hst.keepalive()
                kept = now
            st = cap.status()
            if st.state == oc.STATE["error"]:
                raise oh.Failed(None, f"the capture stopped with an error ({st.error_name or 'unknown'})")
            ready = [x for x in cap.segments(nxt) if x.serial < st.serial_done] if st.serial_done > nxt else []
            if not ready:
                if now - last > req.timeout:
                    what = f"no trigger ({req.trigger[0]}:{req.trigger[1]})" if req.trigger else "no segment"
                    raise TimeoutError(f"{what} within {req.timeout:g} s (--timeout) after {len(parts)} of "
                                       f"{req.segments} segments")
                time.sleep(REPEAT_POLL_S)
                continue
            for seg in sorted(ready, key=lambda x: x.serial):
                if len(parts) == req.segments:
                    break
                data = cap.read_segment(seg)
                received += len(data)
                cap.release(seg.serial, seg.generation or None)
                nxt = seg.serial + 1
                if getattr(cfg, "event_d", None):
                    chans = _from_blocks(cap, data, names, seg.samples, cfg)
                else:
                    chans = from_interleaved(data, names, cfg.width, cfg.positions, seg.samples)
                for c, ch in zip(chans, ids):
                    c.acquisition = {"pin": ch}
                meta = {"start_ns": seg.start_ns, "start_uncertainty_ns": seg.start_uncertainty_ns}
                if seg.trigger_index is not None:
                    meta["trigger_tick"] = seg.trigger_index
                if seg.flags & slipped:
                    meta["time_base_slipped"] = True
                if first_ns is None:
                    first_ns = seg.start_ns
                if prev is not None and not req.trigger and not seg.flags & gap:
                    tick0 = prev.tick0 + prev_samples                # right after the one before
                else:
                    tick0 = round(Fraction(seg.start_ns - first_ns, 10**9) * rate)
                prev, prev_samples = Part(chans, meta, tick0), seg.samples
                parts.append(prev)
            last = time.monotonic()
    finally:
        try:
            cap.stop()
        except Exception:                 # noqa: BLE001 - stopping is tidying up; the capture's own error is the news
            pass
    read_s = time.monotonic() - t0
    probe["link"] = _link_info(hst, fast, open_s, received, read_s)
    clock = _clock_info(clock_before, _clock(hst))
    if clock:
        probe["clock"] = clock
    if cap.generation is not None:
        probe["generation"] = {"logic": cap.generation}
    meta = {"probe_channels": dict(zip(names, ids)), "probe": probe,
            "repeat": {"segments": len(parts), "triggered": bool(req.trigger)}}
    if req.trigger:               # each should start on its own trigger (rearm 1): one that did not is named
        untriggered = [k for k, p in enumerate(parts) if "trigger_tick" not in p.meta]
        if untriggered:
            meta["repeat"]["without_trigger"] = untriggered
    return Result(rate, parts[0].channels, meta, parts, [], "segments" if req.trigger else "timeline")


STREAM_POLL_S = 0.2       # how often a stream looks for a loss while collecting
STREAM_MIN_BYTES = 16384  # a stream's notifications (core §10.3): at least this much a frame ...
STREAM_MAX_DELAY_MS = 20  # ... or after this long. With (0, 0) the P4X sent each format 1 block (tens of bytes on
                          # quiet lines) as a transfer of its own, 2000 a second, and dropped 35 % it could not send


def _collect_group(group, tracks, link, req: Request, oc, spool=None):
    """_collect for a group's streaming tracks (capture §4.1: each subscribed, started at once): -> ({fn: Received},
    seconds). The first track's Received goes to the spool as it comes; a loss in any track stops it with "stop"."""
    import time
    for t in tracks:
        t.subscribe(STREAM_MIN_BYTES, STREAM_MAX_DELAY_MS)
    group.start(tracks)
    t_read = time.monotonic()
    deadline, got = t_read + req.seconds, {t.fn: oc.Received() for t in tracks}
    lead = tracks[0].fn
    while True:
        now = time.monotonic()
        if now >= deadline:
            break
        got = group.stream(link, tracks, seconds=min(STREAM_POLL_S, deadline - now), into=got)
        if spool is not None:
            spool.take(got[lead])
        lost = (spool is not None and spool.lost) or any(
            g.gaps or (g.start or 0) > 0 for fn, g in got.items() if spool is None or fn != lead)
        if req.on_loss == "stop" and lost:
            break
    group.stop()
    got = group.finish(link, tracks, got)
    return got, time.monotonic() - t_read


def _analog_runs(an, acfg, got) -> tuple[list, list]:
    """An analog track's stream -> its gapless runs (start_ns, start_uncertainty_ns, bytes, samples) and the stream
    positions it skipped (each a loss: where, as the next run's start_ns or None)."""
    frame = acfg.slot // 8 * len(acfg.order)
    segs = sorted(got.segments.values(), key=lambda x: x.position)
    runs, skips, pos, at = [], [], got.start or 0, 0
    if got.start:
        skips.append(None)
    for index, skipped in sorted(got.gaps) + [(len(got.data), 0)]:
        n = index - at
        inside = [x for x in segs if pos <= x.position < pos + n]
        samples = n // frame
        if samples and inside:
            runs.append((inside[0].start_ns, inside[0].start_uncertainty_ns, bytes(got.data[at:at + samples * frame]),
                         samples))
        if skipped:
            skips.append(len(runs))
        pos += n + skipped
        at = index
    return runs, skips


def _collect(track, link, req: Request, oc, spool=None):
    """Subscribe, start, collect for req.seconds, stop and collect the rest -> (Received, seconds). A loss stops it
    at once with on_loss "stop"."""
    import time
    track.subscribe(STREAM_MIN_BYTES, STREAM_MAX_DELAY_MS)   # batched: a capture wants bandwidth, not a frame per block
    track.start()
    t_read = time.monotonic()
    deadline, got = t_read + req.seconds, oc.Received()
    while True:                                  # stream() renews the lease across calls on the same Received
        now = time.monotonic()
        if now >= deadline:
            break
        got = track.stream(link, seconds=min(STREAM_POLL_S, deadline - now), into=got)
        if spool is not None:                    # put together as it comes: memory holds a poll's worth, not the stream
            spool.take(got)
            if req.on_loss == "stop" and spool.lost:
                break
        elif req.on_loss == "stop" and (got.gaps or (got.start or 0) > 0):
            break
    track.stop()
    got = track.finish(link, got)
    return got, time.monotonic() - t_read


def _group_stream_result(cap, an, hst, cfg, acfg, req: Request, names, ids, aids, probe, fast, open_s, clock_before,
                         oc, calib, frontends):
    """Logic and analog streamed in one group (capture §4.1, §4.2: each track's pushes to its own fn): the logic put
    together as a stream alone is (its parts and losses), each part then given the analog samples of its time
    (placed by the segments' start_ns); an analog loss is a loss of the capture too."""
    group = oc.CaptureGroup(hst)
    group.bind([cap, an])
    try:
        if getattr(cfg, "stream_buffer", None) is not None:
            probe["stream_buffer"] = {"bytes": cfg.stream_buffer, "tolerable_delay_s": cfg.tolerable_delay_s(None)}
        spool = _Spool(cap, cfg, names, ids, req) if _spools(cfg) else None
        got, read_s = _collect_group(group, [cap, an], hst.link, req, oc, spool)
    finally:
        group.bind([])
    lg, ag = got[cap.fn], got[an.fn]
    st, ast = cap.status(), an.status()
    if spool is not None:
        spool.give(lg, write_pos=st.write_pos if st.state == oc.STATE["error"] else None)
    drops = oc.CaptureDrops.of(st) if hasattr(oc, "CaptureDrops") else None
    if drops is not None:
        import dataclasses
        probe["drops"] = dataclasses.asdict(drops)
    received = (spool.received if spool is not None else len(lg.data)) + len(ag.data)
    tail = None
    if st.state != oc.STATE["error"]:
        end = spool.end if spool is not None else (lg.start or 0) + len(lg.data) + sum(n for _, n in lg.gaps)
        if st.write_pos > end:
            tail = st.write_pos - end
    probe["link"] = _link_info(hst, fast, open_s, received, read_s)
    clock = _clock_info(clock_before, _clock(hst))
    if clock:
        probe["clock"] = clock
    probe["generation"] = {k: t.generation for k, t in (("logic", cap), ("analog", an)) if t.generation is not None}
    if calib is not None and calib.factory:
        probe["calibration"] = [{"frontend": fe, "scheme": scheme, "raw": bytes(raw).hex()}
                                for fe, scheme, raw in calib.factory]
    meta = {"probe_channels": {**dict(zip(names, ids)), **dict(zip([n for n, *_ in req.analog], aids))},
            "probe": probe,
            "stream": {"duration_s": req.seconds, "on_loss": req.on_loss, "frames": lg.frames + ag.frames,
                       "bytes": received, "read_s": round(read_s, 3)}}
    logic = _Streamed(cap, cfg, lg, st, names, ids, req, meta, oc, spool, tail)
    aend = (ag.start or 0) + len(ag.data) + sum(n for _, n in ag.gaps)
    atail = ast.write_pos - aend if ast.state != oc.STATE["error"] and ast.write_pos > aend else None
    return _GroupStreamed(logic, an, acfg, ag, ast, req, aids, calib, frontends, atail, oc)


class _GroupStreamed:
    """A group stream collected: the logic put together (after the session), then each part given its analog."""
    def __init__(self, logic, an, acfg, ag, ast, req, aids, calib, frontends, atail, oc):
        self.logic, self.an, self.acfg, self.ag, self.ast, self.req, self.aids = logic, an, acfg, ag, ast, req, aids
        self.calib, self.frontends, self.atail, self.oc = calib, frontends, atail, oc

    def result(self) -> Result:
        res = self.logic.result()
        tick = Fraction(res.tick_hz)
        arate = Fraction(self.acfg.rate)
        runs, skips = _analog_runs(self.an, self.acfg, self.ag)
        frame = self.acfg.slot // 8 * len(self.acfg.order)
        first_ns = min((p.meta.get("start_ns") for p in res.parts if p.meta.get("start_ns") is not None), default=None)
        for part in res.parts:
            s_ns = part.meta.get("start_ns")
            span = Fraction(max(c.end if hasattr(c, "end") else c.n for c in part.channels), 1) / tick
            best = None                          # the analog run that covers most of this part's time
            if s_ns is not None:
                for r0, unc, data, n in runs:
                    a = max(Fraction(s_ns, 10**9), Fraction(r0, 10**9))
                    b = min(Fraction(s_ns, 10**9) + span, Fraction(r0, 10**9) + n / arate)
                    if b > a and (best is None or b - a > best[0]):
                        best = (b - a, r0, unc, data, n, a, b)
            if best is None:
                part.channels += _analog_chans(self.an, self.acfg, self.req, self.aids, b"", 0, Fraction(0), tick,
                                               self.frontends, self.calib, None, None)
                continue
            _, r0, unc, data, n, a, b = best
            k0 = max(0, -(-((a - Fraction(r0, 10**9)) * arate) // 1))
            k1 = min(n, int((b - Fraction(r0, 10**9)) * arate))
            t0 = (Fraction(r0, 10**9) + k0 / arate - Fraction(s_ns, 10**9)) * tick
            part.channels += _analog_chans(self.an, self.acfg, self.req, self.aids,
                                           data[k0 * frame:k1 * frame], max(0, k1 - k0), t0, tick,
                                           self.frontends, self.calib, unc, None)
        lost = list(res.lost)
        for k in skips:                          # analog losses: after the logic part its run starts in, roughly
            at = len(res.parts) - 1
            if k is not None and k < len(runs) and first_ns is not None:
                r0 = runs[k][0]
                at = max([i for i, p in enumerate(res.parts) if (p.meta.get("start_ns") or 0) <= r0] or [0])
            lost.append({"after_part": -1 if k is None else at, "reason": "analog: the probe dropped a segment (no "
                         "room to send)"})
        if self.ast.state == self.oc.STATE["error"]:
            lost.append({"after_part": len(res.parts) - 1, "stopped": True,
                         "reason": f"analog: the probe stopped in error ({self.ast.error_name})"})
        if self.atail:
            lost.append({"after_part": len(res.parts) - 1, "stopped": True,
                         "reason": f"analog: the stream's last {self.atail} bytes did not arrive after the stop"})
        return Result(res.tick_hz, res.parts[0].channels, res.meta, res.parts, lost, res.layout, res.temp_dir)


def _analog_stream_result(an, hst, acfg, req: Request, aids, probe, fast, open_s, clock_before, oc, calib,
                          frontends) -> Result:
    """An analog track alone, streamed (capture §2.1 mode 3): its frames of values joined per gapless stretch, a part
    each on one timeline (tick: the analog rate), placed by the segments' start_ns; a skipped position is a loss."""
    if getattr(acfg, "stream_buffer", None) is not None:
        probe["stream_buffer"] = {"bytes": acfg.stream_buffer, "tolerable_delay_s": acfg.tolerable_delay_s(None)}
    got, read_s = _collect(an, hst.link, req, oc)
    st = an.status()
    drops = oc.CaptureDrops.of(st) if hasattr(oc, "CaptureDrops") else None
    if drops is not None:
        import dataclasses
        probe["drops"] = dataclasses.asdict(drops)
    tail = None
    end = (got.start or 0) + len(got.data) + sum(n for _, n in got.gaps)
    if st.state != oc.STATE["error"] and st.write_pos > end:
        tail = st.write_pos - end
    probe["link"] = _link_info(hst, fast, open_s, len(got.data), read_s)
    clock = _clock_info(clock_before, _clock(hst))
    if clock:
        probe["clock"] = clock
    if an.generation is not None:
        probe["generation"] = {"analog": an.generation}
    if calib is not None and calib.factory:
        probe["calibration"] = [{"frontend": fe, "scheme": scheme, "raw": bytes(raw).hex()}
                                for fe, scheme, raw in calib.factory]
    names = [n for n, *_ in req.analog]
    meta = {"probe_channels": dict(zip(names, aids)), "probe": probe,
            "stream": {"duration_s": req.seconds, "on_loss": req.on_loss, "frames": got.frames,
                       "bytes": len(got.data), "read_s": round(read_s, 3)}}

    def build():
        if not got.data or not got.segments:
            raise RuntimeError("the stream brought no data" + (" (the probe dropped it all)" if got.gaps else ""))
        frame = acfg.slot // 8 * len(acfg.order)
        rate = Fraction(acfg.rate)
        segs = sorted(got.segments.values(), key=lambda x: x.position)
        first_ns = segs[0].start_ns
        parts, lost, pos, at = [], [], got.start, 0
        if got.start != 0:
            if req.on_loss == "stop":
                raise RuntimeError(f"the probe dropped the stream's first {got.start} bytes before any arrived")
            lost.append({"after_part": -1, "reason": f"the probe dropped the first {got.start} bytes (no room to send)"})
        for r, (index, skipped) in enumerate(sorted(got.gaps) + [(len(got.data), 0)]):
            n = index - at
            if r and req.on_loss == "stop":
                lost[-1]["stopped"] = True
                break
            inside = [x for x in segs if pos <= x.position < pos + n] or [None]
            samples = n // frame
            if samples:
                x = inside[0]
                chans = _analog_chans(an, acfg, req, aids, bytes(got.data[at:at + samples * frame]), samples,
                                      Fraction(0), rate, frontends, calib,
                                      x.start_uncertainty_ns if x else None, None)
                tick0 = round(Fraction(x.start_ns - first_ns, 10**9) * rate) if x else \
                    (parts[-1].tick0 + parts[-1].channels[0].n if parts else 0)
                parts.append(Part(chans, _seg_meta(x) if x else {}, tick0))
            if skipped:
                lost.append({"after_part": len(parts) - 1, "reason": "the probe dropped a segment (no room to send)"})
            pos += n + skipped
            at = index
        if not parts:
            raise RuntimeError("the stream brought no whole sample")
        return parts, lost

    return _Streamed(an, acfg, got, st, names, aids, req, meta, oc, None, tail, build)


def _stream_result(cap, hst, cfg, req: Request, names, ids, probe, fast, open_s, clock_before, oc) -> Result:
    """A stream (capture §2.1 mode 3): subscribe, start, collect for req.seconds, stop and collect the rest. A loss
    (a skipped position: the probe dropped a segment it had no room to send) stops it at once with on_loss "stop"
    (the default), else it goes on; either way the capture is incomplete, a failure (parts and losses)."""
    import time
    link = hst.link
    if getattr(cfg, "stream_buffer", None) is not None:   # how long this host may pause (§3.3), before it starts
        worst = None
        if getattr(cfg, "event_d", None):                 # format 1: over its worst bandwidth, a lower bound
            from oep_client import capture_blocks
            worst = capture_blocks.worst_bytes(len(cfg.raw_roles), list(cfg.event_d.values()), cfg.block) \
                * float(cfg.rate) / cfg.block
        probe["stream_buffer"] = {"bytes": cfg.stream_buffer, "tolerable_delay_s": cfg.tolerable_delay_s(worst)}
    spool = _Spool(cap, cfg, names, ids, req) if _spools(cfg) else None
    got, read_s = _collect(cap, link, req, oc, spool)
    st = cap.status()
    if spool is not None:                        # handed over now; waited for once the session is closed (the worker
        spool.give(got, write_pos=st.write_pos if st.state == oc.STATE["error"] else None)   # may be behind)
    drops = oc.CaptureDrops.of(st) if hasattr(oc, "CaptureDrops") else None   # the reference firmware's own
    if drops is not None:
        import dataclasses
        probe["drops"] = dataclasses.asdict(drops)
    received = spool.received if spool is not None else len(got.data)
    tail = None
    if st.state != oc.STATE["error"]:            # stopped: every byte up to write_pos should have come (§2.1 rule 2)
        end = spool.end if spool is not None else (got.start or 0) + len(got.data) + sum(n for _, n in got.gaps)
        if st.write_pos > end:
            tail = st.write_pos - end
    probe["link"] = _link_info(hst, fast, open_s, received, read_s)
    clock = _clock_info(clock_before, _clock(hst))
    if clock:
        probe["clock"] = clock
    if cap.generation is not None:
        probe["generation"] = {"logic": cap.generation}
    meta = {"probe_channels": dict(zip(names, ids)), "probe": probe,
            "stream": {"duration_s": req.seconds, "on_loss": req.on_loss, "frames": got.frames,
                       "bytes": received, "read_s": round(read_s, 3)}}
    return _Streamed(cap, cfg, got, st, names, ids, req, meta, oc, spool, tail)


class _Streamed:
    """A stream collected, to be put together once the session is closed (decoding takes time; the lease does not
    wait for it). cap.blocks only reads the configure answer kept in the client."""
    def __init__(self, cap, cfg, got, st, names, ids, req, meta, oc, spool=None, tail=None, build=None):
        self.cap, self.cfg, self.got, self.st, self.names, self.ids, self.req, self.meta, self.oc = (
            cap, cfg, got, st, names, ids, req, meta, oc)
        self.spool, self.tail, self.build = spool, tail, build

    def result(self) -> Result:
        if self.spool is not None:
            self.spool.finish()                  # after the session closed: the lease does not wait for it
            parts, lost = self.spool.parts, self.spool.done_lost
            if not parts:
                raise RuntimeError("the stream brought no data" + (" (the probe dropped it all)" if lost else ""))
        elif self.build is not None:             # an analog stream: its own parts
            parts, lost = self.build()
        else:
            parts, lost = _stream_parts(self.cap, self.cfg, self.got, self.names, self.ids, self.req)
        st = self.st
        why = _drops_line(self.meta["probe"].get("drops"))
        if st.state == self.oc.STATE["error"]:
            lost.append({"after_part": len(parts) - 1, "reason": f"the probe stopped in error ({st.error_name})",
                         "stopped": True})
        elif st.dropped and not lost:
            lost.append({"after_part": len(parts) - 1, "reason": "the probe dropped data (status flags bit 0)"})
        if self.tail:                            # the end did not come before finish() gave up: a loss too
            lost.append({"after_part": len(parts) - 1, "stopped": True,
                         "reason": f"the stream's last {self.tail} bytes did not arrive after the stop"})
        if why and lost:                         # where the probe says the data went (its own diagnostics; the
            lost[-1]["reason"] += f" - the probe's counts since start: {why}"    # counters are totals: said once)
        return Result(Fraction(self.cfg.rate), parts[0].channels, self.meta, parts, lost, "timeline",
                      self.spool.dir if self.spool is not None else None)


def _spools(cfg) -> bool:
    """Whether a stream is put together on disk as it comes (_Spool): format 0 with whole bytes a sample, and format
    1 (segment by segment). Format 0 with a sample across bytes is put together at the end, in memory."""
    return bool(getattr(cfg, "event_d", None)) or cfg.width % 8 == 0


class _Spool:
    """A stream put together while it comes (parts and losses parts on a timeline): after each poll the bytes
    that arrived are taken out of the client's Received (its start moved on, so it goes on counting positions), the
    channels' bits appended to files by the output and the events kept; a skipped position ends the part (a loss)
    and the next part starts where the bytes do again. Memory holds about a poll's worth, whatever the length."""

    def __init__(self, cap, cfg, names, ids, req: Request):
        import tempfile
        self.cap, self.cfg, self.names, self.ids, self.req = cap, cfg, names, ids, req
        self.dir = tempfile.mkdtemp(prefix=".oep-acquire-stream-", dir=req.spool_dir)
        self.blocks = bool(getattr(cfg, "event_d", None))
        self.ds = list(getattr(cfg, "event_d", {}).values())
        self.rate = Fraction(cfg.rate)
        self.parts, self.lost = [], []
        self.pending = bytearray()      # bytes from position `at` not put together yet (a gapless stretch)
        self.at = None                  # stream position of pending[0]
        self.bytes = 0
        self.received = 0               # bytes handed over (the caller's count; `bytes` is the worker's)
        self.end = 0                    # the stream position just past what arrived
        self.segs = {}                  # segment position -> SegmentInfo (the latest)
        self.first_ns = None
        self.cur = None                 # the part being put together: dict (tick0, meta, samples, files, events, ...)
        self.stopped = False
        import queue
        import threading
        self._queue, self._error = queue.Queue(), None
        self._worker = threading.Thread(target=self._drain, name="oep-acquire-spool", daemon=True)
        self._worker.start()

    # -- taking what arrived (the caller's thread: O(1), so its polls keep coming) --
    def give(self, got, write_pos: int | None = None) -> None:
        """The last of the stream: handed over with the end mark, not waited for (finish() waits)."""
        self.take(got)
        self._queue.put(("final", write_pos))
        self._given = True

    def finish(self) -> None:
        """Wait for the worker to put everything together (its error raised here)."""
        self._worker.join()
        if self._error is not None:
            raise self._error

    _given = False

    def take(self, got, final: bool = False, write_pos: int | None = None) -> None:
        """Hand what arrived to the worker: the client's buffers are swapped for empty ones (no copy) and its start
        moved on, so it goes on counting positions. final: wait for the worker to finish (its error raised here)."""
        segs, got.segments = got.segments, {}
        data, gaps, start = got.data, sorted(got.gaps), got.start
        if start is not None:
            got.data, got.gaps = bytearray(), []
            got.start = start + len(data) + sum(n for _, n in gaps)
            self.received += len(data)
            self.end = got.start                 # the position just past what arrived
        self._queue.put((segs, start, data, gaps))
        if final:
            self._queue.put(("final", write_pos))
            self._worker.join()
            if self._error is not None:
                raise self._error

    def _drain(self) -> None:
        try:
            while True:
                item = self._queue.get()
                if item[0] == "final":
                    self._work(True, item[1])
                    self._close()
                    return
                segs, start, data, gaps = item
                self.segs.update(segs)
                if start is not None and data:
                    pos, i = start, 0
                    for index, skipped in gaps + [(len(data), 0)]:
                        self._run(pos, memoryview(data)[i:index])
                        pos += index - i + skipped
                        i = index
                self._work(False, None)
        except BaseException as e:               # noqa: BLE001 - raised in the caller's thread at the end
            self._error = e

    def _run(self, p0: int, data: bytes) -> None:
        if self.stopped or not data:
            return
        self.bytes += len(data)
        if self.at is None and not self.parts and self.cur is None:
            if p0 != 0:                          # positions count from the generation's start: the first is gone
                if self.req.on_loss == "stop":
                    raise RuntimeError(f"the probe dropped the stream's first {p0} bytes before any arrived: nothing "
                                       f"to keep (a lower --rate or fewer channels, or --on-loss continue)")
                self.lost.append({"after_part": -1, "reason": f"the probe dropped the first {p0} bytes (no room to send)"})
            self.at = p0
        elif p0 != self.at + len(self.pending):  # a skipped position: the probe dropped a segment
            self._work(True, None)
            self._close()
            stop = self.req.on_loss == "stop"
            self.lost.append({"after_part": len(self.parts) - 1,
                              "reason": "the probe dropped a segment (no room to send)", "_next": p0,
                              **({"stopped": True} if stop else {})})
            if stop:
                self.stopped = True
                return
            self.at, self.pending = p0, bytearray()
        self.pending += data

    # -- putting together --
    def _work(self, final: bool, write_pos: int | None) -> None:
        if self.at is None or not self.pending:
            return
        if self.blocks:
            self._segments(final, write_pos)
        else:
            self._samples(final, write_pos)

    def _samples(self, final: bool, write_pos: int | None) -> None:
        b = self.cfg.width // 8
        n = len(self.pending) // b
        if write_pos is not None:                # an error stop: nothing at or past write_pos is data (§2.2)
            n = min(n, max(0, (write_pos - self.at) // b))
        if not final:
            n = n // 8 * 8                       # whole bytes of bits; the rest waits for the next poll
        if not n:
            return
        self._open(self.at)
        chans = _samples_at_once(memoryview(self.pending), self.names, self.ids, self.cfg, n)
        for c, f in zip(chans, self.cur["files"]):
            f.write(c.bits)
        self.cur["samples"] += n
        del self.pending[:n * b]
        self.at += n * b
        for q in [q for q in self.segs if q < self.at and q != self.cur.get("first", self.at)]:
            del self.segs[q]                     # put together: only a part's start needs its record
        if final:
            self.pending = bytearray()

    def _segments(self, final: bool, write_pos: int | None) -> None:
        end = self.at + len(self.pending)
        starts = sorted(p for p in self.segs if self.at <= p < end)
        ready = []
        for k, p in enumerate(starts):
            nxt = starts[k + 1] if k + 1 < len(starts) else (end if final else None)
            if nxt is None or (write_pos is not None and p >= write_pos):
                break
            ready.append((p, nxt))
        L = self.cfg.block
        k = 0
        while k < len(ready):                    # segments of whole blocks run on: their blocks read in one go
            j = k
            while j + 1 < len(ready) and self.segs[ready[j][0]].samples % L == 0:
                j += 1
            p, nxt = ready[k][0], ready[j][1]
            x = self.segs[p]
            samples = sum(self.segs.pop(q).samples for q, _ in ready[k:j + 1])
            if self.cur is not None and (self.cur["samples"] % 8 or any(self.cur["samples"] % d for d in self.ds)):
                self._close(follows=True)      # a grid restart (format 1) is not a loss: the next part follows on
            self._open(p, x)
            data = bytes(self.pending[p - self.at:nxt - self.at])
            cols = _segment_columns(self.cap, data, samples, len(self.names), self.cfg)
            for col, f, ev in zip(cols, self.cur["files"], self.cur["events"]):
                if isinstance(col, bytes):
                    f.write(col[:(samples + 7) // 8])
                else:
                    ev[0].append(col[0] + self.cur["samples"] // col[4])
                    ev[1].append(col[1])
                    ev[2].append(col[2])
                    if ev[3] is None:
                        ev[3] = col[3]
            self.cur["samples"] += samples
            del self.pending[:nxt - self.at]
            self.at = nxt
            k = j + 1
        if final:
            self.pending = bytearray()

    def _open(self, position: int, seg=None) -> None:
        if self.cur is not None:
            return
        import os
        seg = seg or self.segs.get(position)
        if self.first_ns is None and seg is not None:
            self.first_ns = seg.start_ns
        if self.parts and self.parts[-1] is not None and getattr(self, "_follows", False):
            tick0 = self.parts[-1].tick0 + self._last_samples
        elif seg is not None and self.first_ns is not None:
            tick0 = round(Fraction(seg.start_ns - self.first_ns, 10**9) * self.rate)
        else:                                    # format 0 without the segment's record: from the bytes
            tick0 = (self.parts[-1].tick0 + self._last_samples if self.parts else 0) + \
                (position - (self._last_end or 0)) * 8 // self.cfg.width
        k = len(self.parts)
        files = [open(os.path.join(self.dir, f"p{k}-{j}.bits"), "wb") for j in range(len(self.names))]
        self.cur = {"tick0": tick0, "meta": _seg_meta(seg) if seg is not None else {}, "samples": 0, "files": files,
                    "events": [[[], [], [], None] for _ in self.names]}
        self._follows = False

    _last_samples, _last_end, _follows = 0, None, False

    def _close(self, follows: bool = False) -> None:
        if self.cur is None:
            return
        import os
        cur, n = self.cur, self.cur["samples"]
        chans = []
        for role, (name, ch, f, ev) in enumerate(zip(self.names, self.ids, cur["files"], cur["events"])):
            f.close()
            d = self.cfg.event_d.get(role) if self.blocks else None
            if d is not None:
                os.unlink(f.name)
                import numpy as np
                cat = [np.concatenate(x) if x else np.zeros(0, np.int64) for x in ev[:3]]
                chans.append(_events_np(name, *cat, ev[3] or 0, -(-n // d), d, pin=ch))
            else:
                os.truncate(f.name, (n + 7) // 8)
                chans.append(Bits(name, OnDisk(f.name), n, 1, 0, {"pin": ch}))
        if n:
            self.parts.append(Part(chans, dict(cur["meta"]), int(cur["tick0"])))
        self._last_samples, self._last_end = n, self.at
        self.cur = None
        self._follows = follows
        for x in self.lost:                      # how long the loss before this part was, when the times say
            if x.get("_next") is not None and x["after_part"] == len(self.parts) - 2 and len(self.parts) >= 2:
                gap = self.parts[-1].tick0 - self.parts[-2].tick0 - self.parts[-2].channels[0].end
                if gap > 0:
                    x["ticks"] = int(gap)
                x.pop("_next")

    @property
    def done_lost(self):
        return [{k: v for k, v in x.items() if k != "_next"} for x in self.lost]


DROP_COUNTS = ("no_stage_bytes", "unqueued_bytes", "dma_overruns", "queue_overflows")


def _drops_line(drops: dict | None) -> str:
    """The reference firmware's capture-drops counters that are not 0, in one line."""
    if not drops:
        return ""
    return ", ".join(f"{k} {drops[k]}" for k in DROP_COUNTS if drops.get(k))


def _stream_parts(cap, cfg, got, names, ids, req: Request) -> tuple[list, list]:
    """What a stream collected -> parts on one timeline and the losses (parts and losses). Segments run in
    order; a skipped position is a loss, and a part ends there (with on_loss "stop", the capture too). Each segment
    is read as a one-shot one is and joined to the part; tick0 within a part is exact (samples), across a loss from
    the segments' start_ns. A format 1 segment whose samples are not a multiple of every D starts a new part
    without a loss (the next segment's ticks restart, capture §1.3)."""
    import numpy as np
    if not got.data or not got.segments:
        raise RuntimeError("the stream brought no data" + (" (the probe dropped it all)" if got.gaps else ""))
    runs, pos, at = [], got.start, 0            # (stream position, data index, length) of each gapless stretch
    marks = sorted(got.gaps) + [(len(got.data), 0)]
    for index, skipped in marks:
        runs.append((pos, at, index - at))
        pos += index - at + skipped
        at = index
    segs = sorted(got.segments.values(), key=lambda x: x.position)
    rate = Fraction(cfg.rate)
    first_ns = segs[0].start_ns
    ds = [d for d in getattr(cfg, "event_d", {}).values()]
    parts, lost = [], []
    if got.start != 0:                           # positions count from the generation's start: the first is gone
        if req.on_loss == "stop":
            raise RuntimeError(f"the probe dropped the stream's first {got.start} bytes before any arrived: nothing "
                               f"to keep (a lower --rate or fewer channels, or --on-loss continue)")
        lost.append({"after_part": -1, "reason": f"the probe dropped the first {got.start} bytes (no room to send)"})
    for r, (p0, i0, n) in enumerate(runs):
        if r and req.on_loss == "stop":
            lost.append({"after_part": len(parts) - 1, "reason": "the probe dropped a segment (no room to send)",
                         "stopped": True})
            break
        if r:
            lost.append({"after_part": len(parts) - 1, "reason": "the probe dropped a segment (no room to send)"})
        inside = [x for x in segs if p0 <= x.position < p0 + n]
        run = memoryview(got.data)[i0:i0 + n]   # no copy: a 30 s stream at 40 MB/s is 1.2 GB
        if not ds and cfg.width % 8 == 0:        # format 0, whole bytes a sample: the stretch at once (no padding)
            samples = min(sum(x.samples for x in inside), len(run) * 8 // cfg.width)
            parts.append(_part(_samples_at_once(run, names, ids, cfg, samples), _seg_meta(inside[0]),
                               round(Fraction(inside[0].start_ns - first_ns, 10**9) * rate)))
            continue
        acc, acc_samples, acc_meta, tick0 = None, 0, None, None
        for k, x in enumerate(inside):
            end_ = inside[k + 1].position if k + 1 < len(inside) else p0 + n
            data = bytes(run[x.position - p0:end_ - p0])
            samples = x.samples
            if not ds:
                samples = min(samples, len(data) * 8 // cfg.width)
                chans = from_interleaved(data, names, cfg.width, cfg.positions, samples)
            else:
                chans = _from_blocks(cap, data, names, samples, cfg)
            restart = acc is not None and (acc_samples % 8 or any(acc_samples % d for d in ds))
            if acc is None or restart:           # a grid restart (format 1) is not a loss: the next part follows on
                if acc is not None:
                    parts.append(_part(_built(acc, acc_samples, ids), acc_meta, tick0))
                    tick0 += acc_samples
                else:
                    tick0 = round(Fraction(x.start_ns - first_ns, 10**9) * rate)
                acc, acc_samples, acc_meta = [_Acc(c) for c in chans], 0, _seg_meta(x)
            for a_, c in zip(acc, chans):
                a_.add(c, acc_samples)
            acc_samples += samples
        if acc is not None:
            parts.append(_part(_built(acc, acc_samples, ids), acc_meta, tick0))
    for x in lost:                               # how long, when the segments' times say
        k = x["after_part"]
        if 0 <= k < len(parts) - 1:
            gap = parts[k + 1].tick0 - parts[k].tick0 - parts[k].channels[0].end
            if gap > 0:
                x["ticks"] = int(gap)
    return parts, lost


def _part(chans, meta, tick0):
    return Part(chans, dict(meta), int(tick0))


def _seg_meta(x) -> dict:
    return {"start_ns": x.start_ns, "start_uncertainty_ns": x.start_uncertainty_ns}


AT_ONCE = 1 << 22         # samples unpacked at a time (a sample's byte per channel would be GBs for a long stream)
WORDS_AT_ONCE = 1 << 15   # 64-bit words of samples worked on at a time (256 KiB: stays in the cache)


def _samples_at_once(run, names, ids, cfg, samples: int) -> list:
    """Format 0 with whole bytes a sample: channel k is bit pos[k] of each little-endian sample, packed a chunk of
    AT_ONCE samples at a time (a multiple of 8, so the chunks' bytes join as they are)."""
    import numpy as np
    b = cfg.width // 8
    if b == 1:                   # 8 samples a 64-bit word: each channel's bits gathered into a byte by one multiply
        whole = samples // 8 * 8
        words = np.frombuffer(run, "<u8", whole // 8)
        planes = np.empty((len(names), whole // 8), np.uint8)
        lo, mul, sh = np.uint64(0x0101010101010101), np.uint64(0x0102040810204080), np.uint64(56)
        tmp = np.empty(WORDS_AT_ONCE, np.uint64)
        for at in range(0, len(words), WORDS_AT_ONCE):       # a cache-sized run of words at a time
            w = words[at:at + WORDS_AT_ONCE]
            t = tmp[:len(w)]
            for k, pos in enumerate(cfg.positions):
                np.right_shift(w, np.uint64(pos), out=t)
                np.bitwise_and(t, lo, out=t)
                np.multiply(t, mul, out=t)
                np.right_shift(t, sh, out=t)
                planes[k, at:at + len(w)] = t
        out = []
        for k, (name, ch, pos) in enumerate(zip(names, ids, cfg.positions)):
            bits = planes[k].tobytes()
            if whole < samples:              # the last few samples
                tail = np.frombuffer(run, np.uint8, samples - whole, whole)
                bits += np.packbits((tail >> pos) & 1, bitorder="little").tobytes()
            out.append(Bits(name, bits, samples, 1, 0, {"pin": ch}))
        return out
    a = np.frombuffer(run, np.uint8, samples * b).reshape(samples, b)
    pos = np.array(cfg.positions)
    packed = [bytearray() for _ in names]
    for at in range(0, samples, AT_ONCE):
        bits = np.unpackbits(a[at:at + AT_ONCE], axis=1, bitorder="little")   # (samples, w): bit j of each sample
        planes = np.packbits(np.ascontiguousarray(bits[:, pos].T), axis=1, bitorder="little")   # (channels, bytes)
        for k in range(len(names)):
            packed[k] += planes[k].tobytes()
    return [Bits(name, bytes(bits), samples, 1, 0, {"pin": ch})
            for name, ch, bits in zip(names, ids, packed)]


class _Acc:
    """One channel of a part being joined from its segments (bits: whole bytes, the part's samples so far a multiple
    of 8; events: quanta offset by the samples so far, a multiple of D). A segment that follows on carries the level
    before it as its start (§1.3), so its changes join the part's as they are."""
    def __init__(self, c):
        self.first, self.pieces = c, []
        self.start = getattr(c, "start_level", None)

    def add(self, c, offset: int) -> None:
        if isinstance(c, Changes):
            ks, lv, ct = c.decode()
            q = offset // c.step
            self.pieces.append(([q + k for k in ks], lv, ct, c.n))
        else:
            self.pieces.append((c.bits, c.n))


def _built(acc: list, samples: int, ids) -> list:
    out = []
    for a, ch in zip(acc, ids):
        c = a.first
        if isinstance(c, Changes):
            ks, lv, ct = [], [], []
            for k_, l_, c_, _ in a.pieces:
                ks += k_
                lv += l_
                ct += c_
            out.append(changes(c.name, ks, lv, ct, -(-samples // c.step), c.step, 0, a.start, pin=ch))
        else:
            out.append(Bits(c.name, b"".join(b for b, _ in a.pieces)[:(samples + 7) // 8], samples, 1, 0,
                                          {"pin": ch}))
    return out


def _budget(oc, cfg, got: int, samples: int, channels: int) -> dict:
    """meta.probe.data: the bytes the probe sent for the segment, the most §1.3's formula allows for it, and what
    format 0 would have taken (1 bit a channel a sample, at least)."""
    from oep_client import capture_blocks
    L = cfg.block
    worst_block = capture_blocks.worst_bytes(len(cfg.raw_roles), list(cfg.event_d.values()), L)
    return {"format": int(cfg.format), "block": L, "bytes": got, "worst_bytes": -(-samples // L) * worst_block,
            "format0_bytes": -(-samples * channels // 8)}


def _segment(track):
    segments = track.segments()
    if not segments:
        raise RuntimeError("the probe finished without a segment")
    return segments[0]


def _times(meta: dict, seg) -> None:
    meta["start_ns"] = seg.start_ns
    meta["start_uncertainty_ns"] = seg.start_uncertainty_ns
