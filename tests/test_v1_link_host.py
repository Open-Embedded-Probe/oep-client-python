"""The link's correlation matching and §1 resync, and the host's call / fn cache, on a scripted byte stream (no serial
port)."""

import struct

import pytest

from oep_client.v1 import catalog, core, frames, host as h, link, message as m


class Stream:
    """A pyserial stand-in: reads come from a queue the test fills (or `respond` fills per written frame, or `noise`
    keeps filling); writes are recorded."""

    def __init__(self, respond=None):
        self.rx, self.writes, self.timeout, self.resets = bytearray(), [], 0.05, 0
        self.respond = respond
        self.noise = None

    @property
    def in_waiting(self):
        return len(self.rx)

    def read(self, n=1):
        if self.noise and not self.rx:
            self.rx += self.noise
        out = bytes(self.rx[:n])
        del self.rx[:n]
        return out

    def write(self, data):
        self.writes.append(bytes(data))
        if self.respond:
            at = 0
            while at < len(data):
                n = struct.unpack_from("<H", data, at)[0]
                for reply in self.respond(bytes(data[at + 2:at + 2 + n])):
                    self.rx += frame(reply)
                at += 2 + n

    def reset_input_buffer(self):
        self.rx.clear()
        self.resets += 1


def frame(msg: bytes) -> bytes:
    return struct.pack("<H", len(msg)) + msg


def make_link(stream):
    return link.SerialLink.on_stream(stream, "length", 0.3)


def result(corr, payload=b"", res=m.COMPLETED, detail=m.SUCCESS):
    return m.Result(corr, res, detail, payload).pack()


CONFIRM_V1 = struct.pack("<4sBBHIB", b"OEP!", 1, 0, 1024, 65536, 4)


def answering(by_op=None):
    """A responder: confirm answers v1, other ops by by_op[op] (payload), default an empty success."""
    def respond(msg):
        req = m.Request.unpack(msg)
        if req.fn == 0 and req.op == m.OP_CONFIRM:
            return [result(req.corr, CONFIRM_V1)]
        return [result(req.corr, (by_op or {}).get(req.op, b""))]
    return respond


def sent(stream):
    """Every request written, in order, as (role, corr, op)."""
    out = []
    for w in stream.writes:
        at = 0
        while at < len(w):
            n = struct.unpack_from("<H", w, at)[0]
            msg = w[at + 2:at + 2 + n]
            out.append((msg[0], msg[1] | msg[2] << 8, msg[5]))
            at += 2 + n
    return out


# ---- routing ------------------------------------------------------------------------------------------------

def test_pushes_are_routed_by_role_and_never_taken_for_a_reply():
    s = Stream()
    lk = make_link(s)
    # A data push for fn 7 carries 07 00 where a result carries its correlation id: matched by bytes, it would pass
    # for the reply to request 7.
    push = bytes([0x06]) + struct.pack("<HHI", 7, 0, 100) + b"data"
    event = bytes([0x05, 7, 0, 0, 0, 1])
    s.rx += frame(push) + frame(event) + frame(bytes([0x03, 7, 0])) + frame(result(7, b"reply"))
    assert lk.send(m.Request(7, 0, 0x01, b"").pack()).endswith(b"reply")
    assert list(lk.pushes) == [push] and list(lk.events) == [event] and lk.dropped == 1 and lk.stale == 0
    s.rx += frame(push)
    assert lk.pump(0.05) == 1 and len(lk.pushes) == 2


# ---- §1 resync ----------------------------------------------------------------------------------------------

def test_a_reply_for_another_request_resyncs_and_a_read_is_sent_once_more():
    s = Stream(answering({0x13: b"\x00" + bytes(4)}))
    lk = make_link(s)
    s.rx += frame(result(6, b"old"))                              # 6 answered late
    reply = lk.send(m.Request(7, 0, m.OP_LOCK_STATE, b"").pack())
    assert m.Result.unpack(reply).corr == 7
    ops = sent(s)
    assert [op for _, _, op in ops] == [m.OP_LOCK_STATE, m.OP_CONFIRM, m.OP_LOCK_STATE]   # resync confirm between
    assert lk.stale == 1 and lk.resyncs == 1 and lk.retries == 1


def test_a_state_changing_request_is_never_sent_again_after_a_resync():
    s = Stream(answering())
    lk = make_link(s)
    s.rx += frame(result(99))
    with pytest.raises(link.CorrMismatch):
        lk.send(m.Request(7, 1, 0x01, b"x", session=0x1234).pack())
    assert [op for _, _, op in sent(s)] == [0x01, m.OP_CONFIRM]
    assert lk.resyncs == 1 and lk.retries == 0


def test_an_impossible_length_resyncs():
    s = Stream(answering())
    lk = make_link(s)
    lk.frames.max_frame = 1024
    s.rx += struct.pack("<H", 5000) + b"garbage"
    lk.send(m.Request(3, 0, m.OP_LOCK_STATE, b"").pack())         # lock-free: sent again after the resync
    assert lk.resyncs == 1 and [op for _, _, op in sent(s)] == [m.OP_LOCK_STATE, m.OP_CONFIRM, m.OP_LOCK_STATE]


