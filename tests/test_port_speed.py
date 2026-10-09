"""port_speed (oep-if-link §3, an op of the optional oep.probe.link; the handshake only): the virtual bench's state machine (the
request baud / step / verify_ms on the port it came on, try / commit / revert, port_speed_tolerance_pct, going back
after verify_ms, after port_speed_idle_ms with no good frame or answer, or at the session's end - never on broken
candidates - another port and off), and the client's opt-in raise_speed against it (host guide §17: the procedure, the
first wait and the resend at the raised rate before the boot speed) - in process, where the virtual bench also sees the host's
own rate (`VirtualSerialStream`), and on a pty through open_host(port_speed=...)."""

import json
import struct
import subprocess
import sys
import time

import pytest

from oep_client import cobs, core, endpoint, virtual_bench, virtual_bench_serial, host as h, link, message as m

TRY, COMMIT, REVERT = 0, 1, 2
PS = endpoint.OP_PORT_SPEED


@pytest.fixture(autouse=True)
def _ceiling(request, monkeypatch):
    """Most tests here are about the procedure at any rate the virtual bench makes (1500000, 921600 ... included): they run
    without the default ceiling, so a rate above 500000 is not given the 1 s verify. The tests named `ceiling` keep
    the real one (host guide §17, §17.3.3)."""
    if "ceiling" not in request.node.name:
        monkeypatch.setattr(link, "DEFAULT_CEILING", 10 ** 9)


class Clock:
    def __init__(self):
        self.t = 1000

    def __call__(self):
        return self.t


def ps(baud, step, verify_ms=500):
    """port_speed's request (oep-if-link §1): baud(u32) step(u8) verify_ms(u16) - no port (the one it comes on), no
    idle_ms (port_speed_idle_ms is the registry's)."""
    return struct.pack("<IBH", baud, step, verify_ms)


CAUSE_6 = bytes([0x01, 1, 0, 6])   # unavailable's payload: the cause TLV (core §4.3), wrong_state


def framed(req):
    return cobs.frame(req.pack())


class Port:
    """An endpoint's serial port `index` (0: the profile's UART bridge) driven by whole requests, its answers decoded
    (a broken one: None)."""

    def __init__(self, profile=virtual_bench.esp32_v003, peer=None, index=0):
        if peer is None:
            self.clock = Clock()
            self.ep = endpoint.Endpoint(profile(), self.clock)
            self.corrs = [0]
        else:                                            # another port of `peer`'s probe: one session, one corr count
            self.clock, self.ep, self.corrs = peer.clock, peer.ep, peer.corrs
        self.port = virtual_bench_serial.VirtualSerialPort(self.ep, index)

    def send(self, fn, op, payload=b"", session=None, raw=False):
        self.corrs[0] += 1
        self.port.feed(framed(m.Request(self.corrs[0], fn, op, payload, session)))
        out = self.port.output()
        if not out:
            return None
        try:
            return m.Result.unpack(cobs.unframe(out[1:-1]))
        except cobs.CorruptFrame:
            return None

    def noise(self):
        self.port.feed(b"\x00\x31\x32\x33\x00")      # a candidate whose CRC does not match

    def good_frame(self):
        """A frame that decodes and passes its CRC but is no request (a result: the probe drops it, answers nothing)."""
        self.port.feed(cobs.frame(m.Result(1, m.COMPLETED, m.SUCCESS, b"").pack()))

    def tick(self, ms):
        self.clock.t += ms
        self.port.tick()


def opened(p, sid=5, lease=60000):
    assert p.send(0, m.OP_OPEN, struct.pack("<IB", lease, 0), sid).succeeded   # the id in the header (core §4.1)
    return sid


def committed(p, sid, baud=500000):
    assert p.send(p.ep.link_fn, PS, ps(baud, TRY), sid).succeeded
    assert p.send(p.ep.link_fn, PS, ps(baud, COMMIT), sid).succeeded and p.ep.speed_state == "committed"


# ---- the virtual bench's state machine (oep-if-link §3, the handshake) --------------------------------------------------------

def link_ops(p):
    """The ops tag of the probe's oep.link describe (core §7.4)."""
    tlvs = m.split_tlvs(b"".join(p.ep._declarations(p.ep.link_fn)))
    return virtual_bench.catalog.unpack_ops(next(v for t, v in tlvs if t == virtual_bench.catalog.OPS))


def test_the_ops_tag_offers_it_only_when_on_and_off_is_unknown_operation():
    p = Port()
    assert link_ops(p) == {1, 2, PS}                                            # source, sink, port_speed
    p.ep.port_speed_base = None
    assert link_ops(p) == {1, 2}
    sid = opened(p)
    r = p.send(p.ep.link_fn, PS, ps(1500000, TRY), sid)
    assert r.resolution == m.REJECTED and r.detail == m.UNKNOWN_OPERATION


def test_the_request_is_baud_step_verify_ms_and_a_longer_fixed_part_is_a_tlv_tail():
    """oep-if-link §1: no port and no idle_ms. The old 12-byte form (port u8 first) is read as baud step verify_ms and a
    TLV tail: refused (a tail that does not decode, or a baud no UART makes) and nothing switches; a short fixed part:
    malformed (core §2.3)."""
    p = Port()
    sid = opened(p)
    old = struct.pack("<BIBHI", 0, 500000, TRY, 500, 0)
    assert p.send(p.ep.link_fn, PS, old, sid).resolution == m.REJECTED and p.ep.speed_state == "base"
    assert p.send(p.ep.link_fn, PS, ps(500000, TRY)[:6], sid).detail == m.MALFORMED
    r = p.send(p.ep.link_fn, PS, ps(500000, TRY), sid)
    assert r.succeeded and r.payload == struct.pack("<I", 500000) and p.ep.speed_state == "try"


def test_try_then_commit_answered_at_the_old_speed():
    p = Port()
    sid = opened(p)
    assert p.send(p.ep.link_fn, PS, ps(1500000, TRY)).detail == m.SESSION_REQUIRED       # the lock is needed
    r = p.send(p.ep.link_fn, PS, ps(1500000, TRY, 800), sid)
    assert r.succeeded and m.Reader(r.payload).u32() == 1500000
    assert p.ep.speed_state == "try" and p.ep.port_baud(0) == 1500000
    r = p.send(p.ep.link_fn, PS, ps(1000000, COMMIT), sid)                               # another baud: cause 6
    assert r.detail == m.UNAVAILABLE and r.payload[:4] == CAUSE_6
    r = p.send(p.ep.link_fn, PS, ps(1500000, TRY), sid)                                  # a try while trying: cause 6
    assert r.detail == m.UNAVAILABLE and r.payload[:4] == CAUSE_6 and p.ep.speed_state == "try"
    r = p.send(p.ep.link_fn, PS, ps(1500000, COMMIT, 0), sid)                            # verify_ms: any value here
    assert r.succeeded and m.Reader(r.payload).u32() == 1500000 and p.ep.speed_state == "committed"
    r = p.send(p.ep.link_fn, PS, ps(1500000, COMMIT), sid)                               # committed already: cause 6
    assert r.detail == m.UNAVAILABLE and r.payload[:4] == CAUSE_6 and p.ep.speed_state == "committed"
    r = p.send(p.ep.link_fn, PS, ps(921600, TRY), sid)                                   # a try while committed: cause 6
    assert r.detail == m.UNAVAILABLE and r.payload[:4] == CAUSE_6 and p.ep.port_baud(0) == 1500000
    p.tick(endpoint.SPEED_IDLE_MS - 1)                    # port_speed_idle_ms from the last answer sent
    assert p.ep.port_baud(0) == 1500000
    p.tick(1)
    assert p.ep.port_baud(0) == 115200 and p.ep.speed_state == "base"


def test_a_step_that_does_not_fit_the_boot_state_is_cause_6():
    p = Port()
    sid = opened(p)
    for step in (COMMIT, REVERT):
        r = p.send(p.ep.link_fn, PS, ps(500000, step), sid)
        assert r.detail == m.UNAVAILABLE and r.payload[:4] == CAUSE_6
    assert p.ep.speed_state == "base" and p.ep.speed_log == []
    r = p.send(p.ep.link_fn, PS, ps(500000, 3), sid)                     # a step the definition leaves unused (§2.5)
    assert (r.detail, r.payload) == (m.UNSUPPORTED, b"\x00")             # the fixed part's tag
    assert p.send(p.ep.link_fn, PS, ps(500000, 0xFF), sid).detail == m.UNSUPPORTED
    assert p.send(p.ep.link_fn, PS, ps(500000, TRY, 0), sid).detail == m.MALFORMED   # verify_ms 0 in a try
    assert p.ep.speed_state == "base"


