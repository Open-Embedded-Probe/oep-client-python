"""oep.probe.restart (oep-spec interfaces/oep-if-restart, op 0x01, an optional interface): the fake answers completed
success with no payload, then restarts once the answer is out (a new boot_id, no session, the saved settings again);
its refusals are those of any op that needs the lock; a probe without the interface does not list it. The client's
Host.restart_probe finds it by name, sends it, waits and confirms the new boot_id - on an in-process serial port, on
fake_serve's pty and TCP."""

import socket
import struct
import subprocess
import sys
import time

import pytest

from oep_client import cobs, config, core, endpoint, fake, fake_serial, host as h, link, message as m, registry as reg

RESTART = reg.PROBE_RESTART.op["restart"]
VENDOR = 1                                       # p4-x035's transport 1: vendor bulk (not a serial port)
FN = 15                                          # p4-x035's oep.probe.restart


class Clock:
    t = 0

    def __call__(self):
        return self.t


def bench(transport: int = VENDOR):
    ep = endpoint.Endpoint(fake.p4_x035(), Clock())
    return ep, h.Host(lambda b: ep.handle(b, transport))


def test_the_registry_and_the_fake_offer_it():
    assert RESTART == core.OP_RESTART == h.OP_RESTART == 0x01 and RESTART not in reg.PROBE_RESTART.lock_free  # the lock
    assert "restart" not in reg.CORE.op                                 # not the core's any more
    assert reg.LIMITS["restart_after_answer_ms"] == 100
    ep, hst = bench()
    assert core.restart_fn(hst) == FN and core.offers(hst, FN, RESTART)
    assert core.ops(hst, 0) == set(reg.CORE.op.values())                # fn 0: the eight mandatory ops only


def test_restart_answers_first_then_the_probe_restarts():
    """completed success, no payload; then a new boot_id, the session gone (no_session), confirm shows the new boot."""
    ep, hst = bench()
    before = hst.confirm()["boot_id"]
    hst.open(3000)
    sid = hst.session
    out = m.Result.unpack(ep.handle(m.Request(hst.next_corr(), FN, RESTART, b"", sid).pack(), VENDOR))
    assert (out.resolution, out.detail, out.payload) == (m.COMPLETED, m.SUCCESS, b"")
    assert ep.reboots == 1 and ep.boot_id != before and ep.holder is None and not ep.restarting
    with pytest.raises(h.NoSession):
        hst.keepalive()                                                 # the old session's id: no_session (§6.2)
    assert hst.confirm()["boot_id"] == ep.boot_id != before


def test_restart_needs_the_lock():
    ep, hst = bench()
    with pytest.raises(h.Rejected, match="session required"):
        hst.request_restart()                                           # session_id 0 (core §4.1, §4.3 order 1)
    other = h.Host(lambda b: ep.handle(b, VENDOR))
    other.open(3000)
    hst.session = 0x01020304
    with pytest.raises(h.Locked):
        hst.request_restart()                                           # another session holds it (§6.2)
    other.end()
    with pytest.raises(h.NoSession):
        hst.request_restart()                                           # the lock free: no_session
    assert ep.reboots == 0


def test_a_probe_without_oep_probe_restart():
    """The interface is optional: a probe without it lists none, the host's restart raises LookupError and sends
    nothing, and a restart sent to its old fn anyway is unknown_function."""
    ep = endpoint.Endpoint(fake.without(fake.p4_x035(), fake.RESTART), Clock())
    hst = h.Host(lambda b: ep.handle(b, VENDOR))
    assert core.find_all(hst, fake.RESTART) == [] and core.restart_max_ms(hst) is None
    hst.open(3000)
    with pytest.raises(LookupError):
        hst.request_restart()
    with pytest.raises(LookupError):
        hst.restart_probe(wait_s=1)
    with pytest.raises(h.Rejected) as e:
        hst.request(FN, RESTART)
    assert e.value.result.detail == m.UNKNOWN_FUNCTION and ep.reboots == 0


def test_a_tlv_after_restart():
    """No fixed part: a non-critical TLV is ignored and listed, a critical one refused unsupported (core §2.3)."""
    ep, hst = bench()
    hst.open(3000)
    with pytest.raises(h.Unsupported) as e:
        hst.request(FN, RESTART, m.tlv(0x3D, b"\x01", critical=True))
    assert e.value.tag == 0x3D | m.TAG_CRITICAL and ep.reboots == 0
    r = hst.request(FN, RESTART, m.tlv(0x3D, b"\x01"))
    assert r.succeeded and r.payload == bytes([m.TAG_IGNORED, 1, 0, 0x3D]) and ep.reboots == 1


def test_the_saved_settings_come_back_the_unsaved_ones_do_not():
    ep, hst = bench()
    hst.open(3000)
    cfg = config.ProbeConfig(hst)
    cfg.set([config.Idle(channel=21, mode="pull-down")])
    cfg.save()
    cfg.set([config.Disable(channel=20)])                               # not saved
    hst.restart_probe(wait_s=1)
    assert ep.disabled == set() and ep.parked[21] == 2
    hst.open(3000)
    assert config.ProbeConfig(hst).items() == [config.Idle(channel=21, mode="pull-down")]