def test_a_frame_that_stops_half_way_resyncs(monkeypatch):
    monkeypatch.setattr(frames, "STALL_S", 0.02)
    s = Stream(answering())
    lk = make_link(s)
    s.rx += struct.pack("<H", 10) + b"\x02\x05"                   # 2 of 10 bytes, then nothing
    with pytest.raises(frames.FramingLost):
        lk.send(m.Request(4, 1, 0x01, b"", session=1).pack())
    assert lk.resyncs == 1 and [op for _, _, op in sent(s)] == [0x01, m.OP_CONFIRM]


def test_pushes_that_never_stop_are_stopped_blind_with_unsubscribe_and_end(monkeypatch):
    monkeypatch.setattr(link.SerialLink, "NOISY_S", 0.05)
    s = Stream()
    lk = make_link(s)
    hst = h.Host(lk.send)
    hst.session, hst.revision, hst.subscriptions = 0xABCD, 1, {5}
    lk.corr_source, lk.blind = hst.next_corr, hst.blind_stop
    s.noise = frame(bytes([0x06]) + struct.pack("<HHI", 5, 0, 0) + b"x" * 32)

    def respond(msg):
        req = m.Request.unpack(msg)
        if req.op == m.OP_END:
            s.noise = None                                        # the lock ends, the subscription with it
        if req.op == m.OP_CONFIRM:
            return [result(req.corr, CONFIRM_V1)]
        return [result(req.corr)]

    s.respond = respond
    s.rx += frame(result(50))                                     # a stray result starts the resync
    with pytest.raises(link.CorrMismatch):
        lk.send(m.Request(hst.next_corr(), 5, 0x01, b"", session=0xABCD).pack())
    ops = sent(s)
    assert [op for _, _, op in ops][1:] == [m.OP_UNSUBSCRIBE, m.OP_END, m.OP_CONFIRM]
    assert all(role == 0x81 for role, _, op in ops if op in (m.OP_UNSUBSCRIBE, m.OP_END))
    assert hst.subscriptions == set()


def test_each_frame_goes_out_in_one_write():
    s = Stream(answering())
    lk = make_link(s)
    reqs = [m.Request(c, 1, 0x02, b"x" * 40).pack() for c in (10, 11, 12)]
    lk.exchange(reqs, 3, 4096)
    at = 0
    for w in s.writes:                                            # every write holds whole frames only
        while at < len(w):
            at += 2 + struct.unpack_from("<H", w, at)[0]
        assert at == len(w)
        at = 0


def test_exchange_resyncs_after_a_stray_reply_and_raises():
    s = Stream()
    lk = make_link(s)
    reqs = [m.Request(c, 1, 0x02, b"x").pack() for c in (10, 11, 12)]
    s.rx += frame(result(10)) + frame(result(11)) + frame(result(12))
    assert [m.Result.unpack(r).corr for r in lk.exchange(reqs, 2, 4096)] == [10, 11, 12]
    s.respond = answering()
    s.rx += frame(result(19))                                     # not 20
    with pytest.raises(link.CorrMismatch):
        lk.exchange([m.Request(c, 1, 0x02, b"x").pack() for c in (20, 21)], 2, 4096)
    assert lk.resyncs == 1


def test_attach_host_binds_limits_and_max_frame():
    s = Stream(answering())
    lk = make_link(s)
    hst = h.Host(lk.send)
    lk.attach_host(hst)
    assert hst.revision == 1 and hst.limits["window"] == 65536 and lk.frames.max_frame == 1024
    assert lk.corr_source == hst.next_corr and hst.link is lk


# ---- host ---------------------------------------------------------------------------------------------------

def test_call_raises_failed_and_request_does_not():
    hst = h.Host(lambda raw: result(m.Request.unpack(raw).corr, res=m.COMPLETED, detail=m.FAILED))
    assert not hst.request(3, 1).succeeded
    with pytest.raises(h.Failed):
        hst.call(3, 1)


@pytest.mark.parametrize("res, detail", [(0x03, 0), (m.COMPLETED, 0x07)])
def test_an_unknown_resolution_or_outcome_is_a_failure(res, detail):
    hst = h.Host(lambda raw: result(m.Request.unpack(raw).corr, res=res, detail=detail))
    r = hst.request(3, 1)
    assert not r.succeeded and not r.ran and "unknown" in r.describe()
    with pytest.raises(h.Failed, match="unknown"):
        hst.call(3, 1)


def test_find_is_cached_until_the_probe_reboots_and_an_empty_page_ends_the_search():
    lists, boot = [], [0x11]

    def send(raw):
        req = m.Request.unpack(raw)
        if req.op == m.OP_CONFIRM:
            return result(req.corr, CONFIRM_V1)
        if req.op == m.OP_LIST:
            lists.append(req.payload)
            name = catalog.unpack_list_request(req.payload)[0]
            page = [catalog.ListEntry(4, 1, 1, 0, name)] if name == "oep.wire.swd" else []
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
    epoch = hst.epoch
    boot[0] = 0x22
    hst.open()                                                    # rebooted: interfaces may be numbered anew
    assert hst.epoch == epoch + 1                                 # ... and every connection and the plan are gone
    core.find(hst, "oep.wire.swd")
    assert len(lists) == 3
