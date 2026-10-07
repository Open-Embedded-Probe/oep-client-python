"""virtual_bench_serve --tcp with length framing serves several connections at once (default 3, --tcp-connections N;
one more is accepted and closed at once), each a transport of its own (transports §1: its confirm, revision in use and
notifications), all on one probe (transports §3: the session, the lock and the resend table are shared and outlive a
closed connection). --once ends when no connection is left."""

import select
import socket
import struct
import subprocess
import sys
import time

import pytest

from oep_client import capture as c, core, endpoint, host as h, link, message as m, virtual_bench


def _serve(*argv):
    proc = subprocess.Popen([sys.executable, "-m", "oep_client.virtual_bench_serve", "--tcp", "0", "--framing", "length",
                             *argv], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return proc, int(proc.stdout.readline().decode().split()[1])


@pytest.fixture
def bench():
    proc, port = _serve("--profile", "esp32-v003")
    try:
        yield port
    finally:
        proc.stdin.close()
        proc.wait(5)


def _host(port: int) -> h.Host:
    return link.open_host(f"tcp://127.0.0.1:{port}", keep_session=False)


class Raw:
    """A bare length-framed connection: requests out, every frame in (answers and notifications)."""

    def __init__(self, port: int):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.buf = bytearray()
        self.corr = 0

    def send(self, fn: int, op: int, payload: bytes = b"", session: int = 0) -> int:
        self.corr += 1
        msg = m.Request(self.corr, fn, op, payload, session).pack()
        self.sock.sendall(struct.pack("<H", len(msg)) + msg)
        return self.corr

    def frames(self, seconds: float) -> list[bytes]:
        """Every frame that comes within `seconds`."""
        out, deadline = [], time.monotonic() + seconds
        while (left := deadline - time.monotonic()) > 0:
            if select.select([self.sock], [], [], left)[0]:
                data = self.sock.recv(65536)
                if not data:
                    break
                self.buf += data
            while len(self.buf) >= 2 and len(self.buf) >= 2 + struct.unpack_from("<H", self.buf)[0]:
                n = struct.unpack_from("<H", self.buf)[0]
                out.append(bytes(self.buf[2:2 + n]))
                del self.buf[:2 + n]
        return out

    def result(self, corr: int) -> m.Result:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            for f in self.frames(0.05):
                if f[0] == m.ROLE_RESULT and m.Result.unpack(f).corr == corr:
                    return m.Result.unpack(f)
        raise AssertionError(f"no answer to corr {corr}")

    def closed_by_peer(self, seconds: float = 2.0) -> bool:
        if not select.select([self.sock], [], [], seconds)[0]:
            return False
        try:
            return self.sock.recv(1) == b""
        except ConnectionResetError:
            return True

    def close(self):
        self.sock.close()


# ---- two at once: one lock -----------------------------------------------------------------------------------------

def test_two_clients_at_once_share_one_lock(bench):
    a, b = _host(bench), _host(bench)                                 # both connected before either opens
    assert a.confirm()["transport"] == b.confirm()["transport"]      # one listener: one describe entry (transports §1)
    a.open(5000, owner="alpha")
    with pytest.raises(h.Locked) as e:                                # the other connection: the same probe's lock
        b.open(3000, owner="beta")
    assert e.value.owner == "alpha" and 0 < e.value.remaining_ms <= 5000
    locked, remaining, owner = b.lock_owner()                         # lock_state seen from the other connection
    assert locked and remaining > 0 and owner == "alpha"
    assert a.lock_state()[0]
    a.end()
    assert b.lock_state() == (False, 0) and a.lock_state() == (False, 0)
    b.open(3000, owner="beta")                                        # ended: the other one opens
    assert a.lock_owner()[2] == "beta"
    with pytest.raises(h.Locked):
        a.open(3000)
    b.end()
    a.link.close()
    b.link.close()


# ---- the limit -----------------------------------------------------------------------------------------------------

def test_a_fourth_connection_is_accepted_and_closed_at_once(bench):
    hosts = [_host(bench) for _ in range(3)]                          # three at once (the default)
    for x in hosts:
        assert x.confirm()["revision"] >= 1
    fourth = Raw(bench)
    assert fourth.closed_by_peer()                                    # accepted, then closed at once
    fourth.close()
    for x in hosts:                                                   # the three go on
        assert x.lock_state() == (False, 0)
    hosts[0].link.close()                                             # one leaves: there is room again
    time.sleep(0.1)
    again = _host(bench)
    assert again.lock_state() == (False, 0)
    again.link.close()
    for x in hosts[1:]:
        x.link.close()


def test_tcp_connections_sets_the_limit():
    proc, port = _serve("--tcp-connections", "1")
    try:
        first = _host(port)
        second = Raw(port)
        assert second.closed_by_peer()
        second.close()
        assert first.lock_state() == (False, 0)
        first.link.close()
    finally:
        proc.stdin.close()
        proc.wait(5)


def test_tcp_connections_is_for_the_length_framing():
    run = subprocess.run([sys.executable, "-m", "oep_client.virtual_bench_serve", "--tcp", "0", "--tcp-connections", "2"],
                         stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)
    assert run.returncode == 2 and "--tcp-connections" in run.stderr


# ---- a connection goes, the session stays --------------------------------------------------------------------------

def test_one_client_drops_while_another_keeps_going(bench):
    a, b = _host(bench), _host(bench)
    a.open(5000, owner="alpha")
    sid = a.session
    a.link.close()                                                    # no end: the connection just goes
    for _ in range(5):                                                # the other connection goes on
        assert b.confirm()["revision"] >= 1
        locked, _, owner = b.lock_owner()
        assert locked and owner == "alpha"                            # the session outlives its connection
    with pytest.raises(h.Locked):
        b.open(3000)
    third = _host(bench)                                              # a new connection takes the session back
    r = third.request(m.CORE_FN, m.OP_OPEN, struct.pack("<IB", 3000, 0), session=sid)
    assert r.succeeded
    third.session = sid
    third.keepalive()
    third.end()
    b.open(3000)                                                      # free now: the one that stayed opens
    b.end()
    b.link.close()
    third.link.close()


def test_a_session_resumed_from_a_new_connection_while_the_old_one_is_open(bench):
    a = _host(bench)
    a.open(5000, owner="alpha")
    sid = a.session
    b = _host(bench)                                                  # a is still connected
    r = b.request(m.CORE_FN, m.OP_OPEN, struct.pack("<IB", 5000, 0), session=sid)
    assert r.succeeded                                                # the same id from another connection: taken back
    b.session = sid
    b.keepalive()
    assert a.lock_owner()[:1] == (True,)
    b.end()
    assert a.lock_state() == (False, 0)
    a.link.close()
    b.link.close()


# ---- notifications go to the subscriber's connection ---------------------------------------------------------------

def test_notifications_go_only_to_the_subscribers_connection_and_move_with_the_session():
    proc, port = _serve()                                             # p4-x035: oep.fixture.logic
    try:
        a = _host(port)
        watcher = Raw(port)                                           # a second connection, in no session
        core.take(a, 5000, owner="alpha")
        sid = a.session
        lc = c.LogicCapture(a)
        core.plan_apply(a, [(lc.fn, 0, 20), (lc.fn, 1, 21)])
        lc.configure(rate=100_000, mode=c.STREAMING)
        lc.subscribe()
        lc.start()
        got = lc.stream(a.link, nbytes=1000, seconds=3)               # the subscriber's connection gets the data
        assert len(got.data) >= 1000
        corr = watcher.send(m.CORE_FN, m.OP_LOCK_STATE)
        frames = watcher.frames(0.5)
        assert [f[0] for f in frames] == [m.ROLE_RESULT] and m.Result.unpack(frames[0]).corr == corr   # nothing else

        taker = Raw(port)                                             # the session taken back on a third connection
        corr = taker.send(m.CORE_FN, m.OP_OPEN, struct.pack("<IB", 5000, 0), session=sid)
        assert taker.result(corr).resolution == m.COMPLETED
        pushed = [f for f in taker.frames(0.5) if f[0] in (m.ROLE_DATA, m.ROLE_EVENT)]
        assert pushed and all(struct.unpack_from("<H", f, 1)[0] == lc.fn for f in pushed)   # the data moved there
        assert all(f[0] == m.ROLE_RESULT for f in watcher.frames(0.2))
        corr = taker.send(m.CORE_FN, m.OP_END, session=sid)
        assert taker.result(corr).resolution == m.COMPLETED
        for x in (watcher, taker):
            x.close()
        a.link.close()
    finally:
        proc.stdin.close()
        proc.wait(5)


def test_the_endpoint_routes_pushes_by_connection():
    now = [0]
    ep = endpoint.Endpoint(virtual_bench.p4_x035(), lambda: now[0])
    first = h.Host(lambda b: ep.handle(b, 0, link="first"))
    second = h.Host(lambda b: ep.handle(b, 0, link="second"))
    first.open(3000)
    lc = c.LogicCapture(first)
    core.plan_apply(first, [(lc.fn, 0, 20)])
    lc.configure(rate=100_000, mode=c.STREAMING)
    lc.subscribe()
    lc.start()
    now[0] += 50
    assert {to for to, _ in ep.pushes_to()} == {"first"}             # where the subscribe came
    assert set(ep.revision_in_use) == {"first"}                      # each connection's own confirm
    second.confirm()
    assert set(ep.revision_in_use) == {"first", "second"}
    assert second.request(m.CORE_FN, m.OP_OPEN, struct.pack("<IB", 3000, 0), session=first.session).succeeded
    now[0] += 50
    assert {to for to, _ in ep.pushes_to()} == {"second"}            # taken back: they go there now


# ---- --once --------------------------------------------------------------------------------------------------------

def test_once_ends_when_the_last_connection_closes():
    proc, port = _serve("--once")
    try:
        a, b = _host(port), _host(port)
        a.link.close()
        time.sleep(0.3)
        assert proc.poll() is None                                    # b is still connected
        assert b.lock_state() == (False, 0)
        b.link.close()
        proc.wait(5)                                                  # none left: the program ends
        assert proc.returncode == 0
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(5)


# ---- stdin commands with several connections ---------------------------------------------------------------------

def test_reboot_and_wifi_air_on_stdin_reach_every_connection():
    from test_virtual_bench_serve_reboot import _command
    proc, port = _serve("--profile", "esp32-v003")
    try:
        a, b = _host(port), _host(port)
        a.open(5000)
        boot = a.confirm()["boot_id"]
        line = _command(proc, "reboot")
        assert line.startswith("virtual_bench_serve: rebooted, boot_id 0x")
        new = int(line.rsplit("0x", 1)[1], 16)
        assert new != boot
        assert a.confirm()["boot_id"] == new == b.confirm()["boot_id"]   # both connections stay open
        assert b.lock_state() == (False, 0)                               # the lock went with the reboot
        assert _command(proc, "wifi-air lab=secret") == "virtual_bench_serve: wifi air now 1 network(s)"
        assert _command(proc, "lose") == "virtual_bench_serve: lost connection(s) none"
        assert a.lock_state() == (False, 0)
        a.link.close()
        b.link.close()
    finally:
        proc.stdin.close()
        proc.wait(5)
