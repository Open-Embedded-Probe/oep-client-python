"""acquire._Spool's queue to its worker is bounded (QUEUE_MAX bytes): past it the caller's thread waits for the worker,
so a stream faster than this host fills no memory - the transport's bounded buffers then hold it back and the probe
drops whole frames (core §10.4), which the positions show as a loss."""
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
    sp.done = []
    sp._run = lambda pos, data: (time.sleep(slow_s), sp.done.append(bytes(data)))
    sp._work = lambda final, write_pos: None
    sp._close = lambda: None
    sp._worker = threading.Thread(target=sp._drain, daemon=True)
    sp._worker.start()
    return sp


def test_the_caller_waits_while_the_queue_is_past_its_bound():
    sp = spool(0.2)
    got = Got()
    t0 = time.monotonic()
    for k in range(4):                                   # 8 bytes each: the third waits for the worker
        got.data += bytes([k]) * 8
        sp.take(got)
    waited = time.monotonic() - t0
    assert waited >= 0.15 and sp._queued <= sp.QUEUE_MAX + 8
    sp._queue.put(("final", None))
    sp._worker.join(2)
    assert [d[0] for d in sp.done] == [0, 1, 2, 3] and sp._queued == 0


def test_a_worker_that_failed_does_not_keep_the_caller_waiting():
    sp = spool(0.0)
    sp._error = RuntimeError("worker failed")
    sp._queued = 100                                      # full, and nobody will take it
    got = Got()
    got.data += b"x"
    t0 = time.monotonic()
    sp.take(got)
    assert time.monotonic() - t0 < 0.5
