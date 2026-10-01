"""port_speed (oep-core §3.5): the fake's state machine (try / commit / revert, the timers, broken candidates, the
session's end, the wrong port, off), and the client's opt-in raise_speed against it - in process, where the fake
also sees the host's own rate (`FakeSerialStream`), and on a pty through open_host(port_speed=...)."""

import struct
import subprocess
import sys
import time

import pytest

from oep_client import cobs, core, endpoint, fake, fake_serial, host as h, link, message as m

TRY, COMMIT, REVERT = 0, 1, 2
PS = endpoint.OP_PORT_SPEED


class Clock:
    def __init__(self):
        self.t = 1000

    def __call__(self):
        return self.t


def ps(port, baud, step, verify_ms=500, idle_ms=0):
    return struct.pack("<BIBHI", port, baud, step, verify_ms, idle_ms)


def framed(req):
    return cobs.frame(req.pack())


class Port:
    """An endpoint's serial port 0 driven by whole requests, its answers decoded (a broken one: None)."""

    def __init__(self, profile=fake.esp32_v003):
        self.clock = Clock()
        self.ep = endpoint.Endpoint(profile(), self.clock)
        self.port = fake_serial.FakeSerialPort(self.ep, 0)
        self.corr = 0

    def send(self, fn, op, payload=b"", session=None, raw=False):
        self.corr += 1
        self.port.feed(framed(m.Request(self.corr, fn, op, payload, session)))
        out = self.port.output()
        if not out:
            return None
        try:
            return m.Result.unpack(cobs.unframe(out[1:-1]))
        except cobs.CorruptFrame:
            return None

    def noise(self):
        self.port.feed(b"\x00\x31\x32\x33\x00")      # a candidate whose CRC does not match

    def tick(self, ms):
        self.clock.t += ms
        self.port.tick()


def opened(p, sid=5, lease=60000):
    assert p.send(0, m.OP_OPEN, struct.pack("<IIB", sid, lease, 0)).succeeded
    return sid


# ---- the fake's state machine ------------------------------------------------------------------------------------------

def test_describe_declares_it_only_when_on_and_off_is_unknown_operation():
    p = Port()
    assert any(t[0] == endpoint.PORT_SPEED_TAG for t in p.ep._declarations(0))
    p.ep.port_speed_base = None
    assert not any(t[0] == endpoint.PORT_SPEED_TAG for t in p.ep._declarations(0))
    sid = opened(p)
    r = p.send(0, PS, ps(0, 1500000, TRY), sid)
    assert r.resolution == m.REJECTED and r.detail == m.UNKNOWN_OPERATION


def test_try_then_commit_answered_at_the_old_speed():
    p = Port()
    sid = opened(p)
    assert p.send(0, PS, ps(0, 1500000, TRY)).detail == m.SESSION_REQUIRED     # the lock is needed
    r = p.send(0, PS, ps(0, 1500000, TRY, 800), sid)
    assert r.succeeded and m.Reader(r.payload).u32() == 1500000
    assert p.ep.speed_state == "try" and p.ep.port_baud(0) == 1500000
    r = p.send(0, PS, ps(0, 1000000, COMMIT), sid)                              # another baud: cause 6
    assert r.detail == m.UNAVAILABLE and r.payload[:3] == bytes([0x01, 1, 6])
    r = p.send(0, PS, ps(0, 1500000, COMMIT), sid)
    assert r.succeeded and p.ep.speed_state == "committed"
    p.tick(endpoint.SPEED_IDLE_MAX_MS - 1)                                      # idle_ms 0: the maximum, not never
    assert p.ep.port_baud(0) == 1500000
    p.tick(1)
    assert p.ep.port_baud(0) == 115200


def test_a_long_idle_ms_is_clamped_to_the_maximum():
    p = Port()
    sid = opened(p)
    p.send(0, PS, ps(0, 500000, TRY), sid)
    assert p.send(0, PS, ps(0, 500000, COMMIT, 0, 600_000), sid).succeeded
    assert p.ep.speed_idle_ms == endpoint.SPEED_IDLE_MAX_MS == 3000
    p.tick(2999)
    assert p.ep.port_baud(0) == 500000
    p.tick(1)
    assert p.ep.port_baud(0) == 115200