def test_the_nearest_rate_the_uart_makes_must_be_within_the_tolerance():
    """oep-if-link §3: the nearest rate the UART makes more than port_speed_tolerance_pct (2 %) from the request:
    unsupported; else the answer's baud is the rate applied. The virtual bench makes 300..5000000 exactly."""
    assert endpoint.SPEED_TOLERANCE_PCT == virtual_bench.reg.LIMITS["port_speed_tolerance_pct"] == 2
    p = Port()
    sid = opened(p)
    for baud in (9_000_000, 5_103_000, 290):                             # 5000000 / 300: over 2 % off
        r = p.send(p.ep.link_fn, PS, ps(baud, TRY), sid)
        assert (r.detail, r.payload[:1]) == (m.UNSUPPORTED, b"\x00") and p.ep.speed_state == "base"
    r = p.send(p.ep.link_fn, PS, ps(5_100_000, TRY), sid)                # 5000000: within 2 %
    assert r.succeeded and m.Reader(r.payload).u32() == 5_000_000 and p.ep.port_baud(0) == 5_000_000
    assert p.send(p.ep.link_fn, PS, ps(5_000_000, COMMIT), sid).detail == m.UNAVAILABLE   # not the baud tried
    assert p.send(p.ep.link_fn, PS, ps(5_100_000, COMMIT), sid).succeeded                # the same baud as the try
    assert p.send(p.ep.link_fn, PS, ps(0, REVERT), sid).succeeded and p.ep.port_baud(0) == 115200
    r = p.send(p.ep.link_fn, PS, ps(295, TRY), sid)                       # 300: within 2 % at the low end too
    assert r.succeeded and m.Reader(r.payload).u32() == 300


def test_try_times_out_and_a_late_commit_is_wrong_state():
    p = Port()
    sid = opened(p)
    p.send(p.ep.link_fn, PS, ps(750000, TRY, 1000), sid)
    p.tick(999)
    assert p.ep.port_baud(0) == 750000
    p.tick(1)
    assert p.ep.port_baud(0) == 115200
    r = p.send(p.ep.link_fn, PS, ps(750000, COMMIT), sid)
    assert r.detail == m.UNAVAILABLE and r.payload[:4] == CAUSE_6


def test_broken_candidates_never_take_it_back_while_trying_or_committed():
    """oep-if-link §3: the probe goes back by itself only when a try outlives verify_ms, when committed and no good frame
    came for port_speed_idle_ms, or at the session's end - broken candidates are none of these."""
    p = Port()
    sid = opened(p)
    p.send(p.ep.link_fn, PS, ps(230400, TRY, 5000), sid)
    p.noise()                                                                    # the switch-over's leftovers
    p.send(0, m.OP_LOCK_STATE)                                                  # a good frame at the new speed
    for _ in range(5):
        p.noise()
    assert p.ep.speed_state == "try" and p.ep.speed_log == [(0, 230400)]
    assert p.send(p.ep.link_fn, PS, ps(230400, COMMIT), sid).succeeded
    for _ in range(10):
        p.noise()                                                               # many in a row
        p.tick(10)
    assert p.ep.speed_state == "committed" and p.ep.port_baud(0) == 230400 and p.ep.speed_log == [(0, 230400)]


def test_committed_goes_back_after_the_idle_time_counted_from_a_good_frame_or_an_answer():
    """oep-if-link §3 return condition 2: port_speed_idle_ms (3000) with no good frame on the port, counted again from
    each good frame received and each answer sent; a broken candidate does not count."""
    assert endpoint.SPEED_IDLE_MS == link.IDLE_MS == 3000
    p = Port()
    sid = opened(p)
    committed(p, sid)                                                            # its answer: the count starts
    p.tick(2000)
    p.good_frame()                                                               # a good frame, no answer to it
    p.tick(2000)
    p.noise()                                                                    # broken: no restart
    p.tick(999)
    assert p.ep.port_baud(0) == 500000
    p.tick(1)
    assert p.ep.port_baud(0) == 115200 and p.ep.speed_state == "base"
    committed(p, sid)
    p.tick(2500)
    p.ep.speed_answered(0)                                                       # an answer sent there (as handle does)
    p.tick(2999)
    assert p.ep.port_baud(0) == 500000
    p.tick(1)
    assert p.ep.port_baud(0) == 115200
    q = Port()                                                                   # the answers of requests: both
    sid = opened(q)
    committed(q, sid)
    for _ in range(4):
        q.tick(2900)
        assert q.send(0, m.OP_KEEPALIVE, b"", sid).succeeded and q.ep.port_baud(0) == 500000


def test_step_2_and_the_sessions_end_answer_at_the_speed_then_revert():
    p = Port()
    sid = opened(p)
    committed(p, sid)
    p.ep.broken_rates[115200] = endpoint.BrokenRate()    # an answer sent at the boot speed would come out broken
    r = p.send(p.ep.link_fn, PS, ps(0, REVERT, 0), sid)
    assert r is not None and r.succeeded and m.Reader(r.payload).u32() == 115200   # it went out at 500000
    assert p.ep.port_baud(0) == 115200
    del p.ep.broken_rates[115200]
    committed(p, sid)
    p.ep.broken_rates[115200] = endpoint.BrokenRate()
    r = p.send(0, m.OP_END, b"", sid)
    assert r is not None and r.succeeded and p.ep.port_baud(0) == 115200
    del p.ep.broken_rates[115200]
    sid = opened(p, 6, 1000)                                                     # a lapse reverts too
    p.send(p.ep.link_fn, PS, ps(500000, TRY), sid)                               # trying (verify_ms 500) ...
    p.send(p.ep.link_fn, PS, ps(500000, COMMIT), sid)
    p.tick(1100)
    assert p.ep.holder is None and p.ep.port_baud(0) == 115200
    sid = opened(p, 7)
    committed(p, sid)
    p.ep.broken_rates[115200] = endpoint.BrokenRate()
    r = p.send(0, m.OP_OPEN, struct.pack("<IB", 60000, 1), 8)                  # force: another holder
    assert r is not None and r.succeeded and p.ep.holder == 8 and p.ep.port_baud(0) == 115200


def test_only_the_uart_bridge_the_request_came_in_on_and_one_port_at_a_time():
    """oep-if-link §3: the port is the one the request came on, a UART bridge only (else unavailable cause 6); a try
    on another port while one port is raised: cause 6."""
    p = Port()
    two = p.ep.add_transport(virtual_bench.TRANSPORT["uart_bridge"])                     # a second UART bridge
    q = Port(peer=p, index=two)
    sid = opened(p)
    committed(p, sid)
    r = q.send(p.ep.link_fn, PS, ps(500000, TRY), sid)                           # port 0 is raised: cause 6
    assert r.detail == m.UNAVAILABLE and r.payload[:4] == CAUSE_6 and q.ep.port_baud(two) == 115200
    r = q.send(p.ep.link_fn, PS, ps(0, REVERT, 0), sid)                          # nothing to go back from there
    assert r.detail == m.UNAVAILABLE and r.payload[:4] == CAUSE_6 and p.ep.port_baud(0) == 500000
    assert p.send(p.ep.link_fn, PS, ps(0, REVERT, 0), sid).succeeded and p.ep.port_baud(0) == 115200
    r = q.send(p.ep.link_fn, PS, ps(230400, TRY), sid)                           # none raised: this one may
    assert r.succeeded and p.ep.speed_port == two and p.ep.port_baud(two) == 230400 and p.ep.port_baud(0) == 115200
    u = Port(virtual_bench.p4_x035)                                                      # USB-Serial/JTAG: no UART bridge
    u.ep.port_speed_base = 115200
    sid = opened(u)
    r = u.send(u.ep.link_fn, PS, ps(500000, TRY), sid)
    assert r.detail == m.UNAVAILABLE and r.payload[:4] == CAUSE_6 and u.ep.speed_state == "base"


# ---- the client: raise_speed (host guide §17) -----------------------------------------------------------------------------

def in_process(profile=virtual_bench.esp32_v003, lease=10000):
    start = time.monotonic()
    ep = endpoint.Endpoint(profile(), lambda: int((time.monotonic() - start) * 1000))
    stream = virtual_bench_serial.VirtualSerialStream(ep, 0)
    lk = link.SerialLink.on_stream(stream, "cobs", 0.5)
    lk.transport = "serial"
    lk.wait_add_s = 0.0          # the in-process virtual bench answers at once: the floor's 1000 ms would only slow the losses
    hst = h.Host(lk.send)
    lk.attach_host(hst)
    core.take(hst, lease)
    return ep, hst, lk