def test_restart_probe_on_a_bare_send():
    ep, hst = bench()
    before = hst.confirm()["boot_id"]
    hst.open(3000)
    epoch = hst.epoch
    after = hst.restart_probe(wait_s=1)
    assert after == ep.boot_id != before and hst.session is None and hst.epoch == epoch + 1
    assert hst.open(3000).boot_id == after


def test_restart_probe_without_a_session():
    ep, hst = bench()
    with pytest.raises(h.Rejected, match="session required"):
        hst.restart_probe(wait_s=1)
    assert ep.reboots == 0


def test_requests_behind_the_restart_are_lost_with_the_old_boot():
    """On a serial port: the answer is queued, the probe restarts after it, and a request that came in the same write
    behind it is never answered (oep-if-restart §2)."""
    ep = endpoint.Endpoint(fake.p4_x035(), Clock())
    port = fake_serial.FakeSerialPort(ep, 0)
    sid = 0x11223344
    port.feed(cobs.frame(m.Request(1, 0, m.OP_OPEN, struct.pack("<IB", 3000, 0), sid).pack()))
    assert m.Result.unpack(cobs.unframe(port.output()[1:-1])).succeeded
    before = ep.boot_id
    port.feed(cobs.frame(m.Request(2, FN, RESTART, b"", sid).pack())
              + cobs.frame(m.Request(3, 0, m.OP_KEEPALIVE, b"", sid).pack()))
    out = port.output()
    assert out.count(0) == 2                                            # one frame: the restart's answer
    r = m.Result.unpack(cobs.unframe(out[1:-1]))
    assert (r.corr, r.resolution, r.payload) == (2, m.COMPLETED, b"")
    assert ep.reboots == 1 and ep.boot_id != before and ep.holder is None


def test_restart_probe_on_an_in_process_serial_port():
    start = time.monotonic()
    ep = endpoint.Endpoint(fake.esp32_v003(), lambda: int((time.monotonic() - start) * 1000))
    lk = link.SerialLink.on_stream(fake_serial.FakeSerialStream(ep, 0), "cobs", 0.5)
    lk.transport = "serial"
    hst = h.Host(lk.send)
    lk.attach_host(hst)
    before = hst.limits["boot_id"]
    hst.open(3000)
    after = hst.restart_probe(wait_s=2)
    assert after == ep.boot_id != before and hst.limits["boot_id"] == after
    assert hst.open(3000).boot_id == after
    hst.keepalive()


def test_restart_probe_when_the_answer_is_lost():
    """The answer is dropped on the line: the link sends the restart once more with the same corr, the restarted probe
    refuses it no_session, and the confirm shows the new boot_id (host guide §5.2 item 7)."""
    start = time.monotonic()
    ep = endpoint.Endpoint(fake.esp32_v003(), lambda: int((time.monotonic() - start) * 1000))
    drop = {}

    def answer_filter(n, result):
        r = m.Result.unpack(result)
        if not drop and ep.restarting:
            drop["corr"] = r.corr
            return None                                                 # the restart's answer, lost once
        return cobs.frame(result)

    lk = link.SerialLink.on_stream(fake_serial.FakeSerialStream(ep, 0, answer_filter=answer_filter), "cobs", 0.3)
    lk.transport = "serial"
    lk.wait_add_s = 0.1
    hst = h.Host(lk.send)
    lk.attach_host(hst)
    before = hst.limits["boot_id"]
    hst.open(3000)
    after = hst.restart_probe(wait_s=2)
    assert drop and after == ep.boot_id != before and ep.reboots == 1
    assert any(r.op == RESTART and r.corr == drop["corr"] for r in ep.requests)   # the resend reached the new boot


def test_not_restarted_when_the_boot_id_stays():
    ep, hst = bench()
    hst.confirm()
    hst.open(3000)
    real = ep.reboot
    ep.reboot = lambda boot_id=None: real(ep.boot_id)                   # a source that repeated its boot_id
    with pytest.raises(h.NotRestarted):
        hst.restart_probe(wait_s=1)



# ---- restart_max_ms (oep-if-restart §1) ---------------------------------------------------------------------------

def test_the_restart_interface_declares_restart_max_ms():
    """oep.probe.restart's describe carries restart_max_ms (tag 0x40, required, at least restart_after_answer_ms);
    fn 0's carries none of it."""
    ep, hst = bench()
    tags = [t & 0x7F for t, _ in core.describe(hst, FN)]
    assert tags.count(reg.PROBE_RESTART.tlv["describe"]["restart_max_ms"]) == 1 == tags.count(fake.RESTART_MAX_MS_TAG)
    assert core.restart_max_ms(hst) == fake.RESTART_MAX_MS == 2000 >= reg.LIMITS["restart_after_answer_ms"]
    assert 0x4F not in [t & 0x7F for t, _ in core.describe(hst)]


