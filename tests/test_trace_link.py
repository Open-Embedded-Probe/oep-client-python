"""oep.probe.link (oep-spec a09fa45 interfaces/oep-if-link.ja.md): the rules tests/test_port_speed.py and the link
checkers leave to the trace - at most one oep.probe.link, source / sink / port_state change nothing, sink's count
bound, the host's wait for a line test, the session and lock kept while trying, and the host's switches (the
20 ms after try, back to the boot speed on another session's force open or a restart, not on the same id's)."""

import struct
import time

import pytest

from oep_client import core, endpoint, link, virtual_bench, host as h, message as m, registry as reg

from test_link_serial import Scripted
from test_port_speed import COMMIT, PS, TRY, FAST, Port, committed, in_process, opened, ps

LINK = reg.PROBE_LINK.op


class Clock:
    t = 0

    def __call__(self):
        return self.t


@pytest.fixture(autouse=True)
def _ceiling(monkeypatch):
    """As in tests/test_port_speed.py: the procedure at a rate above the default ceiling without the 1 s verify."""
    monkeypatch.setattr(link, "DEFAULT_CEILING", 10 ** 9)


@pytest.mark.parametrize("name", sorted(virtual_bench.PROFILES))
def test_a_probe_lists_oep_probe_link_at_most_once_and_with_port_speed(name):
    """link preamble: optional; a probe with port_speed lists it; at most one oep.probe.link."""
    ep = endpoint.Endpoint(virtual_bench.PROFILES[name](), Clock())
    hst = h.Host(lambda b: ep.handle(b, 0))
    found = core.find_all(hst, "oep.probe.link")
    assert len(found) <= 1
    if ep.port_speed_base is not None:
        assert found == [ep.link_fn] and core.offers(hst, ep.link_fn, PS)


def planned(p, sid):
    """gpio (fn 4) role 1 on channel 5, through plan_apply."""
    assign = m.tlv(reg.PROBE_PLAN.tlv["plan_apply"]["role_assignment"], struct.pack("<HBH", 4, 1, 5), critical=True)
    assert p.send(p.ep.fns["oep.probe.plan"], reg.PROBE_PLAN.op["plan_apply"], assign, sid).succeeded


def snapshot(ep):
    return (ep.holder, ep.lease_ms, set(ep.plan), dict(ep.gpio_modes), dict(ep.parked), dict(ep.config), ep.generation,
            dict(ep.conns), dict(ep.streams), ep.speed_state, ep.port_baud(0))


def test_source_sink_and_port_state_change_nothing():
    """link preamble, §2: the line tests and port_state change no state of the probe (only port_speed changes the
    port's speed): the session, its plan and pins, the settings, connections, streams and the speed stay."""
    p = Port()
    sid = opened(p)
    planned(p, sid)
    before = snapshot(p.ep)
    for payload, op in ((struct.pack("<I", 100), LINK["source"]), (struct.pack("<H", 3) + b"abc", LINK["sink"]),
                        (b"", LINK["port_state"])):
        for session in (0, sid):
            assert p.send(p.ep.link_fn, op, payload, session).succeeded
            assert snapshot(p.ep) == before


