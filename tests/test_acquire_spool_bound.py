"""acquire._Spool's queue to its worker is bounded (QUEUE_MAX bytes): past it what arrives is dropped (a loss at its
positions) and the caller's thread never waits - it goes on reading the link, so answers are not held behind the stream."""
import queue
import threading
import time

from oep_client import acquire


class Got:
    def __init__(self):
        self.segments, self.data, self.gaps, self.start = {}, bytearray(), [], 0


def spool(slow_s):
    sp = acquire._Spool.__new__(acquire._Spool)
    sp.QUEUE_MAX, sp.received, sp.end, sp.segs = 10, 0, 0, {}
    sp._queue, sp._error = queue.Queue(), None
    sp._queued, sp._room = 0, threading.Condition()
    sp._host_gaps = set()
    sp.done = []
    sp._run = lambda pos, data: (time.sleep(slow_s), sp.done.append(bytes(data)))
    sp._work = lambda final, write_pos: None
    sp._close = lambda: None
    sp._worker = threading.Thread(target=sp._drain, daemon=True)
    sp._worker.start()
    return sp


def test_past_the_bound_what_arrives_is_dropped_and_the_caller_does_not_wait():
    sp = spool(0.2)
    got = Got()
    t0 = time.monotonic()
    for k in range(4):                                   # 8 bytes each: past 10 queued, dropped
        got.data += bytes([k]) * 8
        sp.take(got)
    assert time.monotonic() - t0 < 0.15 and sp._queued <= sp.QUEUE_MAX + 8
    sp._queue.put(("final", None))
    sp._worker.join(2)
    kept = [d[0] for d in sp.done]
    assert kept[0] == 0 and len(kept) < 4 and sp._queued == 0
    assert all(k * 8 in sp._host_gaps for k in range(4) if k not in kept)


def test_a_worker_that_failed_does_not_keep_the_caller_waiting():
    sp = spool(0.0)
    sp._error = RuntimeError("worker failed")
    sp._queued = 100                                      # full, and nobody will take it
    got = Got()
    got.data += b"x"
    t0 = time.monotonic()
    sp.take(got)
    assert time.monotonic() - t0 < 0.5


def test_putting_a_stream_together_keeps_the_callers_lease():
    """capture(hst, ...): the session is the caller's and still open while the parts are put together - keepalive at a
    third of the lease meanwhile; with no session of the caller's, nothing is sent."""
    class Slow:
        def result(self):
            time.sleep(1.2)
            return "parts"

    class Host:
        session, lease_ms, sent = 0x1234, 1500, 0

        def keepalive(self):
            self.sent += 1
    h = Host()
    assert acquire._put_together(Slow(), h) == "parts" and h.sent >= 2
    h2 = Host()
    h2.session = None
    assert acquire._put_together(Slow(), h2) == "parts" and h2.sent == 0


def test_the_spill_diagnostics_go_into_meta_with_the_harvest_gap_in_us():
    """capture-spill (the reference firmware's): its fields and the harvest's longest pass in microseconds."""
    import struct
    from oep_client import capture as oc

    class St:
        named = {oc.CAPTURE_SPILL: struct.pack("<QQQQIII", 1, 2, 3, 4, 5, 3_600_000, 360_000_000)}
    probe = {}
    acquire._note_spill(probe, oc, St())
    assert probe["spill"]["overruns_with_room"] == 5 and probe["spill"]["harvest_gap_max_us"] == 10000.0

    class Old:
        named = {}
    probe = {}
    acquire._note_spill(probe, oc, Old())
    assert "spill" not in probe


def test_a_loss_where_this_host_dropped_says_so():
    """A skipped position at a place take() dropped is "this host fell behind", elsewhere the probe's drop."""
    import types
    sp = acquire._Spool.__new__(acquire._Spool)
    sp.req = types.SimpleNamespace(on_loss="continue")
    sp.at, sp.pending, sp.parts, sp.cur, sp.lost, sp.stopped, sp.bytes = None, bytearray(), [], None, [], False, 0
    sp._work = lambda final, write_pos: None
    sp._close = lambda: None
    sp._host_gaps = {8}
    sp._run(0, b"x" * 8)
    sp._run(16, b"y" * 8)            # 8..16 dropped here
    sp._run(40, b"z" * 8)            # 24..40 not ours: the probe's
    assert [x["reason"].split(" (")[0] for x in sp.lost] == ["this host fell behind",
                                                              "the probe dropped a segment"]