UNIT = "fafe00000003"   # the virtual bench esp32-v003's unit_id
FAST = dict(verify_ms=900)   # the probe's try state ends soon: a failed candidate costs under a second


def ops(ep, since=0):
    """(op, port_speed's step or None) of fn 0's requests and oep.link's port_speed ones, in order."""
    return [(q.op, q.payload[4] if q.fn == ep.link_fn else None) for q in ep.requests[since:]
            if q.fn == 0 or (q.fn == ep.link_fn and q.op == PS)]


def test_the_minimal_form_tries_confirms_and_commits_without_a_measurement():
    """Host guide §17.2: one candidate, switch, 20 ms, a confirm, commit - no flows, no baseline."""
    ep, hst, lk = in_process()
    n = len(ep.requests)
    report = link.raise_speed(hst, [1500000], **FAST)
    assert report.supported and report.chosen == report.rate == 1500000 and report.base == 115200
    assert not report.verified and report.baseline == {} and report.baseline_flows == []
    t, = report.trials
    assert t.committed and t.actual == t.switched == 1500000 and t.flows == [] and t.n_cap == 0
    assert report.in_kb_s is None and t.in_kb_s is None
    assert ops(ep, n) == [(m.OP_LIST, None), (m.OP_DESCRIBE, None),             # oep.link found, its ops read
                          (PS, TRY), (m.OP_CONFIRM, None), (PS, COMMIT)]
    assert lk.speed is report and lk.baud == 1500000 and ep.speed_state == "committed"
    assert link.IDLE_MS == 3000 and lk.keepalive_s == link.KEEPALIVE_S == 1.0 and lk.inflight_cap == 0
    hst.keepalive()                                                              # the session goes on at the new rate
    assert "committed" in report.to_text() and "in force: 1500000 (raised)" in report.to_text()


def test_the_default_candidate_is_500000():
    ep, hst, lk = in_process()
    report = link.raise_speed(hst, **FAST)
    assert link.DEFAULT_CANDIDATES == (500000,) and report.chosen == 500000 and lk.baud == 500000


def test_minimal_form_falls_back_when_the_confirm_does_not_come_and_goes_on():
    ep, hst, lk = in_process()
    ep.broken_rates[1000000] = endpoint.BrokenRate(to_probe=False)   # probe -> host only: the probe sees nothing wrong
    t0 = time.monotonic()
    report = link.raise_speed(hst, [9_000_000, 1000000, 500000], **FAST)
    b, a, c = report.trials
    assert not a.committed and a.why == "no confirm at the new rate" and a.actual == 1000000
    assert b.why.startswith("unsupported") and b.actual is None
    assert c.committed and report.chosen == 500000 and lk.baud == 500000
    assert ep.speed_state == "committed" and ep.port_baud(0) == 500000
    assert (0, 1000000) in ep.speed_log and time.monotonic() - t0 < 3.0
    assert "no confirm" in report.to_text()


def test_the_full_form_measures_every_flow_and_fails_a_rate_whose_frames_break():
    """Host guide §17.3.2: a baseline per flow at the boot speed, then 16 frames per flow at each candidate; a flow fails
    on broken + lost >= 3 over max(2 x baseline, 5 %), and one failed flow fails the candidate."""
    ep, hst, lk = in_process()
    ep.broken_rates[230400] = endpoint.BrokenRate(min_size=40, to_probe=False)   # the confirm passes, full answers break
    report = link.raise_speed(hst, [230400, 500000], verify=True, verify_ms=5000)   # the try state outlasts the measurement
    assert report.verified and set(report.baseline) == {"in", "out", "duplex"} and all(v == 0 for v in report.baseline.values())
    assert [f.name for f in report.baseline_flows] == ["in@4", "out@4", "duplex@4"]   # measured: 60 frames each
    assert all(f.frames == 60 for f in report.baseline_flows) and report.baseline_frames == 0
    a, b = report.trials
    assert not a.committed and a.why.startswith("in@") and "over 5%" in a.why
    assert [f.name for f in a.flows] == ["in@4", "in@1"] and all(not f.passed for f in a.flows)
    assert a.flows[0].broken + a.flows[0].lost >= 3 and a.flows[0].frames >= 16
    assert b.committed and [f.name for f in b.flows] == ["in@4", "out@4", "duplex@4"] and all(f.passed for f in b.flows)
    assert all(f.frames >= 16 and f.broken == f.lost == 0 and f.kb_s > 0 for f in b.flows)
    assert report.in_kb_s == b.in_kb_s > 0 and report.out_kb_s > 0 and report.duplex_kb_s > 0 and b.n_cap == 0
    assert report.chosen == 500000 and lk.baud == 500000 and ep.port_baud(0) == 500000 and lk.inflight_cap == 0
    text = report.to_text()
    assert "baseline at 115200 (measured, 60 frames per flow)" in text and "failed" in text and "committed" in text


def test_the_full_form_verifies_only_the_flows_asked_and_caps_n_at_1_when_a_flow_needs_it():
    ep, hst, lk = in_process()
    ep.broken_rates[921600] = endpoint.BrokenRate(min_size=40, duplex=True, to_probe=False)   # answers break with both ways busy
    report = link.raise_speed(hst, [921600], flows=[("in", 2), ("duplex", 0)], **FAST)
    assert report.verified
    t, = report.trials
    assert t.committed and [f.name for f in t.flows] == ["in@2", "duplex@4", "duplex@1"]
    assert [f.passed for f in t.flows] == [True, False, True] and t.flows[1].broken + t.flows[1].lost >= 3
    assert t.n_cap == 1 and lk.inflight_cap == 1 and lk.inflight_for(hst.limits) == 1
    assert "committed (in flight 1)" in report.to_text()
    assert [f.name for f in report.baseline_flows] == ["in@2", "duplex@4"]
    assert link.resolve_flows(["out", ("in", 9)], 4) == [("out", 4), ("in", 4)]
    with pytest.raises(ValueError):
        link.resolve_flows([("sideways", 1)], 4)


def test_the_baseline_comes_from_the_sessions_frames_when_there_are_enough():
    ep, hst, lk = in_process()
    for _ in range(70):
        hst.request(0, m.OP_LOCK_STATE)
    assert lk.base_counts["good"] >= 70 and lk.base_counts["broken"] == lk.base_counts["lost"] == 0
    report = link.raise_speed(hst, [500000], flows=[("duplex", 1)], **FAST)
    assert report.baseline_frames >= 70 and report.baseline == {"duplex": 0.0} and report.baseline_flows == []
    assert report.chosen == 500000 and "frames of this session" in report.to_text()
    report = link.raise_speed(hst, [500000], flows=[("in", 1)], baseline=0.02, **FAST)   # given: nothing measured
    assert report.baseline == {"in": 0.02} and report.baseline_flows == [] and report.baseline_frames == 0
    hst.end()
    core.take(hst, 10000)                                                        # a new session: its own count
    assert lk.base_counts["good"] <= 2


def test_a_boot_speed_that_loses_too_much_is_not_raised():
    ep, hst, lk = in_process()
    ep.broken_rates[115200] = endpoint.BrokenRate(min_size=40, to_probe=False, every=4)   # 25 % of full answers
    report = link.raise_speed(hst, [500000], flows=[("in", 2)], **FAST)
    assert report.supported and not report.trials and "not raised" in report.why and "in@1" in report.why
    assert [f.name for f in report.baseline_flows] == ["in@2", "in@1"]       # over 10 %: once more at n = 1
    assert all(f.ratio > 0.1 for f in report.baseline_flows)
    assert lk.baud == 115200 and ep.speed_state == "base" and ep.speed_log == []
    del ep.broken_rates[115200]
    hst.keepalive()


def test_requests_that_break_towards_the_probe_lose_the_flow_until_the_try_runs_out():
    """The probe does not go back on broken candidates (oep-if-link §3): its try outlives verify_ms."""
    ep, hst, lk = in_process()
    ep.broken_rates[230400] = endpoint.BrokenRate(min_size=40, to_host=False)   # full requests break: no answer
    t0 = time.monotonic()
    report = link.raise_speed(hst, [230400, 500000], flows=["out"], **FAST)
    a, b = report.trials
    assert not a.committed and a.flows[0].lost > 0 and a.flows[0].gone and a.why == \
        "out@4: no answer at 230400 any more (the probe went back)"
    assert b.committed and (0, 230400) in ep.speed_log and lk.baud == 500000 and time.monotonic() - t0 < 4.0


def test_the_sessions_end_takes_the_link_back_to_the_boot_speed():
    ep, hst, lk = in_process()
    link.raise_speed(hst, [750000], **FAST)
    assert lk.baud == 750000
    hst.end()
    assert ep.port_baud(0) == 115200 and lk.baud == 115200 and lk.speed.rate == 115200 and lk.speed.chosen is None
    hst.confirm()