def test_sink_takes_up_to_max_frame_less_12_and_a_count_past_the_bytes_is_malformed():
    """link §2 sink: count(u16) then count bytes of any value; the request fits max_frame, so count is at most
    max_frame - 12; a count larger than the bytes that follow is malformed, an empty success otherwise."""
    p = Port()
    top = p.ep.probe.max_frame - 12
    r = p.send(p.ep.link_fn, LINK["sink"], struct.pack("<H", top) + bytes(range(256)) * (top // 256) + bytes(top % 256))
    assert r.succeeded and r.payload == b""
    r = p.send(p.ep.link_fn, LINK["sink"], struct.pack("<H", 5) + b"abcd")
    assert (r.resolution, r.detail) == (m.REJECTED, m.MALFORMED)
    r = p.send(p.ep.link_fn, LINK["sink"], struct.pack("<H", 0))
    assert r.succeeded and r.payload == b""


def test_the_host_waits_for_a_source_answer_as_for_any_request_never_repeating_it_like_confirm():
    """link §2: the host waits for the answer as for other requests (core §4.4): one resend with the same corr after
    the wait (core §5.2), then the failure - not confirm's repeated tries."""
    s = Scripted()
    lk = link.SerialLink.on_stream(s, "cobs", 0.05)
    writes = []
    s.on_write = writes.append
    request = m.Request(9, 4, LINK["source"], struct.pack("<I", 16)).pack()
    with pytest.raises((TimeoutError, ConnectionError)):
        lk.send(request)
    assert 1 <= len(writes) <= 2 and len(set(writes)) == 1


def test_the_session_and_the_lock_stay_while_trying_and_committed():
    """link §3: between try and commit (and after), the session's resources and the lock are unchanged."""
    p = Port()
    sid = opened(p)
    planned(p, sid)
    kept = lambda: (p.ep.holder, set(p.ep.plan), dict(p.ep.gpio_modes), dict(p.ep.conns), dict(p.ep.streams))
    before = kept()
    assert p.send(p.ep.link_fn, PS, ps(500000, TRY), sid).succeeded and p.ep.speed_state == "try"
    assert kept() == before
    assert p.send(p.ep.link_fn, PS, ps(500000, COMMIT), sid).succeeded and p.ep.speed_state == "committed"
    assert kept() == before and p.ep.holder == sid


def test_the_host_waits_port_speed_switch_wait_ms_after_switching(monkeypatch):
    """link §3 host 1: after the try's answer the host switches and waits port_speed_switch_wait_ms (20 ms) or more
    before the next request at the new rate."""
    assert link.SWITCH_SETTLE_S == reg.TIMING["port_speed_switch_wait_ms"] / 1000 == 0.02
    s = Scripted()
    s.baudrate = 115200
    lk = link.SerialLink.on_stream(s, "cobs", 0.05)
    slept = []
    monkeypatch.setattr(link.time, "sleep", slept.append)
    assert lk.set_baud(500000) == 500000 and s.baudrate == 500000
    assert slept and max(slept) >= 0.02
    ep, hst, lk = in_process()
    n = len(ep.requests)
    order = []
    real = lk.set_baud
    monkeypatch.setattr(lk, "set_baud", lambda rate, fallback=None: (order.append(("switch", rate)), real(rate, fallback))[1])
    link.raise_speed(hst, [500000], **FAST)
    asked = [(q.op, q.payload[4] if q.fn == ep.link_fn and q.op == PS else None) for q in ep.requests[n:]
             if q.op in (m.OP_CONFIRM, PS)]
    assert asked[:3] == [(PS, TRY), (m.OP_CONFIRM, None), (PS, COMMIT)] and order[0] == ("switch", 500000)


def test_the_host_goes_back_on_another_ids_force_open_not_on_its_own():
    """link §3 host 2: a successful open with force for another session_id takes the port back to the boot speed
    (the probe goes back after that answer); the same session_id's open, force or not, keeps the session and the rate."""
    ep, hst, lk = in_process()
    link.raise_speed(hst, [500000], **FAST)
    assert lk.baud == 500000
    r = hst.request(0, m.OP_OPEN, struct.pack("<IB", 10000, 1), session=hst.session)   # the same id, force
    assert r.succeeded and lk.baud == 500000 and ep.port_baud(0) == 500000 and ep.speed_state == "committed"
    hst.open(10000, force=True)                                                 # another id: the lock changes hands
    assert lk.baud == 115200 and ep.port_baud(0) == 115200 and ep.speed_state == "base"
    hst.keepalive()


def test_the_host_goes_back_after_a_restart():
    """link §3 host 2: a successful answer of the op that restarts the probe takes the port back to the boot speed."""
    ep, hst, lk = in_process()
    link.raise_speed(hst, [500000], **FAST)
    before = ep.boot_id
    assert hst.restart_probe(wait_s=2) != before
    assert lk.baud == 115200 and ep.port_baud(0) == 115200
    hst.open(3000)
    hst.keepalive()


def test_the_interface_row_lists_oep_probe_link_at_revision_1():
    """link preamble (the interface row): the probe lists oep.probe.link by that name at revision 1."""
    ep = endpoint.Endpoint(virtual_bench.esp32_v003(), Clock())
    hst = h.Host(lambda b: ep.handle(b, 0))
    entries = [e for e in core.list_entries(hst) if e.name == "oep.probe.link"]
    assert [(e.fn, e.revision) for e in entries] == [(ep.link_fn, 1)] and reg.PROBE_LINK.revision == 1


@pytest.mark.parametrize("op, locked", [("source", False), ("sink", False), ("port_speed", True), ("port_state", False)])
def test_the_lock_column_of_the_op_table(op, locked):
    """link §1 (the op table, lock column): source, sink and port_state need no lock - with session_id 0 they are
    not refused session_required; port_speed needs it - session_required with 0, the port's speed unchanged."""
    p = Port()
    payload = {"source": struct.pack("<I", 4), "sink": struct.pack("<H", 1) + b"x",
               "port_speed": ps(500000, TRY), "port_state": b""}[op]
    r = p.send(p.ep.link_fn, LINK[op], payload, 0)
    assert (LINK[op] not in reg.PROBE_LINK.lock_free) == locked
    if locked:
        assert r.detail == m.SESSION_REQUIRED and p.ep.speed_state == "base" and p.ep.port_baud(0) == 115200
    else:
        assert r.succeeded


def test_the_probe_goes_back_by_itself_without_telling():
    """link §3 (the probe's own return, "not announced"): going back after verify_ms or after port_speed_idle_ms sends
    nothing on the port - no frame at either rate - and port_state shows the boot speed afterwards."""
    p = Port()
    sid = opened(p)
    assert p.send(p.ep.link_fn, PS, ps(750000, TRY, 1000), sid).succeeded
    p.port.output()
    p.tick(1000)
    assert p.ep.port_baud(0) == 115200 and p.port.output() == b""
    committed(p, sid)
    p.port.output()
    p.tick(3000)
    assert p.ep.port_baud(0) == 115200 and p.ep.speed_state == "base" and p.port.output() == b""
    r = p.send(p.ep.link_fn, LINK["port_state"], b"", 0)
    assert r.succeeded and struct.unpack_from("<IB", r.payload) == (115200, 0)


def test_the_host_reads_the_phase_with_a_new_corr_not_from_an_old_answer():
    """link §3 host 2: a past success answer resent is no evidence of the speed now - after a try that failed (its
    confirm's answers broken at the new rate), the host reverts, then reads port_state with a new corr (above every
    corr sent before) and only then tries the next candidate."""
    ep, hst, lk = in_process()
    ep.broken_rates[1000000] = endpoint.BrokenRate(to_probe=False)
    n = len(ep.requests)
    report = link.raise_speed(hst, [1000000, 500000], **FAST)
    sent = ep.requests[n:]
    at = next(i for i, q in enumerate(sent) if q.fn == ep.link_fn and q.op == LINK["port_state"])
    revert = max(i for i, q in enumerate(sent[:at]) if q.fn == ep.link_fn and q.op == PS)
    assert sent[revert].payload[4] == 2                                          # the failed try's revert ...
    assert sent[at].corr > max(q.corr for q in sent[:at])                       # ... then port_state, a new corr
    assert any(q.op == PS and q.payload[4] == TRY for q in sent[at + 1:])         # the next try after it
    assert report.chosen == 500000 and lk.baud == 500000
