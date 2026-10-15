"""Review 7, class host-recovery (oep-spec a09fa45): the host's waits, resends and recovery.

core §4.4: "この下限はすべての要求に当てはまる" - the link's own requests (keepalive, port_speed's revert, a measurement's
source / sink, a resync's or a recovery's confirm) wait core §4.4's floor too; confirms repeated with new corrs may go
before the earlier one's floor, but the last one gets its whole floor before the host decides none came (transports §3).
core §4.4: "host は状態を変える要求の前に fn 0 の describe で max_op_ms を取得する" - after an open that shows a reboot too.
"""

import struct
import time

import pytest

from oep_client import endpoint, host as h, link, message as m, registry as reg, virtual_bench

from test_host_rules_2026_10_02 import Serial
from test_link_host import CONFIRM_V1, Stream, make_link, result
from test_virtual_bench_rules_2026_10_02 import Clock

MAX_OP = reg.CORE.tlv["describe"]["max_op_ms"]


def bound(lk, max_op_ms=None):
    """A host bound to `lk` whose fn 0 describe declared `max_op_ms` (None: not read yet)."""
    hst = h.Host(lk.send)
    if max_op_ms is not None:
        hst._describes = {0: [(MAX_OP, struct.pack("<I", max_op_ms))]}
    lk._bind_host(hst)
    return hst


def waits_of(lk, answer=lambda corr: result(corr)):
    """`_recv_for` replaced: each call records the wait the link would use, and answers at once."""
    seen = []

    def recv_for(corr):
        seen.append((lk._wait(), lk.timeout))
        return answer(corr)
    lk._recv_for = recv_for
    return seen


def test_the_links_own_keepalive_waits_the_floor():
    lk = link.SerialLink.on_stream(Serial(), "cobs", 0.2)
    seen = waits_of(lk)
    lk._own += 1                                                       # keep_alive / _keep / a revert send this way
    lk._send(m.Request(1, 0, m.OP_KEEPALIVE).pack())
    assert seen[0][0] >= 1.0 + 1.0 + lk.transfer_s()                   # quick bound (max_op_ms unknown) + 1000 ms
    lk._own -= 1
    assert lk._wait() == pytest.approx(1.0 + lk.transfer_s())         # outside a request: the old floor, unchanged


def test_a_port_speed_revert_of_the_link_waits_max_op_ms():
    lk = link.SerialLink.on_stream(Serial(), "cobs", 0.2)
    bound(lk, 5000)
    seen = waits_of(lk)
    lk._own += 1
    lk._send(m.Request(1, 9, link.OP_PORT_SPEED, struct.pack("<IBH", 0, 2, 0), 7).pack())
    assert seen[0][0] >= 5.0 + 1.0                                     # max_op_ms + host_wait_add_ms


def test_a_measurements_source_and_sink_wait_max_op_ms():
    """oep-if-link §2: "host はその応答を、ほかの要求と同じように待つ（core §4.4）" - linktest and the port_speed
    verify pipeline source / sink outside Host.request (expect_ms 0)."""
    lk = link.SerialLink.on_stream(Serial(), "cobs", 0.05)
    hst = bound(lk, 4000)
    assert hst.expect_ms == 0
    seen = waits_of(lk)
    msgs = [m.Request(c, 9, link.OP_SOURCE, struct.pack("<I", 8)).pack() for c in (1, 2)]
    lk._exchange_once(msgs, 2, 4096, [])
    assert len(seen) == 2 and all(w >= 4.0 + 1.0 for w, _ in seen)


def test_the_last_of_repeated_confirms_waits_its_floor():
    lk = link.SerialLink.on_stream(Serial(), "cobs", 0.05)

    def no_answer(corr):
        raise TimeoutError("none")
    seen = waits_of(lk, no_answer)
    assert not lk.confirm_tries(3, 0.01)
    firsts, last = [t for _, t in seen[:2]], seen[2][1]
    assert all(t <= 0.011 for t in firsts)                             # repeats may go before the floor (§4.4)
    assert last >= 1.0 + 1.0 + lk.transfer_s() - 0.01                 # the last: quick bound + 1000 ms + transfer
    seen.clear()
    assert not lk._confirm_within(0.0, 0.01)                           # wait_boot_speed / back_to_base
    assert len(seen) == 1 and seen[0][1] >= 2.0 - 0.01


