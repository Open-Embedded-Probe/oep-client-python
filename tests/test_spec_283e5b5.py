"""oep-spec 283e5b5 (af3d52b, 4f6d464) and f8bb2de against the virtual bench and the client:

- transports §3: a closed transport does not end the session - the session, its lock, subscriptions and the resend table
  stay until the lease runs out; a new connection's open with the same id takes it back (core §6.2)
  (virtual_bench_serve over TCP);
- host guide §5: a host that may run again keeps its session id per probe (kept_session) and its next run ends the
  session the previous one left (open that id, end at once) before it opens its own;
- core §7.5 / §1.2: a probe with channels declares `channels`, channel numbers are 0 .. channels - 1, absent = none.
"""

import os
import pathlib
import struct
import subprocess
import sys
import time

import pytest

from oep_client import core, endpoint, host as h, kept_session, link, message as m, virtual_bench


class Clock:
    def __init__(self):
        self.ms = 0

    def __call__(self):
        return self.ms


def _serve(*argv):
    proc = subprocess.Popen([sys.executable, "-m", "oep_client.virtual_bench_serve", *argv], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return proc, proc.stdout.readline().decode().split()


@pytest.fixture
def tcp_bench():
    proc, (_, port) = _serve("--tcp", "0", "--framing", "length", "--profile", "esp32-v003")
    try:
        yield f"tcp://127.0.0.1:{port}"
    finally:
        proc.stdin.close()
        proc.wait(5)


def _raw_host(target: str) -> h.Host:
    """A host on a fresh TCP connection, keeping nothing (keep_session off)."""
    return link.open_host(target, keep_session=False)


# ---- transports §3: the session outlives its connection ------------------------------------------------------------

def test_a_dropped_tcp_client_keeps_its_session_and_a_new_connection_takes_it_back(tcp_bench):
    first = _raw_host(tcp_bench)
    first.open(5000, owner="first")
    sid = first.session
    first.link.close()                                               # no end: the connection just goes
    time.sleep(0.2)

    other = _raw_host(tcp_bench)
    with pytest.raises(h.Locked) as e:                               # the session still holds the lock
        other.open(3000, owner="other")
    assert e.value.owner == "first" and e.value.remaining_ms > 3000
    other.link.close()

    again = _raw_host(tcp_bench)
    r = again.request(m.CORE_FN, m.OP_OPEN, struct.pack("<IB", 3000, 0), session=sid)   # the same id: taken back
    assert r.succeeded
    again.session = sid
    again.keepalive()                                                # requests of that session run
    again.end()
    again.link.close()
    other = _raw_host(tcp_bench)
    other.open(3000)                                                 # ended: the lock is free
    other.end()
    other.link.close()


def test_a_dropped_tcp_client_lapses_only_with_its_lease(tcp_bench):
    first = _raw_host(tcp_bench)
    first.open(1000)
    first.link.close()
    time.sleep(1.5)                                                  # the lease (1000 ms) ran out: core §9
    other = _raw_host(tcp_bench)
    other.open(3000)
    other.end()
    other.link.close()


# ---- host guide §5: the kept session id ------------------------------------------------------------------------------

def test_a_host_run_again_ends_the_session_its_previous_run_left(tcp_bench):
    first = link.open_host(tcp_bench)                                # keep_session on (the default)
    assert isinstance(first.kept, kept_session.KeptSession)
    core.take(first, 30000, owner="oep run 1", wait_s=0)
    sid = first.session
    path = pathlib.Path(os.environ["OEP_SESSION_DIR"]) / "fafe00000003.session"
    assert path.read_text() == f"{sid:08x}\n" and first.kept.path == path
    first.link.close()                                               # killed: no end; the file keeps the id

    without = _raw_host(tcp_bench)                                   # a host that keeps nothing meets the old lock
    with pytest.raises(h.InUse):
        core.take(without, 3000, owner="other", wait_s=0)
    without.link.close()

    second = link.open_host(tcp_bench)
    core.take(second, 30000, owner="oep run 2", wait_s=0)            # at once: the old session was ended first
    assert second.kept.previous == sid and second.kept.ended_previous
    assert second.session != sid and path.read_text() == f"{second.session:08x}\n"
    second.end()
    assert path.read_text() == ""                                    # ended: nothing to take back next time
    second.link.close()

    third = link.open_host(tcp_bench)
    core.take(third, 3000, wait_s=0)
    assert third.kept.previous is None and not third.kept.ended_previous
    third.end()
    third.link.close()


def test_a_kept_file_another_running_host_holds_is_left_alone():
    """Two hosts of this user on one probe at once (here in one process, each with its own lock on the file): the
    second finds the file held and leaves it - the running host's session is its own."""
    ep = endpoint.Endpoint(virtual_bench.esp32_v003(), Clock())
    first, second = h.Host(lambda x: ep.handle(x)), h.Host(lambda x: ep.handle(x))
    first.kept, second.kept = kept_session.KeptSession(), kept_session.KeptSession()
    first.open(30000, owner="running")
    with pytest.raises(h.Locked):
        second.open(3000)
    assert second.kept.fd is None and "another running host" in second.kept.why_not
    assert second.kept.previous is None
    first.keepalive()                                                # untouched
    first.end()
    first.kept.release()


def test_an_x_unit_id_keeps_nothing():
    ep = endpoint.Endpoint(virtual_bench.with_unit_id(virtual_bench.esp32_v003(), "x-test"), Clock())
    hst = h.Host(lambda b: ep.handle(b))
    hst.kept = kept_session.KeptSession()
    hst.open(3000)
    assert hst.kept.fd is None and "names no unit" in hst.kept.why_not
    hst.end()


def test_end_previous_does_not_touch_a_session_of_another_holder():
    ep = endpoint.Endpoint(virtual_bench.esp32_v003(), Clock())
    a, b = h.Host(lambda x: ep.handle(x)), h.Host(lambda x: ep.handle(x))
    a.open(3000)
    assert b.end_previous(0x12345678) is False                       # another session holds the lock: locked
    a.keepalive()
    assert b.end_previous(a.session) is True                         # the holder's id: taken back and ended
    with pytest.raises(h.NoSession):
        a.keepalive()


def test_default_dir_order(monkeypatch, tmp_path):
    monkeypatch.setenv(kept_session.ENV, str(tmp_path / "x"))
    assert kept_session.default_dir() == tmp_path / "x"
    monkeypatch.delenv(kept_session.ENV)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    assert kept_session.default_dir() == tmp_path / "run" / "oep-client"
    monkeypatch.delenv("XDG_RUNTIME_DIR")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    if sys.platform != "win32":
        assert kept_session.default_dir() == tmp_path / "cache" / "oep-client" / "sessions"


def test_the_cli_keeps_its_session(tcp_bench):
    """`oep` commands open through link.open_host: a session one left behind is ended by the next."""
    hst = link.open_host(tcp_bench)
    core.take(hst, 30000, owner="oep config", wait_s=0)
    hst.link.close()                                                 # the command was killed
    out = subprocess.run([sys.executable, "-m", "oep_client", "config", "label", tcp_bench, "5", "x.y"],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0 and "set: hash" in out.stdout, out.stdout + out.stderr


# ---- core §7.5 / §1.2: channels ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("profile", sorted(virtual_bench.PROFILES))
def test_every_profile_declares_channels_and_numbers_below_it(profile):
    ep = endpoint.Endpoint(virtual_bench.PROFILES[profile](), Clock())
    assert any(tag == virtual_bench.CORE_CHANNELS for tag, _ in ep.decl[0])
    assert ep.channels > 0 and max(ep._all_channels()) < ep.channels, profile


def test_a_probe_without_channels_has_none():
    probe = virtual_bench.esp32_v003()
    probe.offered = [virtual_bench._with(o, (t for t in o.tlvs if t[0] != virtual_bench.CORE_CHANNELS))
                     if o.fn == virtual_bench.CORE_FN else o for o in probe.offered]
    ep = endpoint.Endpoint(probe, Clock())
    assert ep.channels == 0 and ep._boot_channels() == set()
