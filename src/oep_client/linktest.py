"""Link measurement: run traffic patterns over a probe's link and count what breaks, at the speed in force or at rates
the host asks for with port_speed (oep-core §3.5). Every parameter is the caller's - rates (or none: the speed in
force), patterns, in-flight counts, frame sizes, how long - so the limits of a bridge, a cable or a probe can be found
case by case instead of with fixed numbers.

  from oep_client import link, linktest
  hst = link.open_host("/dev/ttyUSB0")
  core.take(hst, 30000, owner="linktest")
  for row in linktest.matrix(hst, rates=[None, 921600, 500000], patterns=["in", "out", "duplex"],
                             inflight=[1, 2], sizes=[128, 496], frames=300):
      print(row.text())

Patterns: "in" link_source (probe -> host), "out" link_sink (host -> probe), "duplex" the two alternating (both ways at
once when more than one is in flight). A frame is "ok" when its answer came whole and right, "broken" when an answer
came but its content was wrong, "lost" when no good answer came (a broken frame on a held serial port counts here:
the link drops it as noise). After a lost frame the link is resynchronised with a confirm before going on.
"""

from __future__ import annotations

import struct
import time
from dataclasses import dataclass, field

from . import cobs, core, message as m
from .link import FramingLost

LINK_SOURCE, LINK_SINK, PORT_SPEED = 0x40, 0x41, 0x14
PATTERNS = ("in", "out", "duplex")


@dataclass
class Cell:
    """One (rate, pattern, in-flight, size) run."""
    rate: int
    pattern: str
    inflight: int
    size: int
    frames: int = 0
    ok: int = 0
    broken: int = 0
    lost: int = 0
    seconds: float = 0.0
    kb_s: float = 0.0
    error: str = ""

    @property
    def error_rate(self) -> float:
        return (self.broken + self.lost) / self.frames if self.frames else 0.0

    def text(self) -> str:
        if self.error:
            return f"{self.rate:>8} {self.pattern:<6} x{self.inflight} {self.size:>4} B  {self.error}"
        return (f"{self.rate:>8} {self.pattern:<6} x{self.inflight} {self.size:>4} B  {self.kb_s:7.1f} KB/s  "
                f"ok {self.ok:>5}  broken {self.broken:>4}  lost {self.lost:>4}  ({self.error_rate * 100:5.2f} %)")


@dataclass
class RateResult:
    rate: int                    # the rate asked (the speed in force when None was asked)
    actual: int | None = None    # what the probe said it runs at (port_speed's answer)
    switched: bool = False
    why: str = ""
    cells: list[Cell] = field(default_factory=list)

    def text(self) -> str:
        head = f"rate {self.rate}" + (f" (actual {self.actual})" if self.actual else "") + (f": {self.why}" if self.why else "")
        return "\n".join([head] + ["  " + c.text() for c in self.cells])


def run(hst, pattern: str, inflight: int, size: int, *, frames: int = 300, seconds: float | None = None,
        timeout: float = 0.3, rate: int = 0) -> Cell:
    """One pattern at the speed in force: `frames` requests (or for `seconds`), `inflight` at a time, `size` bytes each."""
    if pattern not in PATTERNS:
        raise ValueError(f"pattern {pattern!r}: one of {PATTERNS}")
    lk = hst.link
    cell = Cell(rate or getattr(lk, "baud", 0) or 0, pattern, inflight, size)
    data = bytes(k & 0xFF for k in range(size))
    saved = lk.timeout, lk.resend, getattr(lk, "fallback", None)
    lk.timeout, lk.resend = timeout, False
    if saved[2] is not None:
        lk.fallback = False
    window = (hst.limits or hst.confirm())["window"]
    t0 = time.perf_counter()
    try:
        while (cell.frames < frames) if seconds is None else (time.perf_counter() - t0 < seconds):
            batch = inflight * 2 if seconds is not None else min(inflight * 2, frames - cell.frames)
            kinds = [pattern if pattern != "duplex" else ("in" if (cell.frames + k) % 2 == 0 else "out")
                     for k in range(batch)]
            msgs = [m.Request(hst.next_corr(), m.CORE_FN, LINK_SOURCE if k == "in" else LINK_SINK,
                              struct.pack("<I", size) if k == "in" else data).pack() for k in kinds]
            replies: list[bytes] = []
            try:
                lk._exchange_once(msgs, inflight, window, replies)
            except (cobs.CorruptFrame, TimeoutError, FramingLost):
                pass
            for kind, raw in zip(kinds, replies):
                res = m.Result.unpack(raw)
                good = res.succeeded and (res.payload == data if kind == "in"
                                          else res.payload[:4] == struct.pack("<I", size))
                if good:
                    cell.ok += 1
                else:
                    cell.broken += 1
            cell.lost += len(msgs) - len(replies)
            cell.frames += len(msgs)
            if len(replies) < len(msgs):
                any(lk.confirm_raw(timeout) for _ in range(5))   # in step again before the next batch
            if hst.session is not None and cell.frames % 64 < batch:
                hst.keepalive()
    finally:
        lk.timeout, lk.resend = saved[0], saved[1]
        if saved[2] is not None:
            lk.fallback = saved[2]
    cell.seconds = time.perf_counter() - t0
    cell.kb_s = cell.ok * size / cell.seconds / 1000 if cell.seconds else 0.0
    return cell


