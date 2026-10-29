"""A probe's own log (oep-probe-arduino's OepProbeLog.h): the lines a sketch's code wrote on the probe, each marked with
the probe's clock, read from an interface of the sketch's own that serves them as a position stream (oep-if-common §1,
the read / marks / clear ops of oep.target.console without the stream number).

The probe's clock is the one of core §7.7 (uptime_ns) and of a capture's start_ns, so a line falls at sample
(probe_ns - the segment's start_ns) x its rate of a capture taken meanwhile (`sample_of`), within the segment's
start_uncertainty_ns. `rows` gives the lines as log rows for a recorder (WireSkein's run.json): the host's
time.monotonic() worked out from Host.clock readings, the probe's ns, its boot_id and the probe's name.

Nothing is lost silently: a lost mark (the ring overflowed: the oldest lines went) or a jump in the marks' serials (the
mark ring overflowed) shows in `Log.lost`, and a read past what is kept in `Log.gap`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction

from . import host as h
from .console import MARK_KIND, Mark, PositionStream

LINE = 0x40                       # the mark kind of a line's start (OepProbeLog.h kMarkKindLine; the interface's own)


@dataclass(frozen=True)
class Line:
    position: int                 # the stream position of its first byte
    probe_ns: int                 # the probe's clock when it was written (core §7.7)
    text: str


@dataclass
class Log:
    lines: list[Line] = field(default_factory=list)
    lost: list[str] = field(default_factory=list)    # what says lines are missing (lost marks, serial jumps)
    gap: bool = False                                # the oldest kept was not the first written


class ProbeLog(PositionStream):
    """The log of the sketch's interface named `name` (for the example of oep-probe-arduino extras/LibraryTest:
    io.github.example.library-test)."""

    def __init__(self, hst: h.Host, name: str, revision: int = 1, fn: int | None = None):
        self.NAME, self.REVISION = name, revision
        super().__init__(hst, name, fn=fn)

    def log(self, from_serial: int = 0) -> Log:
        """Every line kept: the text from the oldest byte, cut at each line mark."""
        out = Log()
        data, start = b"", None
        position = None
        while True:
            chunk = self.read(self.FROM_OLDEST) if position is None else self.read_from(position)
            if start is None:
                start, out.gap = chunk.start, chunk.gap
            data += chunk.data
            position = chunk.start + len(chunk.data)
            if not chunk.more:
                break
        marks = self.marks(from_serial)
        last = None
        for mk in marks:
            if last is not None and (mk.serial - last - 1) & 0xFFFFFFFF:
                out.lost.append(f"{(mk.serial - last - 1) & 0xFFFFFFFF} marks missing before serial {mk.serial}")
            last = mk.serial
            if mk.kind == MARK_KIND["lost"]:
                out.lost.append(f"lost (detail {mk.detail}) at position {mk.position}")
        lines = [mk for mk in marks if mk.kind == LINE and mk.position >= start]
        for mk, nxt in zip(lines, lines[1:] + [None]):
            end = nxt.position if nxt is not None else start + len(data)
            text = data[mk.position - start:end - start].decode("utf-8", "replace").rstrip("\n")
            out.lines.append(Line(mk.position, mk.time_ns, text))
        return out


def sample_of(line: Line, start_ns: int, rate: Fraction) -> float:
    """The sample of a capture segment the line falls at (its start_ns and actual rate)."""
    return float((line.probe_ns - start_ns) * rate / 1_000_000_000)


def to_host_ns(probe_ns: int, readings: list[h.ClockReading]) -> int:
    """The host's time.monotonic_ns() for the probe's clock: the nearest reading's offset, or between two readings of
    one boot the line through them."""
    rs = sorted(readings, key=lambda r: r.uptime_ns)
    if len(rs) >= 2 and rs[0].boot_id == rs[-1].boot_id and rs[-1].uptime_ns != rs[0].uptime_ns:
        a, b = rs[0], rs[-1]
        return a.host_ns + round((probe_ns - a.uptime_ns) * (b.host_ns - a.host_ns) / (b.uptime_ns - a.uptime_ns))
    r = min(rs, key=lambda r: abs(r.uptime_ns - probe_ns))
    return r.host_ns + probe_ns - r.uptime_ns


def rows(log: Log, readings: list[h.ClockReading], probe: str) -> list[dict]:
    """The lines as log rows: t the host's time.monotonic() (seconds), src "marker" for a heading line (#...) and "dut"
    for the rest, text, probe_ns, boot_id and probe (how the capture's source names it)."""
    boot = readings[0].boot_id
    return [{"t": to_host_ns(line.probe_ns, readings) / 1e9, "src": "marker" if line.text.startswith("#") else "dut",
             "text": line.text, "probe_ns": line.probe_ns, "boot_id": boot, "probe": probe} for line in log.lines]