def test_try_times_out_and_a_late_commit_is_wrong_state():
    p = Port()
    sid = opened(p)
    p.send(0, PS, ps(0, 750000, TRY, 1000), sid)
    p.tick(999)
    assert p.ep.port_baud(0) == 750000
    p.tick(1)
    assert p.ep.port_baud(0) == 115200
    assert p.send(0, PS, ps(0, 750000, COMMIT), sid).detail == m.UNAVAILABLE


def test_a_broken_candidate_while_trying_reverts_at_once():
    p = Port()
    sid = opened(p)
    p.send(0, PS, ps(0, 230400, TRY, 5000), sid)
    p.noise()
    assert p.ep.speed_state == "base" and p.ep.speed_log == [(0, 230400), (0, 115200)]


def test_committed_reverts_when_idle_or_at_three_broken_candidates_in_a_second():
    p = Port()
    sid = opened(p)
    p.send(0, PS, ps(0, 500000, TRY), sid)
    p.send(0, PS, ps(0, 500000, COMMIT, 0, 1500), sid)
    p.tick(1000)
    p.send(0, m.OP_LOCK_STATE)                                                  # a good frame restarts the count
    p.tick(1000)
    assert p.ep.port_baud(0) == 500000
    p.tick(600)
    assert p.ep.port_baud(0) == 115200
    p.send(0, PS, ps(0, 500000, TRY), sid)
    p.send(0, PS, ps(0, 500000, COMMIT), sid)
    p.noise()
    p.tick(300)
    p.noise()
    p.tick(800)
    p.noise()                                                                   # the first is 1.1 s old
    assert p.ep.port_baud(0) == 500000
    p.tick(100)
    p.noise()
    assert p.ep.port_baud(0) == 115200


def test_step_2_and_the_sessions_end_answer_at_the_speed_then_revert():
    p = Port()
    sid = opened(p)
    p.send(0, PS, ps(0, 500000, TRY), sid)
    p.send(0, PS, ps(0, 500000, COMMIT), sid)
    p.ep.broken_rates[115200] = endpoint.BrokenRate()    # an answer sent at the boot speed would come out broken
    r = p.send(0, PS, ps(0, 0, REVERT), sid)
    assert r is not None and r.succeeded and m.Reader(r.payload).u32() == 115200   # it went out at 500000
    assert p.ep.port_baud(0) == 115200
    del p.ep.broken_rates[115200]
    p.send(0, PS, ps(0, 500000, TRY), sid)
    p.send(0, PS, ps(0, 500000, COMMIT), sid)
    p.ep.broken_rates[115200] = endpoint.BrokenRate()
    r = p.send(0, m.OP_END, b"", sid)
    assert r is not None and r.succeeded and p.ep.port_baud(0) == 115200
    del p.ep.broken_rates[115200]
    sid = opened(p, 6, 1000)                                                     # a lapse reverts too
    p.send(0, PS, ps(0, 500000, TRY), sid)
    p.send(0, PS, ps(0, 500000, COMMIT), sid)
    p.tick(1100)
    assert p.ep.holder is None and p.ep.port_baud(0) == 115200


def test_only_the_uart_bridge_the_request_came_in_on_and_rates_it_can_make():
    p = Port()
    sid = opened(p)
    r = p.send(0, PS, ps(1, 500000, TRY), sid)                                  # not the port it came in on
    assert r.detail == m.UNAVAILABLE and r.payload[:3] == bytes([0x01, 1, 6])
    r = p.send(0, PS, ps(0, 9_000_000, TRY), sid)
    assert r.detail == m.UNSUPPORTED and r.payload[:1] == b"\x00"
    r = p.send(0, PS, ps(0, 500000, 3), sid)
    assert r.detail == m.UNSUPPORTED
    q = Port(fake.p4_x035)                                                      # USB-Serial/JTAG: no UART bridge
    q.ep.port_speed_base = 115200
    sid = opened(q)
    assert q.send(0, PS, ps(0, 500000, TRY), sid).detail == m.UNAVAILABLE


# ---- the client: raise_speed ---------------------------------------------------------------------------------------------