def test_a_revert_seen_as_a_timeout_goes_back_to_the_boot_speed(monkeypatch):
    ep, hst, lk = in_process()
    link.raise_speed(hst, [750000], **FAST)
    monkeypatch.setattr(endpoint, "SPEED_IDLE_MS", 200)    # the probe's idle time, short here (3000 by the registry)
    time.sleep(0.35)                                       # the probe goes back by itself: no good frame for it
    ep.tick()
    assert ep.port_baud(0) == 115200 and lk.baud == 750000
    hst.keepalive()                                        # times out at 750000 twice, then once more at the boot speed
    assert lk.baud == 115200 and lk.speed_lost == 1 and lk.speed.lost and lk.speed.rate == 115200


def test_raise_speed_sends_no_idle_ms_the_probe_keeps_port_speed_idle_ms():
    """oep-if-link §1: commit is baud step verify_ms like every step; the idle time is the registry's
    port_speed_idle_ms, and the link's keepalive comes well inside half of it (host guide §17)."""
    ep, hst, lk = in_process()
    n = len(ep.requests)
    link.raise_speed(hst, [750000], **FAST)
    sent = [q.payload for q in ep.requests[n:] if q.fn == ep.link_fn and q.op == PS]
    assert [len(q) for q in sent] == [7, 7] and [q[4] for q in sent] == [TRY, COMMIT]
    assert [struct.unpack_from("<I", q)[0] for q in sent] == [750000, 750000]
    assert link.IDLE_MS == 3000 and lk.keepalive_s == link.KEEPALIVE_S < link.IDLE_MS / 1000 / 2
    with pytest.raises(TypeError):
        link.raise_speed(hst, [500000], idle_ms=0, **FAST)   # no such argument any more


def test_verify_ms_stays_a_second_under_the_lease():
    ep, hst, lk = in_process(lease=2400)
    link.raise_speed(hst, [750000])
    assert ep.requests[-3].op == PS and struct.unpack_from("<H", ep.requests[-3].payload, 5)[0] == 1400
    ep2, hst2, lk2 = in_process(lease=10000)
    link.raise_speed(hst2, [750000])
    assert struct.unpack_from("<H", ep2.requests[-3].payload, 5)[0] == link.VERIFY_MS == 2000


def test_a_raised_link_keeps_the_line_alive_when_quiet():
    ep, hst, lk = in_process()
    link.raise_speed(hst, [750000], **FAST)
    sent = []
    frame = lk.keepalive_frame
    lk.keepalive_frame = lambda: sent.append(1) or frame()
    assert not lk.keep_alive()                                  # just spoke: nothing to do
    for _ in range(3):                                          # 3.6 s in all, past the probe's idle limit
        time.sleep(1.2)
        assert lk.keep_alive()
    ep.tick()
    assert ep.port_baud(0) == 750000 and len(sent) == 3
    time.sleep(1.2)
    hst.request(0, m.OP_LOCK_STATE, locked=False)               # a request after 1 s of quiet: a keepalive first
    assert len(sent) == 4 and lk.baud == 750000 and lk.speed_lost == 0
    hst.end()
    time.sleep(1.2)
    assert not lk.keep_alive()                                  # back at the boot speed: none


NO_PROBATION = dict(probation_bytes=0, probation_s=0)   # the window alone (the probation has its own tests)


def raised_in_use(lease=10000, **kw):
    ep, hst, lk = in_process(lease=lease)
    report = link.raise_speed(hst, [921600], **{**FAST, **NO_PROBATION, **kw})
    assert report.chosen == 921600
    return ep, hst, lk, report


def test_in_use_the_3_s_window_over_10_percent_steps_down_for_the_session():
    """Host guide §17.3.2 item 4: the last 3 s judged once 50 frames are in them; over max(2 x baseline, 10 %) broken
    or lost -> revert, the boot speed, never raised again in this session."""
    ep, hst, lk, report = raised_in_use()
    ep.broken_rates[921600] = endpoint.BrokenRate(to_probe=False, every=4)   # answers only: the probe sees nothing
    for _ in range(60):
        hst.request(0, m.OP_LOCK_STATE)                                      # every one answered (a resend at once)
    assert lk.baud == 115200 and ep.port_baud(0) == 115200
    assert report.stepped_down and "frames broken or lost within 3 s" in report.down_why and report.chosen is None
    s, = report.step_downs
    assert s.rate == 921600 and s.ratio is not None and s.ratio > 0.10 and s.why == report.down_why
    assert report.rate == 115200 and "stepped down from 921600" in report.to_text()
    assert ep.holder is not None and ep.holder == hst.session                # the lease held throughout
    hst.keepalive()
    again = link.raise_speed(hst, [921600], **FAST)                          # not again in this session
    assert again.trials[0].why.startswith("broke in use earlier") and again.chosen is None and lk.baud == 115200
    hst.end()
    core.take(hst, 10000)                                                    # a new session may try it again
    del ep.broken_rates[921600]
    assert link.raise_speed(hst, [921600], **FAST).chosen == 921600


def test_in_use_broken_requests_step_down_too_and_the_request_goes_on_at_base():
    ep, hst, lk, report = raised_in_use()
    lk.timeout = 0.05                                                        # the virtual bench answers within a millisecond
    ep.broken_rates[921600] = endpoint.BrokenRate(to_host=False, every=3)    # the probe sees broken candidates: lost
    for _ in range(60):
        hst.request(0, m.OP_LOCK_STATE)
    assert lk.baud == 115200 and ep.port_baud(0) == 115200 and report.stepped_down
    assert ep.holder == hst.session
    hst.keepalive()


def test_in_use_no_judgement_under_50_frames_or_under_the_floor():
    ep, hst, lk, report = raised_in_use()
    ep.broken_rates[921600] = endpoint.BrokenRate(to_probe=False, every=2)   # half the answers: 33 % of the frames
    for _ in range(12):
        hst.request(0, m.OP_LOCK_STATE)                                      # 12 and their resends: under 50 frames
    assert lk.baud == 921600 and not report.stepped_down and lk.retries >= 6 and len(lk.window) < 50
    assert sum(bad for _, bad in lk.window) / len(lk.window) > 0.10          # over the floor, yet not judged
    lk.window.clear()
    ep.broken_rates[921600] = endpoint.BrokenRate(to_probe=False, every=20)
    for _ in range(100):
        hst.request(0, m.OP_LOCK_STATE)                                      # about 5 %: under the floor
    assert lk.baud == 921600 and not report.stepped_down and ep.port_baud(0) == 921600 and len(lk.window) >= 100
    assert all(t - lk.window[0][0] <= link.IN_USE_WINDOW_S for t, _ in lk.window)


def test_in_use_the_threshold_doubles_a_measured_baseline():
    ep, hst, lk = in_process()
    report = link.raise_speed(hst, [921600], flows=[("in", 1)], baseline=0.08, **FAST)   # threshold 16 %
    assert report.chosen == 921600 and lk.baseline_ratio == 0.08
    ep.broken_rates[921600] = endpoint.BrokenRate(to_probe=False, every=8)   # 1 in 9 frames: 11 %
    for _ in range(80):
        hst.request(0, m.OP_LOCK_STATE)
    assert lk.baud == 921600 and not report.stepped_down
    ep.broken_rates[921600] = endpoint.BrokenRate(to_probe=False, every=3)   # 25 %
    for _ in range(80):
        hst.request(0, m.OP_LOCK_STATE)
    assert lk.baud == 115200 and report.stepped_down and "over 16%" in report.down_why


def test_no_answer_at_a_raised_rate_falls_back_well_inside_the_lease():
    ep, hst, lk, report = raised_in_use(lease=3000)
    lk.timeout = 3.0                                                         # the default: 2 x 3 s would pass the lease
    ep._speed_revert()                                                       # the probe went back by itself, silently
    t0 = time.monotonic()
    r = hst.request(0, m.OP_LOCK_STATE)                                      # sent again at the boot speed
    took = time.monotonic() - t0
    assert r.succeeded and took < 2.0 and lk.baud == 115200
    assert report.lost and report.stepped_down and "no answer" in report.down_why
    assert report.step_downs[0].ratio is None and ep.holder == hst.session
    again = link.raise_speed(hst, [921600], **FAST)
    assert again.trials[0].why.startswith("broke in use earlier") and lk.baud == 115200