VERIFY_MS = 2000   # the probe's try state lasts this long without a commit: a broken rate costs this much, then it is back


def _speed(hst, port: int, rate: int, step: int, verify_ms: int = VERIFY_MS, idle_ms: int = 3000) -> int:
    r = hst.call(m.CORE_FN, PORT_SPEED, struct.pack("<BIBHI", port, rate, step, verify_ms, idle_ms))
    return m.Reader(r.payload).u32()


def matrix(hst, *, rates: list[int | None] = (None,), patterns: list[str] = PATTERNS, inflight: list[int] = (1,),
           sizes: list[int] | None = None, frames: int = 300, seconds: float | None = None,
           timeout: float = 0.3, port: int | None = None):
    """For each rate (None: the speed in force, no port_speed), switch with port_speed try + commit (by hand, no verify
    of its own: this is the verify), run every (pattern, in-flight, size), then go back to the boot speed. Yields a
    RateResult per rate. Needs the lock. In-flight counts above the probe's max_inflight are skipped."""
    lk = hst.link
    limits = hst.limits or hst.confirm()
    sizes = list(sizes or [limits["max_frame"] - 16])
    base = getattr(lk, "base_baud", None) or getattr(lk, "baud", None)
    if port is None and any(r for r in rates):
        bridges = [index for index, kind, _ in core.transports(hst) if kind == 1]
        if not bridges:
            raise ValueError("the probe has no UART bridge to change the speed of")
        port = bridges[0]
    for rate in rates:
        result = RateResult(rate or getattr(lk, "baud", 0) or 0)
        switched = False
        try:
            if rate and rate != getattr(lk, "baud", None):
                result.actual = _speed(hst, port, rate, 0)
                lk.set_baud(rate)
                switched = True
                if not any(lk.confirm_raw(timeout) for _ in range(3)):
                    result.why = "no confirm at the new rate"
                    yield result
                    continue
                _speed(hst, port, rate, 1)
                result.switched = True
            for pattern in patterns:
                for n in inflight:
                    if n > limits["max_inflight"]:
                        continue
                    for size in sizes:
                        if size > limits["max_frame"] - 16:       # a request or an answer would not fit one frame
                            result.cells.append(Cell(result.rate, pattern, n, size,
                                                     error=f"over the probe's frame ({limits['max_frame']} - 16)"))
                            continue
                        result.cells.append(run(hst, pattern, n, size, frames=frames, seconds=seconds,
                                                timeout=timeout, rate=result.rate))
        except Exception as e:                      # a refusal (unsupported rate, no port_speed): say so, go on
            result.why = result.why or f"{type(e).__name__}: {e}"
        finally:
            if switched:
                try:
                    lk.timeout, saved = timeout, lk.timeout
                    _speed(hst, port, rate, 2)
                except Exception:
                    pass
                finally:
                    lk.timeout = saved
                lk.set_baud(base)
                deadline = time.monotonic() + VERIFY_MS / 1000 + 1.5   # the probe's try state may have to run out
                while not lk.confirm_raw(timeout):
                    if time.monotonic() > deadline:
                        raise ConnectionError(f"after {rate}: no answer at the boot speed {base}")
        yield result
