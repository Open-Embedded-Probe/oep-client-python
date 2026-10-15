"""A TCP connection the host closes and opens again (transports §5: its framing broke) leaves the session and its
subscriptions on the probe, still sending where their subscribe came - the closed connection, whose pushes are dropped
and never carried to the new one (core §6.2, §10.3; transports §3). The host subscribes again, with the same
conditions, before its next request, so the pushes come on the new connection (seq from 0)."""

import subprocess
import sys

from oep_client import capture as c, core, link


def _serve():
    proc = subprocess.Popen([sys.executable, "-m", "oep_client.virtual_bench_serve", "--tcp", "0", "--framing", "length"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return proc, int(proc.stdout.readline().decode().split()[1])


def test_the_pushes_come_again_after_the_connection_is_opened_again():
    proc, port = _serve()                                 # p4-x035: oep.fixture.logic
    try:
        hst = link.open_host(f"tcp://127.0.0.1:{port}", keep_session=False)
        core.take(hst, 5000)
        lc = c.LogicCapture(hst)
        core.plan_apply(hst, [(lc.fn, 0, 20), (lc.fn, 1, 21)])
        lc.configure(rate=100_000, mode=c.STREAMING)
        lc.subscribe()
        lc.start()
        assert len(lc.stream(hst.link, nbytes=500, seconds=3).data) >= 500
        hst.link.resync()                                 # the framing broke: TCP is closed and opened again
        assert hst._moved == {lc.fn}
        hst.keepalive()                                   # the next request: the subscribe again goes first
        assert not hst._moved and lc.fn in hst.subscriptions
        assert len(lc.stream(hst.link, nbytes=500, seconds=3).data) >= 500
        hst.end()
        hst.link.close()
    finally:
        proc.stdin.close()
        proc.wait(5)


def test_nothing_is_subscribed_again_without_a_session():
    proc, port = _serve()
    try:
        hst = link.open_host(f"tcp://127.0.0.1:{port}", keep_session=False)
        core.take(hst, 5000)
        lc = c.LogicCapture(hst)
        core.plan_apply(hst, [(lc.fn, 0, 20), (lc.fn, 1, 21)])
        lc.configure(rate=100_000, mode=c.STREAMING)
        lc.subscribe()
        hst.end()                                         # the subscription ended with the lock (core §10.3)
        hst.link.resync()
        assert not hst._moved
        hst.confirm()
        hst.link.close()
    finally:
        proc.stdin.close()
        proc.wait(5)