def test_raised_in_use_the_first_wait_is_at_most_a_third_of_the_idle_time_never_under_the_floor():
    """Host guide §17.3.2 item 4: the first wait for an answer at a raised rate in use ends well inside
    port_speed_idle_ms (RAISED_FIRST_WAIT_S), so its resend reaches a probe still at the rate; never under core §4.4's
    floor (argument time + host_wait_add_ms + the transfer time)."""
    assert link.RAISED_FIRST_WAIT_S == pytest.approx(link.IDLE_MS / 1000 / 3) == pytest.approx(1.0)
    ep, hst, lk, report = raised_in_use(lease=60000)
    lk.timeout = 3.0
    assert lk._wait() == pytest.approx(link.RAISED_FIRST_WAIT_S)               # floor (transfer time alone) under it
    lk.wait_add_s = 2.0                                                        # a floor over it: the floor
    assert lk._wait() == pytest.approx(lk.wait_floor_s()) and lk.wait_floor_s() > link.RAISED_FIRST_WAIT_S
    lk.wait_add_s = 0.0
    hst.expect_ms = 2500                                                       # an argument time (core §4.4) too
    assert lk._wait() == pytest.approx(lk.wait_floor_s()) and lk._wait() >= 2.5
    hst.expect_ms = 0
    hst.end()                                                                  # the boot speed: the link's timeout
    assert lk.baud == 115200 and lk._wait() == pytest.approx(3.0)


def answer_dropped(ep, lk, op):
    """The virtual bench's first answer to a core request `op` is lost on the line (once). -> the corrs dropped (a list)."""
    dropped = []

    def answer(n, result):
        if not dropped and ep.requests[-1].fn == 0 and ep.requests[-1].op == op:
            dropped.append(result[1] | result[2] << 8)
            return None
        return cobs.frame(result)
    lk.stream.port.answer_filter = answer
    return dropped


def test_raised_a_lost_answer_is_resent_at_the_raised_rate_first_and_no_step_down():
    """Host guide §17.3.2 item 4: no answer within the first wait - the request again at the raised rate, same corr
    (core §5.2); the probe answers it from its resend table and the link stays at the rate."""
    ep, hst, lk, report = raised_in_use(lease=60000)
    lk.timeout = 3.0
    dropped = answer_dropped(ep, lk, m.OP_LOCK_STATE)
    n = len(ep.requests)
    t0 = time.monotonic()
    assert hst.request(0, m.OP_LOCK_STATE).succeeded
    took = time.monotonic() - t0
    assert dropped and link.RAISED_FIRST_WAIT_S - 0.1 < took < link.RAISED_FIRST_WAIT_S + 0.5
    sent = ep.requests[n:]
    assert [q.op for q in sent] == [m.OP_LOCK_STATE] * 2 and sent[0].corr == sent[1].corr == dropped[0]
    assert lk.baud == 921600 and ep.port_baud(0) == 921600 and lk.retries == 1 and lk.speed_lost == 0
    assert not report.stepped_down and report.step_downs == [] and ep.speed_log == [(0, 921600)]
    hst.keepalive()


def test_raised_no_answer_to_the_resend_either_then_the_boot_speed():
    """The probe went back by itself: the request at the raised rate, again at it (same corr), and only then the
    boot speed, its confirm, and the request once more there (host guide §17.3.2 item 4)."""
    ep, hst, lk, report = raised_in_use(lease=60000)
    lk.timeout = 3.0
    writes = []
    write = lk.stream.write

    def logged(data):
        for f in bytes(data).split(b"\x00"):
            if f:
                writes.append((lk.stream.baudrate, m.Request.unpack(cobs.unframe(f))))
        return write(data)
    lk.stream.write = logged
    ep._speed_revert()                                                       # silently back at the boot speed
    assert hst.request(0, m.OP_LOCK_STATE).succeeded
    corr = next(q.corr for _, q in writes if q.op == m.OP_LOCK_STATE)
    mine = [(rate, q.op) for rate, q in writes if q.corr == corr]
    assert mine == [(921600, m.OP_LOCK_STATE), (921600, m.OP_LOCK_STATE), (115200, m.OP_LOCK_STATE)]
    first_base = next(i for i, (rate, _) in enumerate(writes) if rate == 115200)
    assert writes[first_base][1].op == m.OP_CONFIRM                          # confirmed there before the request
    assert lk.baud == 115200 and report.lost and report.stepped_down


def test_no_answer_and_no_confirm_at_the_boot_speed_is_a_link_error():
    ep, hst, lk, report = raised_in_use()
    ep._speed_revert = lambda: None                                          # the probe never comes back
    ep.broken_rates[921600] = endpoint.BrokenRate(to_host=False)             # ... and hears nothing more
    ep.speed_frame = lambda port, good: None
    t0 = time.monotonic()
    with pytest.raises(ConnectionError, match="neither at 921600 nor at the boot speed"):
        hst.request(0, m.OP_LOCK_STATE)
    assert link.OPEN_RETRY_S - 0.5 < time.monotonic() - t0 < link.OPEN_RETRY_S + 2.5   # idle max + 1 s of confirms
    assert lk.baud == 115200                                                 # never back to the raised rate


def test_a_long_run_at_a_raised_rate_waits_its_timeout_ms_without_a_step_down():
    from oep_client import riscv
    ep, hst, lk, report = raised_in_use(lease=3000)
    lk.timeout = 3.0                                     # the default: an ordinary request waits 0.75 s (lease / 4)
    stream, held, late = lk.stream, {}, []
    pull = stream._pull

    def answer(n, result):                               # the run's answer (and any repeat of it) comes 2 s later
        corr = result[1] | result[2] << 8
        if late or corr in held:
            held.setdefault(corr, time.monotonic() + 2.0)
            late.clear()
            stream._held.append((held[corr], cobs.frame(result)))
            return None
        return cobs.frame(result)

    def pull_late():
        pull()
        now = time.monotonic()
        stream._rx += b"".join(w for at, w in stream._held if at <= now)
        stream._held[:] = [(at, w) for at, w in stream._held if at > now]
    stream._held = []
    stream._pull = pull_late
    stream.port.answer_filter = answer
    wire = riscv.Wire(hst, "oep.wire.swio")
    conn, _ = wire.attach()
    dm = riscv.RiscvDm(hst, conn)
    dm.halt()
    ep.target.run_hook = lambda pc, regs: (late.append(1), (True, pc + 4, 1_900_000))[1]
    t0 = time.monotonic()
    r = dm.run(0x20000000, [], timeout_ms=2000)
    assert r.stopped and 1.9 < time.monotonic() - t0 < 3.0
    assert lk.baud == 921600 and not report.stepped_down and lk.retries == 0 and not any(bad for _, bad in lk.window)
    assert ep.holder == hst.session
    hst.keepalive()                                      # after 2 s of quiet: the link's keepalive first, a lower corr
    corrs = [q.corr for q in ep.requests[-2:]]
    assert corrs == sorted(corrs) and [q.op for q in ep.requests[-2:]] == [m.OP_KEEPALIVE] * 2
    assert hst.expect_ms == 0                            # only that request waited longer


class RefusingStream:
    """A pyserial-shaped stream whose driver refuses some baud rates (pyserial raises ValueError)."""

    def __init__(self, refuse):
        self._baud, self.refuse, self.timeout, self.in_waiting = 115200, refuse, 0.05, 0

    @property
    def baudrate(self):
        return self._baud

    @baudrate.setter
    def baudrate(self, v):
        if v in self.refuse:
            raise ValueError(f"Not a valid baudrate: {v}")
        self._baud = v

    def read(self, n=1):
        return b""

    def write(self, data):
        return len(data)

    def reset_input_buffer(self):
        pass


def test_set_baud_switches_to_the_requested_rate_and_to_the_answer_only_when_the_os_refuses():
    lk = link.SerialLink.on_stream(RefusingStream({1500000}), "cobs", 0.1)
    assert lk.set_baud(921600, 922190) == 921600 and lk.stream.baudrate == 921600 and lk.baud == 921600
    assert lk.set_baud(1500000, 1499250) == 1499250 and lk.stream.baudrate == 1499250   # the OS refused: the answer
    with pytest.raises(ValueError):
        lk.set_baud(1500000)                                                 # no fallback given
    with pytest.raises(ValueError):
        lk.set_baud(1500000, 1500000)


def test_set_baud_handles_native_posix_driver_refusal():
    termios = pytest.importorskip("termios")

    class PosixRefusingStream(RefusingStream):
        @RefusingStream.baudrate.setter
        def baudrate(self, rate):
            if rate in self.refuse:
                raise termios.error(22, "Invalid argument")
            self._baud = rate

    lk = link.SerialLink.on_stream(PosixRefusingStream({1500000}), "cobs", 0.1)
    assert lk.set_baud(1500000, 1499250) == 1499250
    assert lk.baud == lk.stream.baudrate == 1499250
    with pytest.raises(termios.error):
        lk.set_baud(1500000, 1500000)


