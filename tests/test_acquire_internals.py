"""acquire's own steps without a probe: the capture group's trigger caps checked before bind, a trigger that never
comes named in the timeout, a stream's parts cut at a loss (stopped there by default), and the spool fed poll by poll
giving what putting the stream together at the end gives."""
from types import SimpleNamespace

import pytest

from oep_client import capture as oc
from oep_client.acquire import Request, _check_caps, _Spool, _stream_parts, _waiting, oc_state

LOST = "the probe dropped a segment (no room to send)"


class FakeCap:
    """Stands for LogicCapture in _stream_parts (format 0)."""


def fake_stream(segments, gaps):
    """A Received of format 0 (w 8, one channel at bit 0): segments as (position, samples, start_ns), each sample a
    byte; `gaps` (data index, bytes skipped) as the probe's drops."""
    got = oc.Received()
    got.start = segments[0][0]
    for pos, n, ns in segments:
        got.segments[pos] = oc.SegmentInfo(pos, n, ns, 100)
        got.data += bytes([(pos + k) & 1 for k in range(n)])
    got.gaps = gaps
    return got


def stream_cfg():
    return SimpleNamespace(rate=1_000_000, width=8, positions=[0], event_d={})


def test_a_condition_the_group_does_not_declare_is_refused_before_bind():
    class Group:
        def trigger_caps(self):
            return oc.TriggerCaps(1, 1, 1, False, sum(1 << oc.COND[k] for k in ("level", "edge")), 0, None)

    an = type("T", (), {"fn": 3})()
    source = oc.TriggerSource([oc.Stage([[oc.Cond.above(an, 0, 100)]])])
    req = Request([("A", "GPIO10")], 1_000_000, 100, trigger=("SQ", "high", 1.0), analog=[("SQ", "GPIO16", None)])
    probe = {}
    with pytest.raises(RuntimeError, match=r"cannot start its tracks on SQ:high:1.0V together: its capture group "
                                           r"triggers on level, edge \(trigger_caps"):
        _check_caps(Group(), source, req, "x", probe)
    assert probe["trigger_caps"]["kinds"] == ["level", "edge"]


def test_a_trigger_that_never_comes_is_named():
    class Waiting:
        def status(self):
            return (oc_state("waiting"), 0, 0, 0)

    def never():
        raise TimeoutError("capture did not finish")

    req = Request([("A", "GPIO10")], 1_000_000, 100, trigger=("A", "rise"), timeout=2)
    with pytest.raises(TimeoutError, match=r"no trigger \(A:rise\) within 2 s"):
        _waiting(never, Waiting(), req)
    with pytest.raises(TimeoutError, match="did not finish"):          # no trigger asked: the plain timeout
        _waiting(never, Waiting(), Request([("A", "GPIO10")], 1_000_000, 100, timeout=2))


def test_stream_parts_split_at_a_loss_and_stop_there_by_default():
    # segments of 100 samples at 1 MHz (100 us each); the one at position 200 was dropped (a gap of 100 bytes)
    got = fake_stream([(0, 100, 0), (100, 100, 100_000), (300, 100, 300_000)], [(200, 100)])
    req = Request([("A", "GPIO1")], 1_000_000, 0, mode="stream", seconds=1)
    parts, lost = _stream_parts(FakeCap(), stream_cfg(), got, ["A"], [1], req)
    assert len(parts) == 1 and parts[0].channels[0].n == 200
    assert lost == [{"after_part": 0, "reason": LOST, "stopped": True}]
    req = Request([("A", "GPIO1")], 1_000_000, 0, mode="stream", seconds=1, on_loss="continue")
    parts, lost = _stream_parts(FakeCap(), stream_cfg(), got, ["A"], [1], req)
    assert [p.tick0 for p in parts] == [0, 300] and [p.channels[0].n for p in parts] == [200, 100]
    assert lost == [{"after_part": 0, "reason": LOST, "ticks": 100}]
    assert parts[1].meta["start_ns"] == 300_000


def test_a_stream_that_lost_its_start_keeps_nothing_by_default():
    got = fake_stream([(100, 100, 100_000)], [])
    req = Request([("A", "GPIO1")], 1_000_000, 0, mode="stream", seconds=1)
    with pytest.raises(RuntimeError, match="dropped the stream's first 100 bytes"):
        _stream_parts(FakeCap(), stream_cfg(), got, ["A"], [1], req)
    req = Request([("A", "GPIO1")], 1_000_000, 0, mode="stream", seconds=1, on_loss="continue")
    parts, lost = _stream_parts(FakeCap(), stream_cfg(), got, ["A"], [1], req)
    assert parts[0].tick0 == 0 and lost[0]["after_part"] == -1


def test_a_spooled_stream_is_the_same_as_one_put_together_at_the_end(tmp_path):
    """_Spool, fed poll by poll (the bytes taken out of Received each time), gives the parts and losses that
    _stream_parts gives from everything at once - and keeps the bits on disk."""
    segs = [(0, 100, 0), (100, 100, 100_000), (300, 100, 300_000), (400, 60, 400_000)]
    req = Request([("A", "GPIO1")], 1_000_000, 0, mode="stream", seconds=1, on_loss="continue",
                  spool_dir=str(tmp_path))
    want_parts, want_lost = _stream_parts(FakeCap(), stream_cfg(), fake_stream(segs, [(200, 100)]), ["A"], [1], req)
    full = fake_stream(segs, [(200, 100)])
    spool = _Spool(FakeCap(), stream_cfg(), ["A"], [1], req)
    got = oc.Received()
    for lo, hi in ((0, 37), (37, 150), (150, 260), (260, 360)):   # the bytes, a poll at a time
        if got.start is None:
            got.start = 0
        got.data += full.data[lo:hi]
        got.gaps += [(i - lo + len(got.data) - (hi - lo), n) for i, n in full.gaps if lo <= i < hi]
        got.segments.update(full.segments)
        spool.take(got)
        assert len(got.data) == 0                                  # taken out: memory holds a poll's worth
    spool.take(got, final=True)
    assert [(p.tick0, p.channels[0].n) for p in spool.parts] == [(p.tick0, p.channels[0].n) for p in want_parts]
    assert spool.done_lost == want_lost
    for a, b in zip(spool.parts, want_parts):
        assert a.channels[0].bits[:] == b.channels[0].bits        # the same bits, read back from disk