def test_a_recovery_confirm_waits_its_floor_on_a_short_link_timeout():
    lk = link.SerialLink.on_stream(Serial(), "cobs", 0.2)
    lk.failed_transport = "a resend went unanswered"
    seen = waits_of(lk, lambda corr: result(corr, CONFIRM_V1))
    lk.recover_transport()
    assert seen[0][1] >= 2.0 and not lk.failed_transport                # quick bound + 1000 ms (+ transfer)


def test_a_resync_confirm_from_a_pump_waits_its_floor():
    lk = make_link(Stream())
    lk.timeout = 0.2
    lk.frames.discard_until_quiet = lambda quiet, most: True
    seen = []

    def recv():
        seen.append(lk._wait())
        raise TimeoutError("none")
    lk._recv = recv
    lk._own += 1                                                       # pump's reads are the link's own
    with pytest.raises(ConnectionError):
        lk.resync(tries=1)
    assert seen and seen[0] >= 2.0


def test_an_open_that_shows_a_reboot_reads_max_op_ms_again():
    ep = endpoint.Endpoint(virtual_bench.p4_bench(), Clock())
    hst = h.Host(lambda b: ep.handle(b, 0))
    hst.open(3000)
    hst.end()
    ep.reboot()
    n = len(ep.requests)
    hst.open(3000)                                                     # max_op_ms known from the old boot: no describe first
    assert hst.max_op_ms() is not None                                 # read again before a state-changing request
    after = [(r.fn, r.op) for r in ep.requests[n:]]
    assert after.index((0, m.OP_OPEN)) < after.index((0, m.OP_DESCRIBE))


def test_a_serial_request_given_up_confirms_the_boot_id_before_the_next():
    """core §5.1: "再送を諦めたら ... ほかの経路では [経路] §5 に従って立て直す。boot_id を確認し、状態を読んでから次の
    状態変更を判断する" - with resend off on a serial port too, not only after an unanswered resend."""
    lk = link.SerialLink.on_stream(Serial(), "cobs", 0.05)
    lk.resend = False

    def no_answer(corr):
        raise TimeoutError("none")
    lk._recv_for = no_answer
    with pytest.raises(TimeoutError):
        lk._send(m.Request(1, 9, 0x10, b"", 7).pack())
    assert lk.failed_transport                                         # the next request recovers with a confirm first


def test_a_step_down_whose_revert_answer_is_lost_reads_port_state():
    """oep-if-link §3: "try/commit/revertの応答を失った場合は、起動時/試した速さでconfirmと新しいcorrのport_stateを使って
    復旧する" - the in-use step down's revert too, not only raise_speed's own."""
    from test_port_speed import PS, raised_in_use
    from oep_client import cobs
    ep, hst, lk, report = raised_in_use(lease=60000)
    dropped = []

    def answer(n, result):
        q = ep.requests[-1]
        if not dropped and q.fn == ep.link_fn and q.op == PS and q.payload[4] == link.SPEED_STEP["revert"]:
            dropped.append(q.corr)
            return None
        return cobs.frame(result)
    lk.stream.port.answer_filter = answer
    n = len(ep.requests)
    lk._step_down("a test")
    assert dropped and lk.baud == 115200
    sent = [(q.fn, q.op) for q in ep.requests[n:]]
    revert = sent.index((ep.link_fn, PS))
    assert (ep.link_fn, link.OP_PORT_STATE) in sent[revert + 1:]


def test_linktests_lost_revert_answer_reads_port_state():
    """oep-if-link §3, the same rule for the measurement's own revert (linktest.matrix)."""
    from test_port_speed import PS, in_process
    from oep_client import cobs, linktest
    ep, hst, lk = in_process(lease=60000)
    dropped = []

    def answer(n, result):
        q = ep.requests[-1]
        if q.fn == ep.link_fn and q.op == PS and q.payload[4] == link.SPEED_STEP["revert"]:   # and its resend
            dropped.append(q.corr)
            return None
        return cobs.frame(result)
    lk.stream.port.answer_filter = answer
    n = len(ep.requests)
    res, = linktest.matrix(hst, rates=[921600], patterns=["in"], sizes=[16], frames=4, timeout=0.3)
    assert res.switched and dropped and lk.baud == 115200
    sent = [(q.fn, q.op) for q in ep.requests[n:]]
    last_revert = max(i for i, s in enumerate(sent) if s == (ep.link_fn, PS))
    assert (ep.link_fn, link.OP_PORT_STATE) in sent[last_revert + 1:]