def in_process(profile=fake.esp32_v003, lease=10000):
    start = time.monotonic()
    ep = endpoint.Endpoint(profile(), lambda: int((time.monotonic() - start) * 1000))
    stream = fake_serial.FakeSerialStream(ep, 0)
    lk = link.SerialLink.on_stream(stream, "cobs", 0.5)
    lk.transport = "serial"
    hst = h.Host(lk.send)
    lk.attach_host(hst)
    core.take(hst, lease)
    return ep, hst, lk


FAST = dict(verify_bytes=2048, verify_s=0.3, verify_ms=900, duplex_s=0.2, duplex_frames=16)


def test_commit_at_a_good_rate_with_the_report():
    ep, hst, lk = in_process()
    report = link.raise_speed(hst, [1500000], **FAST)
    assert report.supported and report.chosen == report.rate == 1500000 and report.base == 115200
    t, = report.trials
    assert t.committed and t.actual == 1500000 and t.broken_in == t.broken_out == 0
    assert t.in_bytes > 0 and t.out_bytes > 0 and t.in_kb_s > 0 and t.out_kb_s > 0
    assert report.in_kb_s == t.in_kb_s and report.out_kb_s == t.out_kb_s
    assert lk.speed is report and lk.baud == 1500000 and ep.speed_state == "committed"
    hst.keepalive()                                                              # the session goes on at the new rate
    assert "committed" in report.to_text()


def test_a_broken_rate_reverts_and_the_next_one_is_committed():
    ep, hst, lk = in_process()
    ep.broken_rates[230400] = endpoint.BrokenRate(min_size=40)   # a small confirm passes, full frames break
    ep.broken_rates[1000000] = endpoint.BrokenRate(to_probe=False)   # probe -> host only: the probe sees nothing wrong
    report = link.raise_speed(hst, [230400, 1000000, 9_000_000, 500000], **FAST)
    a, b, c, d = report.trials
    assert not a.committed and a.why == "frames broke" and a.broken_in > 0
    assert not b.committed and b.why == "no confirm at the new rate"   # its answers to the confirm never arrive
    assert c.why.startswith("unsupported")
    assert d.committed and report.chosen == 500000 and lk.baud == 500000
    assert ep.speed_state == "committed" and ep.port_baud(0) == 500000
    assert (0, 230400) in ep.speed_log and (0, 1000000) in ep.speed_log


def test_the_sessions_end_takes_the_link_back_to_the_boot_speed():
    ep, hst, lk = in_process()
    link.raise_speed(hst, [750000], **FAST)
    assert lk.baud == 750000
    hst.end()
    assert ep.port_baud(0) == 115200 and lk.baud == 115200 and lk.speed.rate == 115200 and lk.speed.chosen is None
    hst.confirm()


def test_a_revert_seen_as_a_timeout_goes_back_to_the_boot_speed():
    ep, hst, lk = in_process()
    link.raise_speed(hst, [750000], idle_ms=200, **FAST)
    time.sleep(0.35)                                       # the probe reverts by itself (idle_ms)
    ep.tick()
    assert ep.port_baud(0) == 115200 and lk.baud == 750000
    hst.keepalive()                                        # times out at 750000, then once more at the boot speed
    assert lk.baud == 115200 and lk.speed_lost == 1 and lk.speed.lost and lk.speed.rate == 115200


def test_raise_speed_commits_the_idle_maximum_by_default():
    ep, hst, lk = in_process()
    link.raise_speed(hst, [750000], **FAST)
    assert ep.speed_idle_ms == link.IDLE_MAX_MS == 3000
    link.raise_speed(hst, [500000], idle_ms=0, **FAST)          # 0: the maximum too
    assert ep.speed_idle_ms == 3000


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