def test_native_driver_refusal_is_recorded_and_next_candidate_is_tried(monkeypatch):
    termios = pytest.importorskip("termios")
    ep, hst, lk = in_process()
    set_baud = lk.set_baud

    def refuse(rate, fallback=None):
        if rate == 750000:
            raise termios.error(22, "Invalid argument")
        return set_baud(rate, fallback)

    monkeypatch.setattr(lk, "set_baud", refuse)
    try:
        report = link.raise_speed(hst, [750000, 500000], **FAST)
        assert report.chosen == 500000
        refused, accepted = report.trials
        assert not refused.committed and "the OS refuses" in refused.why
        assert accepted.committed and lk.baud == 500000
        hst.keepalive()
    finally:
        hst.end()
    assert lk.baud == 115200


def second_host(ep):
    lk = link.SerialLink.on_stream(virtual_bench_serial.VirtualSerialStream(ep, 0), "cobs", 0.5)
    lk.transport = "serial"
    hst = h.Host(lk.send)
    t0 = time.monotonic()
    lk.attach_host(hst)
    return hst, lk, time.monotonic() - t0


def test_open_waits_out_a_raised_rate_a_host_that_died_left():
    """transports §4: a host opening a UART bridge repeats its confirm at the boot speed for port_speed_idle_ms +
    host_wait_add_ms. Its confirms reach the probe as broken candidates (no good frame), so the probe goes back after
    port_speed_idle_ms (oep-if-link §3) and a confirm then passes."""
    ep, hst, lk = in_process()
    link.raise_speed(hst, [750000], **FAST)                     # this host dies here, its lease long
    hst2, lk2, took = second_host(ep)
    assert ep.port_baud(0) == 115200 and lk2.baud == 115200 and 2.5 < took < link.OPEN_RETRY_S + 0.5
    assert link.OPEN_RETRY_S == 4.0
    hst2.confirm()


def test_open_gives_up_on_a_probe_that_never_comes_back():
    ep2, hst3, lk3 = in_process()
    link.raise_speed(hst3, [750000], **FAST)
    ep2._speed_revert = lambda: None                            # never comes back: the open gives up
    lk4 = link.SerialLink.on_stream(virtual_bench_serial.VirtualSerialStream(ep2, 0), "cobs", 0.5)
    lk4.corr_source = lambda: 7
    with pytest.raises(TimeoutError):
        lk4.wait_boot_speed(0.6)


def test_an_off_probe_is_not_supported_and_stays_at_the_boot_speed(monkeypatch):
    ep, hst, lk = in_process()
    ep.port_speed_base = None
    hst._describes.clear()
    report = link.raise_speed(hst, [1500000], **FAST)
    assert not report.supported and "does not offer port_speed" in report.why and report.rate == 115200 and lk.baud == 115200
    monkeypatch.setattr(link, "_speed_port", lambda hst: (ep.link_fn, 0, ""))   # taken as offered, the op is unknown
    report = link.raise_speed(hst, [1500000], **FAST)
    assert not report.supported and "unknown_operation" in report.why and not report.trials
    assert "not supported" in report.to_text() and lk.baud == 115200
    hst.keepalive()


def test_not_a_serial_port_of_its_own():
    ep, hst, lk = in_process()
    lk.transport = "tcp"
    report = link.raise_speed(hst, [1500000])
    assert not report.supported and "serial port" in report.why


# ---- step downs, the probation, max_tries (host guide §17.3.2 item 4) ----------------------------------------------------

def move(hst, until, size=40, limit_s=3.0):
    """In-use traffic: oep.link source answers of `size` bytes until `until()` (at most `limit_s`)."""
    deadline = time.monotonic() + limit_s
    fn = core.link_fn(hst)
    while not until() and time.monotonic() < deadline:
        hst.request(fn, core.LINK_SOURCE, core.link_source_request(size))


def test_in_use_a_breakdown_steps_down_to_the_next_lower_candidate_not_at_or_above_a_failed_one():
    """Rule: the next lower candidate that has not failed in this session gets a fresh try -> confirm -> commit; a rate
    that broke is not tried again, nor anything above it; none left: the boot speed."""
    ep, hst, lk = in_process()
    ep.broken_rates[1500000] = endpoint.BrokenRate(to_probe=False)              # no confirm there
    report = link.raise_speed(hst, [1500000, 921600, 500000, 230400], **FAST, **NO_PROBATION)
    assert report.chosen == 921600 and lk.failed.keys() == {1500000} and lk.unusable == {}
    ep.broken_rates[921600] = endpoint.BrokenRate(to_probe=False, every=3)
    n = len(ep.requests)
    for _ in range(60):
        hst.request(0, m.OP_LOCK_STATE)
    s, = report.step_downs
    assert s.rate == 921600 and s.to == 500000 and not s.probation and "within 3 s" in s.why
    assert lk.baud == 500000 and ep.port_baud(0) == 500000 and report.chosen == report.rate == 500000
    assert (PS, TRY) in ops(ep, n) and (PS, COMMIT) in ops(ep, n)                # a fresh try and commit at 500000
    assert [t.rate for t in report.trials if t.committed] == [921600, 500000] and report.stepped_down
    assert "-> 500000" in report.to_text() and "in force: 500000 (raised)" in report.to_text()
    ep.broken_rates[500000] = endpoint.BrokenRate(to_probe=False, every=3)
    for _ in range(60):
        hst.request(0, m.OP_LOCK_STATE)
    assert [(s.rate, s.to) for s in report.step_downs] == [(921600, 500000), (500000, 230400)]
    assert lk.baud == 230400 and lk.unusable.keys() == {921600, 500000}
    again = link.raise_speed(hst, [921600, 750000, 230400], **FAST, **NO_PROBATION)   # later in the session: no up
    assert again.trials[0].why.startswith("broke in use earlier") and again.trials[1].why.startswith("above 500000")
    hst.keepalive()


def test_in_use_no_step_down_to_a_rate_above_one_that_failed_its_verify():
    ep, hst, lk = in_process()
    ep.broken_rates[230400] = endpoint.BrokenRate(to_probe=False)              # fails first (an odd order)
    report = link.raise_speed(hst, [230400, 921600, 500000], **FAST, **NO_PROBATION)
    assert report.chosen == 921600
    ep.broken_rates[921600] = endpoint.BrokenRate(to_probe=False, every=3)
    for _ in range(60):
        hst.request(0, m.OP_LOCK_STATE)
    s, = report.step_downs
    assert s.to == 115200 and lk.baud == 115200 and report.chosen is None    # 500000 is above 230400, which failed
    assert [t.rate for t in report.trials] == [230400, 921600]
    assert "the boot speed for the rest of the session" in report.to_text()


def test_the_probation_fails_a_rate_that_passes_the_quick_verify_and_breaks_later():
    """The field case modelled: a rate passes the 16-frame verify, breaks after some kilobytes. In its probation that is
    a verify failure: a step down at once to the next lower candidate, whose probation then passes."""
    from oep_client import speed_record
    ep, hst, lk = in_process(virtual_bench.esp32_v003_64)         # 64-byte frames: the 16-frame verify stays under 3000 bytes
    rec = speed_record.SpeedRecord(None)
    rec.path = None
    notes = []
    rec.note = lambda port, unit, rate, passed, phase="": notes.append((rate, passed, phase))
    ep.broken_rates[921600] = endpoint.BrokenRate(min_size=40, to_probe=False, after=3000)   # past the verify's bytes
    report = link.raise_speed(hst, [921600, 500000], flows=[("in", 1)], record=rec, probation_bytes=4096,
                              probation_s=0.3, **FAST)
    t = report.trials[0]
    assert t.committed and all(f.passed for f in t.flows) and t.probation == "running"
    assert notes == [(921600, True, "verify")]
    move(hst, lambda: lk.baud != 921600)
    s, = report.step_downs
    assert s.rate == 921600 and s.probation and s.to == 500000 and s.why.startswith("in probation")
    assert t.probation == "failed" and 0 < t.probation_bytes < 4096 and (921600, False, "probation") in notes
    t2 = report.trials[-1]
    assert t2.rate == 500000 and t2.committed and t2.flows and t2.probation == "running" and t2.settling
    move(hst, lambda: t2.probation != "running")
    assert t2.probation == "passed" and t2.probation_bytes >= 4096 and lk.probation is None
    assert notes[-2:] == [(500000, True, "verify"), (500000, True, "probation")]
    assert lk.baud == 500000 and "probation passed" in report.to_text() and "(in probation)" in report.to_text()


