"""The link's correlation matching and the host's call / fn cache, on a scripted byte stream (no serial port)."""

import collections
import struct

import pytest

from oep_client.v1 import catalog, core, frames, host as h, link, message as m


class Stream:
    """A pyserial stand-in: reads come from a queue the test fills; writes are recorded."""

    def __init__(self):
        self.rx, self.writes, self.timeout, self.resets = bytearray(), [], 0.05, 0

    @property
    def in_waiting(self):
        return len(self.rx)

    def read(self, n=1):
        out = bytes(self.rx[:n])
        del self.rx[:n]
        return out

    def write(self, data):
        self.writes.append(bytes(data))

    def reset_input_buffer(self):
        self.rx.clear()
        self.resets += 1


def frame(msg: bytes) -> bytes:
    return struct.pack("<H", len(msg)) + msg


def make_link(stream):
    lk = link.SerialLink.__new__(link.SerialLink)
    lk.stream, lk.framing, lk.timeout = stream, "length", 0.3
    lk.retries = lk.corrupt = lk.stale = lk.dropped = 0
    lk.pushes = collections.deque()
    lk.frames = frames.LengthFrames(stream)
    return lk


def result(corr, payload=b"", res=m.COMPLETED, detail=m.SUCCESS):
    return m.Result(corr, res, detail, payload).pack()


def test_a_late_reply_to_an_earlier_request_is_read_past():
    s = Stream()
    lk = make_link(s)
    s.rx += frame(result(6, b"old")) + frame(result(7, b"new"))   # 6 answered late, after its re-send timed out
    assert lk.send(m.Request(7, 0, 0x01, b"").pack()).endswith(b"new")
    assert lk.stale == 1


def test_pushes_are_routed_by_role_and_never_taken_for_a_reply():
    s = Stream()
    lk = make_link(s)
    # A data push for fn 7 carries 07 00 where a result carries its correlation id: matched by bytes, it would pass
    # for the reply to request 7.
    push = bytes([0x06]) + struct.pack("<HHI", 7, 0, 100) + b"data"
    s.rx += frame(push) + frame(bytes([0x05, 7, 0, 0, 0, 1])) + frame(result(7, b"reply"))
    assert lk.send(m.Request(7, 0, 0x01, b"").pack()).endswith(b"reply")
    assert list(lk.pushes) == [push] and lk.dropped == 1 and lk.stale == 0
    s.rx += frame(push)
    assert lk.pump(0.05) == 1 and len(lk.pushes) == 2


def test_exchange_matches_by_correlation_and_clears_after_an_error():
    s = Stream()
    lk = make_link(s)
    reqs = [m.Request(c, 1, 0x02, b"x").pack() for c in (10, 11, 12)]
    s.rx += frame(result(9)) + frame(result(10)) + frame(result(11)) + frame(result(12))
    assert [m.Result.unpack(r).corr for r in lk.exchange(reqs, 2, 4096)] == [10, 11, 12]
    s.rx += frame(result(20))                                     # 21 never comes
    with pytest.raises(TimeoutError):
        lk.exchange([m.Request(c, 1, 0x02, b"x").pack() for c in (20, 21)], 2, 4096)
    assert s.resets == 1                                          # leftovers cannot be read by the next request


def test_call_raises_failed_and_request_does_not():
    hst = h.Host(lambda raw: result(m.Request.unpack(raw).corr, res=m.COMPLETED, detail=m.FAILED))
    assert not hst.request(3, 1).succeeded
    with pytest.raises(h.Failed):
        hst.call(3, 1)


def test_find_is_cached_until_the_probe_reboots_and_an_empty_page_ends_the_search():
    lists, boot = [], [0x11]

    def send(raw):
        req = m.Request.unpack(raw)
        if req.op == m.OP_LIST:
            lists.append(req.payload)
            name = catalog.unpack_list_request(req.payload)[0]
            page = [catalog.ListEntry(4, 1, 0, 0, name)] if name == "oep.wire.swd" else []
            return result(req.corr, catalog.pack_list_result(3 if not page else 1, page))   # claims 3, sends none
        if req.op == m.OP_OPEN:
            return result(req.corr, struct.pack("<IIB", 1000, boot[0], 0))
        raise AssertionError(req.op)

    hst = h.Host(send)
    hst.open()
    assert core.find(hst, "oep.wire.swd") == 4 and core.find(hst, "oep.wire.swd") == 4
    assert len(lists) == 1
    with pytest.raises(LookupError):
        core.find(hst, "oep.fixture.gpio")                        # total 3 but an empty page: no endless loop
    boot[0] = 0x22
    hst.open()                                                    # rebooted: interfaces may be numbered anew
    core.find(hst, "oep.wire.swd")
    assert len(lists) == 3