def test_a_rate_that_breaks_only_both_ways_at_once_fails_the_duplex_phase():
    ep, hst, lk = in_process()
    ep.broken_rates[921600] = endpoint.BrokenRate(min_size=40, duplex=True)   # each way alone passes
    report = link.raise_speed(hst, [921600, 500000], **FAST)
    a, b = report.trials
    assert not a.committed and a.why == "broke both ways at once" and a.broken_duplex > 0
    assert a.broken_in == a.broken_out == 0 and a.in_kb_s > 0 and a.out_kb_s > 0
    assert b.committed and b.duplex_kb_s > 0 and b.duplex_bytes > 0 and report.duplex_kb_s == b.duplex_kb_s
    assert report.chosen == 500000 and ep.port_baud(0) == 500000
    text = report.to_text()
    assert "duplex KB/s" in text and "broke both ways at once" in text
    q = endpoint.BrokenRate(min_size=40, duplex=True, to_probe=False)        # the answer breaks instead
    ep2, hst2, lk2 = in_process()
    ep2.broken_rates[921600] = q
    report = link.raise_speed(hst2, [921600], **FAST)
    assert report.trials[0].why == "broke both ways at once" and report.chosen is None and lk2.baud == 115200
    hst2.keepalive()


def raised_in_use(lease=10000):
    ep, hst, lk = in_process(lease=lease)
    report = link.raise_speed(hst, [921600], **FAST)
    assert report.chosen == 921600
    return ep, hst, lk, report


def test_in_use_three_broken_frames_within_5_s_step_down_for_the_session():
    ep, hst, lk, report = raised_in_use()
    ep.broken_rates[921600] = endpoint.BrokenRate(to_probe=False, every=4)   # answers only: the probe sees nothing
    for _ in range(16):
        hst.request(0, m.OP_LOCK_STATE)                                      # every one answered (a resend at once)
    assert lk.baud == 115200 and ep.port_baud(0) == 115200
    assert report.stepped_down and "3 broken frames" in report.down_why and report.chosen is None
    assert report.rate == 115200 and "stepped down" in report.to_text()
    assert ep.holder is not None and ep.holder == hst.session                # the lease held throughout
    hst.keepalive()
    again = link.raise_speed(hst, [921600], **FAST)                          # not again in this session
    assert again.trials[0].why.startswith("stepped down") and again.chosen is None and lk.baud == 115200
    hst.end()
    core.take(hst, 10000)                                                    # a new session may try it again
    del ep.broken_rates[921600]
    assert link.raise_speed(hst, [921600], **FAST).chosen == 921600


def test_in_use_broken_requests_step_down_too_and_the_request_goes_on_at_base():
    ep, hst, lk, report = raised_in_use()
    ep.broken_rates[921600] = endpoint.BrokenRate(to_host=False, every=3)    # the probe sees broken candidates
    for _ in range(12):
        hst.request(0, m.OP_LOCK_STATE)
    assert lk.baud == 115200 and ep.port_baud(0) == 115200 and report.stepped_down
    assert ep.holder == hst.session
    hst.keepalive()


def test_in_use_a_single_broken_frame_does_not_step_down():
    ep, hst, lk, report = raised_in_use()
    ep.broken_rates[921600] = endpoint.BrokenRate(to_probe=False, every=4)
    for _ in range(4):
        hst.request(0, m.OP_LOCK_STATE)
    del ep.broken_rates[921600]
    for _ in range(10):
        hst.request(0, m.OP_LOCK_STATE)
    assert lk.baud == 921600 and ep.port_baud(0) == 921600 and not report.stepped_down and lk.retries == 1
    time.sleep(0.1)
    ep.broken_rates[921600] = endpoint.BrokenRate(to_probe=False, every=4)   # two more, but the first is old enough
    lk.strikes = [time.monotonic() - link.STRIKE_WINDOW_S]
    for _ in range(8):
        hst.request(0, m.OP_LOCK_STATE)
    assert lk.baud == 921600 and not report.stepped_down


def test_no_answer_at_a_raised_rate_falls_back_well_inside_the_lease():
    ep, hst, lk, report = raised_in_use(lease=3000)
    lk.timeout = 3.0                                                         # the default: 2 x 3 s would pass the lease
    ep._speed_revert()                                                       # the probe went back by itself, silently
    t0 = time.monotonic()
    r = hst.request(0, m.OP_LOCK_STATE)                                      # sent again at the boot speed
    took = time.monotonic() - t0
    assert r.succeeded and took < 2.0 and lk.baud == 115200
    assert report.lost and report.stepped_down and "no answer" in report.down_why
    assert ep.holder == hst.session
    again = link.raise_speed(hst, [921600], **FAST)
    assert again.trials[0].why.startswith("stepped down") and lk.baud == 115200