def test_without_the_probation_the_same_rate_breaks_only_in_use():
    ep, hst, lk = in_process(virtual_bench.esp32_v003_64)         # 64-byte frames: the verify stays under `after`
    ep.broken_rates[921600] = endpoint.BrokenRate(min_size=40, to_probe=False, after=3000, every=3)
    report = link.raise_speed(hst, [921600, 500000], flows=[("in", 1)], **FAST, **NO_PROBATION)
    assert report.trials[0].committed and report.trials[0].probation == "off"
    move(hst, lambda: lk.baud != 921600)
    s, = report.step_downs
    assert not s.probation and "within 3 s" in s.why and s.to == 500000


def test_a_failure_soon_after_a_breakdown_at_another_rate_is_noted_unknown(tmp_path):
    """Record rule: results measured within settle_s of a breakdown (or a step down) at another rate are unknown."""
    from oep_client import speed_record
    path = tmp_path / "link-speed.json"
    ep, hst, lk = in_process()
    ep.broken_rates[921600] = endpoint.BrokenRate(min_size=40, to_probe=False)
    ep.broken_rates[500000] = endpoint.BrokenRate(min_size=40, to_probe=False)
    report = link.raise_speed(hst, [921600, 500000], flows=[("in", 1)], record=str(path), **FAST)
    a, b = report.trials
    assert not a.settling and b.settling and report.chosen is None
    rec = speed_record.SpeedRecord(path)
    assert rec.results("<stream>", UNIT) == {921600: "failed", 500000: "unknown"}
    assert rec.lookup("<stream>", UNIT) == ([], [921600])                    # an unknown is in neither list
    saved = json.loads(path.read_text())[f"<stream>|{UNIT}"]["rates"]
    assert saved["500000"] == {**saved["500000"], "result": "unknown", "passed": None, "phase": "verify"}
    assert "unknown" in report.to_text()
    hst.end()
    core.take(hst, 10000)
    report = link.raise_speed(hst, [921600, 500000], flows=[("in", 1)], record=str(path), settle_s=0, **FAST)
    assert [t.rate for t in report.trials] == [500000] and report.skipped == [921600]
    assert speed_record.SpeedRecord(path).results("<stream>", UNIT)[500000] == "failed"


def test_when_the_record_marks_every_candidate_failed_the_slowest_is_tried_once(tmp_path):
    from oep_client import speed_record
    rec = speed_record.SpeedRecord(tmp_path / "r.json")
    for rate in (1500000, 921600, 500000):
        rec.note("<stream>", UNIT, rate, False, "verify")
    ep, hst, lk = in_process()
    report = link.raise_speed(hst, [1500000, 921600, 500000], record=rec, max_tries=1, **FAST)
    assert report.retried == 500000 and report.skipped == [1500000, 921600] and report.chosen == 500000
    assert [t.rate for t in report.trials] == [500000] and "500000 (the slowest) tried once" in report.to_text()
    assert rec.lookup("<stream>", UNIT) == ([500000], [1500000, 921600])


def test_max_tries_bounds_the_candidates_tried_and_the_step_downs():
    ep, hst, lk = in_process()
    ep.broken_rates[1500000] = endpoint.BrokenRate(to_probe=False)
    report = link.raise_speed(hst, [1500000, 921600, 500000], max_tries=2, **FAST, **NO_PROBATION)
    assert [t.rate for t in report.trials] == [1500000, 921600] and report.capped == [500000]
    assert report.chosen == 921600 and lk.speed_plan.rates == [1500000, 921600]
    assert "left out (max_tries): 500000" in report.to_text()
    ep.broken_rates[921600] = endpoint.BrokenRate(to_probe=False, every=3)
    for _ in range(60):
        hst.request(0, m.OP_LOCK_STATE)
    assert report.step_downs[0].to == 115200 and lk.baud == 115200          # 500000 is not in this call's tries
    ep2, hst2, lk2 = in_process()
    ep2.broken_rates[1500000] = endpoint.BrokenRate(to_probe=False)
    report = link.raise_speed(hst2, [1500000, 921600], max_tries=1, **FAST)
    assert [t.rate for t in report.trials] == [1500000] and report.capped == [921600] and report.chosen is None


# ---- the record (host guide §17.4) --------------------------------------------------------------------------------------

def test_the_record_puts_passed_rates_first_skips_failed_ones_and_expires(tmp_path):
    from oep_client import speed_record
    path = tmp_path / "link-speed.json"
    rec = speed_record.SpeedRecord(path)
    ep, hst, lk = in_process()
    ep.broken_rates[230400] = endpoint.BrokenRate(to_probe=False)            # no confirm there
    report = link.raise_speed(hst, [230400, 500000], record=rec, **FAST)
    assert report.chosen == 500000 and report.skipped == []
    unit = "fafe00000003"                                                    # the virtual bench esp32-v003's unit_id
    assert rec.lookup("<stream>", unit) == ([500000], [230400]) and lk.record_key == ("<stream>", unit)
    saved = json.loads(path.read_text())
    assert saved[f"<stream>|{unit}"]["rates"]["500000"]["passed"] is True
    assert saved[f"<stream>|{unit}"]["rates"]["230400"]["passed"] is False
    hst.end()
    core.take(hst, 10000)
    report = link.raise_speed(hst, [921600, 230400, 500000], record=str(path), **FAST)   # a path: the same file
    assert report.skipped == [230400] and [t.rate for t in report.trials] == [500000]   # passed first, failed out
    assert report.chosen == 500000 and "skipped (the record says failed): 230400" in report.to_text()
    ep.broken_rates[500000] = endpoint.BrokenRate(to_probe=False, every=3)   # in use it breaks: the step down is noted
    for _ in range(60):
        hst.request(0, m.OP_LOCK_STATE)
    assert report.stepped_down
    assert rec.lookup("<stream>", unit) == ([], [500000, 230400]) if False else \
        speed_record.SpeedRecord(path).lookup("<stream>", unit) == ([], [500000, 230400])
    data = json.loads(path.read_text())
    data[f"<stream>|{unit}"]["rates"]["230400"]["at"] = "2026-08-01T00:00:00+00:00"   # older than 30 days
    path.write_text(json.dumps(data))
    rec2 = speed_record.SpeedRecord(path)
    assert rec2.lookup("<stream>", unit) == ([], [500000])
    rec2.save()
    assert "230400" not in path.read_text()
    assert speed_record.SpeedRecord(path).lookup("/dev/other", unit) == ([], [])     # another port: nothing known


def test_the_record_keeps_a_failure_a_day_and_a_pass_30_days(tmp_path):
    import datetime as dt
    from oep_client import speed_record
    path = tmp_path / "r.json"
    rec = speed_record.SpeedRecord(path)
    for rate, passed in ((1500000, False), (921600, None), (500000, True), (230400, True)):
        rec.note("p", "u", rate, passed, "verify")
    assert rec.lookup("p", "u") == ([500000, 230400], [1500000])
    data = json.loads(path.read_text())
    ago = lambda **kw: (dt.datetime.now(dt.timezone.utc) - dt.timedelta(**kw)).isoformat(timespec="seconds")
    rates = data["p|u"]["rates"]
    rates["1500000"]["at"] = ago(hours=25)                                  # a failure: past 1 day
    rates["921600"]["at"] = ago(hours=23)                                   # an unknown: within it
    rates["500000"]["at"] = ago(days=29)                                    # a pass: within 30 days
    rates["230400"]["at"] = ago(days=31)
    rates["115201"] = {"passed": False, "at": ago(hours=1)}                 # the older shape: no result, no phase
    path.write_text(json.dumps(data))
    rec = speed_record.SpeedRecord(path)
    assert rec.results("p", "u") == {921600: "unknown", 500000: "passed", 115201: "failed"}
    assert rec.lookup("p", "u") == ([500000], [115201])
    assert speed_record.FAIL_TTL_S == 86400 and speed_record.PASS_TTL_S == 30 * 86400
    rec = speed_record.SpeedRecord(path, fail_ttl=3600 * 26)
    assert rec.lookup("p", "u") == ([500000], [1500000, 115201])


def test_the_record_is_a_cache_an_unreadable_file_is_not_an_error(tmp_path):
    from oep_client import speed_record
    path = tmp_path / "broken.json"
    path.write_text("{not json")
    rec = speed_record.SpeedRecord(path)
    assert rec.error and rec.lookup("p", "u") == ([], [])
    rec.note("p", "u", 500000, True)
    assert speed_record.SpeedRecord(path).lookup("p", "u") == ([500000], [])
    unwritable = speed_record.SpeedRecord(tmp_path / "nope" / "x" / "y.json")
    unwritable.path.parent.parent.mkdir()
    unwritable.path.parent.parent.chmod(0o500)
    try:
        unwritable.note("p", "u", 1, True)
        assert unwritable.error is None or "y.json" in unwritable.error
    finally:
        unwritable.path.parent.parent.chmod(0o700)
    assert speed_record.default_path().name == "link-speed.json" and speed_record.default_path().parent.name == "oep-client"


