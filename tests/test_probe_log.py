"""probe_log: a probe's own log read as lines by its marks (oep-if-common §1), placed in a capture by the probe's clock,
and given as log rows with the host's time."""
from fractions import Fraction

from oep_client import host as h, probe_log as pl
from oep_client.console import MARK_KIND, Chunk, Mark


class FakeLog(pl.ProbeLog):
    """The stream ops answered from memory (no probe): `data` from `start`, `marks` as the probe keeps them."""

    def __init__(self, data: bytes, marks: list[Mark], start: int = 0, gap: bool = False):
        self.data, self._marks, self.start, self.gap = data, marks, start, gap

    def read(self, start=None, arg=0, maximum=1000):
        return Chunk(self.start, False, self.gap, self.data)

    def read_from(self, position, maximum=1000):
        return Chunk(position, False, False, self.data[position - self.start:])

    def marks(self, from_serial=0):
        return self._marks


def line_mark(serial, position, ns):
    return Mark(serial, position, pl.LINE, ns, 0)


def test_lines_are_cut_at_their_marks_with_their_times():
    data = b"## write\nresult=2\n##\n"
    marks = [line_mark(0, 0, 1000), line_mark(1, 9, 1500), line_mark(2, 18, 2000)]
    log = FakeLog(data, marks).log()
    assert [(x.text, x.probe_ns) for x in log.lines] == [("## write", 1000), ("result=2", 1500), ("##", 2000)]
    assert not log.lost and not log.gap


def test_lost_lines_show():
    data = b"b\n"
    marks = [line_mark(5, 10, 1), Mark(6, 12, MARK_KIND["lost"], 2, 1), line_mark(9, 12, 3)]
    log = FakeLog(data, marks, start=10, gap=True).log()
    assert log.gap and any("lost" in x for x in log.lost) and any("2 marks missing" in x for x in log.lost)


def test_a_line_falls_at_its_sample_and_gets_the_hosts_time():
    line = pl.Line(0, 1_000_000 + 250_000, "## step")
    assert pl.sample_of(line, 1_000_000, Fraction(1_000_000)) == 250.0
    readings = [h.ClockReading(5_000_000_000, 5_000_000_200, 1_000_000, 7, 200),
                h.ClockReading(6_000_000_000, 6_000_000_200, 1_001_000_000, 7, 200)]
    assert pl.to_host_ns(1_250_000, readings) == 5_000_250_100
    log = pl.Log([line, pl.Line(9, 1_500_000, "result=2")])
    rows = pl.rows(log, readings, "usb:30eda0ea068b")
    assert [r["src"] for r in rows] == ["marker", "dut"]
    assert rows[0] == {"t": 5.0002501, "src": "marker", "text": "## step", "probe_ns": 1_250_000, "boot_id": 7,
                       "probe": "usb:30eda0ea068b"}