class _Reopen:
    """A link that records restart_probe's wait and confirms (Host.restart_probe calls link.reopen_after_restart)."""
    def __init__(self):
        self.waits = []

    def reopen_after_restart(self, hst, wait_s):
        self.waits.append(wait_s)
        return hst.confirm()


def test_restart_probe_waits_restart_max_ms_by_default(monkeypatch):
    ep, hst = bench()
    ep.restart_max_ms = 3500
    hst.link = _Reopen()
    hst.open(3000)
    hst.restart_probe()
    assert hst.link.waits == [3.5]                                      # the describe's value, read before the restart
    hst.open(3000)
    hst.restart_probe(wait_s=0.7)                                       # given: that one
    monkeypatch.setattr(core, "restart_max_ms", lambda hst: None)       # a probe that declares none (not conforming)
    hst.open(3000)
    hst.restart_probe()
    assert hst.link.waits == [3.5, 0.7, h.RESTART_WAIT_S] and h.RESTART_WAIT_S == 10.0


def test_a_probe_that_does_not_come_back_within_restart_max_ms_is_gone():
    """No confirm answered by restart_max_ms after the answer: the host gives up (oep-if-restart §3) - not after 10 s."""
    ep = endpoint.Endpoint(fake.p4_x035(), Clock())
    ep.restart_max_ms = 300

    def send(b):
        if ep.reboots:
            raise TimeoutError("silent")                                # the probe never answers again
        return ep.handle(b, VENDOR)
    hst = h.Host(send)
    hst.open(3000)
    start = time.monotonic()
    with pytest.raises(TimeoutError):
        hst.restart_probe()
    assert 0.3 <= time.monotonic() - start < 2.0 and ep.reboots == 1

# ---- fake_serve: the op over TCP and a pty -----------------------------------------------------------------------

def _serve(*argv):
    proc = subprocess.Popen([sys.executable, "-m", "oep_client.fake_serve", *argv], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    return proc, proc.stdout.readline().decode().split()


def _stop(proc):
    proc.stdin.close()
    proc.wait(timeout=10)


def test_fake_serve_restart_over_tcp_length_framing():
    proc, where = _serve("--tcp", "0", "--framing", "length", "--profile", "p4-x035")
    try:
        hst = link.open_host(f"tcp://127.0.0.1:{where[1]}", timeout=2.0)
        before = hst.limits["boot_id"]
        hst.open(3000)
        after = hst.restart_probe(wait_s=5)                             # closes, reconnects, confirms
        assert after != before
        assert hst.open(3000).boot_id == after
        hst.keepalive()
        hst.link.close()
    finally:
        _stop(proc)


def test_fake_serve_restart_over_tcp_kept_open():
    """The TCP connection stays (the fake does not close it): the session's id is no_session, confirm the new boot_id."""
    proc, where = _serve("--tcp", "0", "--framing", "length", "--profile", "p4-x035")
    try:
        with socket.create_connection(("127.0.0.1", int(where[1])), timeout=5) as s:
            def send(message):
                s.sendall(struct.pack("<H", len(message)) + message)
                head = s.recv(2, socket.MSG_WAITALL)
                return s.recv(struct.unpack("<H", head)[0], socket.MSG_WAITALL)
            hst = h.Host(send)
            before = hst.confirm()["boot_id"]
            hst.open(3000)
            sid = hst.session
            hst.request_restart()
            time.sleep(h.RESTART_AFTER_ANSWER_S)
            hst.session = sid
            with pytest.raises(h.NoSession):
                hst.keepalive()
            assert hst.confirm()["boot_id"] != before
    finally:
        _stop(proc)


def test_fake_serve_without_restart():
    proc, where = _serve("--tcp", "0", "--framing", "length", "--profile", "p4-x035", "--no-restart")
    try:
        hst = link.open_host(f"tcp://127.0.0.1:{where[1]}", timeout=2.0)
        assert fake.RESTART not in [e.name for e in core.list_entries(hst)]
        hst.open(3000)
        with pytest.raises(LookupError):
            hst.request_restart()
        with pytest.raises(h.Rejected) as e:
            hst.request(FN, RESTART)
        assert e.value.result.detail == m.UNKNOWN_FUNCTION
        hst.link.close()
    finally:
        _stop(proc)


@pytest.mark.skipif(sys.platform != "linux", reason="pty as on Linux")
def test_fake_serve_restart_over_a_pty():
    proc, where = _serve("--pty", "--profile", "esp32-v003")
    try:
        hst = link.open_host(where[1], timeout=1.0)
        before = hst.limits["boot_id"]
        hst.open(3000)
        after = hst.restart_probe(wait_s=8)                             # closes the port, opens it again, confirms
        assert after != before
        assert hst.open(3000).boot_id == after
        hst.keepalive()
        hst.end()
        hst.link.close()
    finally:
        _stop(proc)