# ---- the default ceiling (host guide §17, §17.3.3) --------------------------------------------------------------------

def try_verify_ms(ep, rate):
    """verify_ms of the host's try at `rate`."""
    q = next(q for q in ep.requests if q.fn == ep.link_fn and q.op == PS and q.payload[4] == TRY
             and struct.unpack_from("<I", q.payload, 0)[0] == rate)
    return struct.unpack_from("<H", q.payload, 5)[0]


def test_ceiling_the_default_is_500000_alone_with_no_measurement():
    ep, hst, lk = in_process()
    assert link.DEFAULT_CANDIDATES == (500000,) and link.DEFAULT_CEILING == 500000
    report = link.raise_speed(hst)
    t, = report.trials
    assert t.committed and t.flows == [] and lk.baud == 500000 and try_verify_ms(ep, 500000) == link.VERIFY_MS


def test_ceiling_a_faster_rate_is_committed_only_after_full_frames_ran_1_s_each_way():
    """The minimal form too: in (source) and out (sink) of max_frame - 26 bytes, each for at least 1 s at the link's
    in-flight count, in the try state; the try asks verify_ms 4000 so the probe waits for it."""
    ep, hst, lk = in_process()
    report = link.raise_speed(hst, [921600])
    t, = report.trials
    assert t.committed and report.chosen == 921600 and not report.verified
    assert [(f.flow, f.n) for f in t.flows] == [("in", lk.inflight_for(hst.limits)), ("out", lk.inflight_for(hst.limits))]
    size = core.link_size(hst.limits["max_frame"])
    for f in t.flows:
        assert f.passed and f.frames > link.FLOW_FRAMES and f.frames * size / (f.kb_s * 1000) >= link.FAST_VERIFY_S
    assert try_verify_ms(ep, 921600) == link.fast_verify_ms(2) == 4000
    assert [o for o in ops(ep) if o[0] == PS][-2:] == [(PS, TRY), (PS, COMMIT)]       # the flows between them
    assert t.probation == "running"                     # and the in-use judging as for any rate


def test_ceiling_a_faster_rate_that_breaks_past_the_quick_verify_fails_its_1_s_verify():
    """The field case: 16 frames would pass (the breakage starts past them), 1 s of full frames does not. The rate is
    not committed; 500000 after it is, with no 1 s verify."""
    ep, hst, lk = in_process()
    size = core.link_size(hst.limits["max_frame"] if hst.limits else hst.confirm()["max_frame"])
    ep.broken_rates[921600] = endpoint.BrokenRate(min_size=40, to_probe=False, after=20 * (size + 30), every=3)
    report = link.raise_speed(hst, [921600, 500000], flows=[("in", 1)], **FAST)
    a, b = report.trials
    assert not a.committed and a.flows[0].flow == "in" and a.flows[0].frames > link.FLOW_FRAMES
    assert "full frames in 1 s broken or lost" in a.why and lk.failed.keys() == {921600}
    assert b.committed and report.chosen == 500000 and [f.flow for f in b.flows] == ["in"]
    assert b.flows[0].frames == link.FLOW_FRAMES                                   # the quick verify at 500000


def test_ceiling_the_full_form_adds_duplex_and_a_6_s_verify_ms():
    ep, hst, lk = in_process()
    report = link.raise_speed(hst, [921600], flows=[("in", 1), ("duplex", 1)])
    t, = report.trials
    assert t.committed and [(f.flow, f.n) for f in t.flows] == [("in", 1), ("out", lk.inflight_for(hst.limits)),
                                                                ("duplex", 1)]
    assert try_verify_ms(ep, 921600) == 6000


def test_ceiling_a_lease_too_short_for_the_1_s_verify_skips_the_rate():
    assert link.lease_for([500000, 230400]) == 0
    assert link.lease_for([921600, 500000]) == 5000 and link.lease_for([921600], flows=[("out", 2)]) == 5000
    assert link.lease_for([921600], verify=True) == 7000 and link.lease_for([921600], flows=["duplex"]) == 7000
    ep, hst, lk = in_process(lease=3000)
    report = link.raise_speed(hst, [921600, 500000], **FAST)
    a, b = report.trials
    assert not a.committed and "needs verify_ms 4000 and a lease of 5000 ms" in a.why and a.actual is None
    assert b.committed and report.chosen == 500000 and lk.failed == {}


# ---- a pty and the CLI -------------------------------------------------------------------------------------------------

@pytest.mark.skipif(sys.platform != "linux", reason="a pty")
def test_open_host_with_port_speed_on_a_pty():
    proc = subprocess.Popen([sys.executable, "-m", "oep_client.virtual_bench_serve", "--pty", "--profile", "esp32-v003",
                             "--broken-rate", "230400"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        where = proc.stdout.readline().split()
        hst = link.open_host(where[1], timeout=1.0, port_speed=[230400, 500000])
        report = hst.link.speed
        assert [t.committed for t in report.trials] == [False, True] and report.chosen == 500000
        assert report.trials[0].why == "no confirm at the new rate" and not report.verified
        assert hst.link.stream.baudrate == 500000 and hst.link.port_path == where[1]
        hst.keepalive()
        hst.end()
        assert hst.link.stream.baudrate == 115200
        hst.confirm()
        hst.link.close()
    finally:
        proc.stdin.close()
        proc.wait(5)


@pytest.mark.skipif(sys.platform != "linux", reason="a pty")
def test_oep_speed_cli_prints_the_report_and_keeps_the_record(capsys, tmp_path, monkeypatch):
    from oep_client import __main__ as cli
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    proc = subprocess.Popen([sys.executable, "-m", "oep_client.virtual_bench_serve", "--pty", "--profile", "esp32-v003",
                             "--broken-rate", "230400:40:in"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        where = proc.stdout.readline().split()
        assert cli.main(["speed", where[1], "--candidates", "230400,921600", "--flows", "in:1"]) == 0
        out = capsys.readouterr().out
        assert "in@1" in out and "failed" in out and "committed" in out and "in force: 921600 (raised)" in out
        record = json.loads((tmp_path / "oep-client" / "link-speed.json").read_text())
        rates = record[f"{where[1]}|fafe00000003"]["rates"]
        assert rates["230400"]["passed"] is False and rates["921600"]["passed"] is True
        assert cli.main(["speed", where[1], "230400,921600", "--minimal", "--json"]) == 0   # the record skips 230400
        out = json.loads(capsys.readouterr().out)
        assert out["skipped"] == [230400] and [t["rate"] for t in out["trials"]] == [921600] and out["chosen"] == 921600
        assert cli.main(["speed", where[1], "921600,500000", "--minimal", "--json", "--max-tries", "1"]) == 0
        out = json.loads(capsys.readouterr().out)
        assert [t["rate"] for t in out["trials"]] == [921600] and out["capped"] == [500000]
        assert out["trials"][0]["probation"] == "running"
        assert cli.main(["speed", where[1], "--no-record"]) == 0                 # the default candidate, 500000
        assert "in force: 500000 (raised)" in capsys.readouterr().out
    finally:
        proc.stdin.close()
        proc.wait(5)


@pytest.mark.skipif(sys.platform != "linux", reason="a pty")
def test_ceiling_oep_speed_tries_a_faster_rate_only_when_named_and_verifies_it_1_s_each_way(capsys):
    from oep_client import __main__ as cli
    proc = subprocess.Popen([sys.executable, "-m", "oep_client.virtual_bench_serve", "--pty", "--profile", "esp32-v003"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        where = proc.stdout.readline().split()
        assert cli.main(["speed", where[1], "--no-record", "--json"]) == 0
        out = json.loads(capsys.readouterr().out)
        assert [t["rate"] for t in out["trials"]] == [500000] and out["trials"][0]["flows"] == []
        assert cli.main(["speed", where[1], "921600,500000", "--no-record", "--json"]) == 0
        out = json.loads(capsys.readouterr().out)
        t = out["trials"][0]
        assert out["chosen"] == 921600 and t["committed"] and [f["flow"] for f in t["flows"]] == ["in", "out"]
        assert all(f["passed"] and f["frames"] > link.FLOW_FRAMES for f in t["flows"])
    finally:
        proc.stdin.close()
        proc.wait(5)