def second_host(ep):
    lk = link.SerialLink.on_stream(fake_serial.FakeSerialStream(ep, 0), "cobs", 0.5)
    lk.transport = "serial"
    hst = h.Host(lk.send)
    t0 = time.monotonic()
    lk.attach_host(hst)
    return hst, lk, time.monotonic() - t0


def test_open_waits_out_a_raised_rate_a_host_that_died_left():
    ep, hst, lk = in_process()
    link.raise_speed(hst, [750000], **FAST)                     # this host dies here, its lease long
    hst2, lk2, took = second_host(ep)                           # the confirms at 115200 are broken candidates: reverts
    assert ep.port_baud(0) == 115200 and lk2.baud == 115200 and took < link.OPEN_RETRY_S
    hst2.confirm()


def test_open_waits_for_the_idle_limit_when_broken_candidates_do_not_revert():
    ep, hst, lk = in_process()
    link.raise_speed(hst, [750000], **FAST)
    heard = ep.speed_frame
    ep.speed_frame = lambda port, good: heard(port, good) if good else None   # only the idle limit takes it back
    hst2, lk2, took = second_host(ep)
    assert ep.port_baud(0) == 115200 and 2.5 < took < link.OPEN_RETRY_S + 1.5
    ep2, hst3, lk3 = in_process()
    link.raise_speed(hst3, [750000], **FAST)
    ep2._speed_revert = lambda: None                            # never comes back: the open gives up
    lk4 = link.SerialLink.on_stream(fake_serial.FakeSerialStream(ep2, 0), "cobs", 0.5)
    lk4.corr_source = lambda: 7
    with pytest.raises(TimeoutError):
        lk4.wait_boot_speed(0.6)


def test_an_off_probe_is_not_supported_and_stays_at_the_boot_speed(monkeypatch):
    ep, hst, lk = in_process()
    ep.port_speed_base = None
    hst._describes.clear()
    report = link.raise_speed(hst, [1500000], **FAST)
    assert not report.supported and "does not declare" in report.why and report.rate == 115200 and lk.baud == 115200
    monkeypatch.setattr(link, "_speed_port", lambda hst: (0, ""))   # declared, but the op is unknown
    report = link.raise_speed(hst, [1500000], **FAST)
    assert not report.supported and "unknown_operation" in report.why and not report.trials
    assert "not supported" in report.to_text() and lk.baud == 115200
    hst.keepalive()


def test_not_a_serial_port_of_its_own():
    ep, hst, lk = in_process()
    lk.transport = "tcp"
    report = link.raise_speed(hst, [1500000])
    assert not report.supported and "serial port" in report.why


@pytest.mark.skipif(sys.platform != "linux", reason="a pty")
def test_open_host_with_port_speed_on_a_pty():
    proc = subprocess.Popen([sys.executable, "-m", "oep_client.fake_serve", "--pty", "--profile", "esp32-v003",
                             "--broken-rate", "230400"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        where = proc.stdout.readline().split()
        hst = link.open_host(where[1], timeout=1.0, port_speed=[230400, 500000])
        report = hst.link.speed
        assert [t.committed for t in report.trials] == [False, True] and report.chosen == 500000
        assert hst.link.stream.baudrate == 500000
        hst.keepalive()
        hst.end()
        assert hst.link.stream.baudrate == 115200
        hst.confirm()
        hst.link.close()
    finally:
        proc.stdin.close()
        proc.wait(5)


@pytest.mark.skipif(sys.platform != "linux", reason="a pty")
def test_oep_speed_cli_prints_the_report(capsys):
    from oep_client import __main__ as cli
    proc = subprocess.Popen([sys.executable, "-m", "oep_client.fake_serve", "--pty", "--profile", "esp32-v003",
                             "--broken-rate", "230400:40:in"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        where = proc.stdout.readline().split()
        assert cli.main(["speed", where[1], "230400,750000", "--verify-bytes", "2048", "--verify-s", "0.3"]) == 0
        out = capsys.readouterr().out
        assert "frames broke" in out and "committed" in out and "in force: 750000 (raised)" in out
    finally:
        proc.stdin.close()
        proc.wait(5)
