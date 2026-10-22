"""TCP discovery (transports §3, host guide §4.1: `discovery`, `oep find`, tcp://HOST and tcp:UNIT_ID), linktest's TCP
wait and KeptSession at exit. (The network settings are the core's, transports §7.)"""

import json
import re
import socket
import struct
import subprocess
import sys

import pytest

from oep_client import (__main__ as cli, core, settings, discovery, endpoint, host as h, kept_session, link, linktest,
                        message as m, virtual_bench)

def _rr(name: bytes, rtype: int, rdata: bytes) -> bytes:
    return name + struct.pack(">HHIH", rtype, 0x8001, 120, len(rdata)) + rdata


def announcement(unit_id="fafe00000003", port=7450, ip="192.168.1.23") -> bytes:
    """A response as the reference probe's mDNS sends one: PTR in the answers, SRV / TXT / A in the additional section,
    names compressed after their first appearance."""
    svc = discovery.encode_name("_oep._tcp.local")
    head = struct.pack(">HHHHHH", 0, 0x8400, 0, 1, 0, 3)
    inst_at = 12 + len(svc) + 10
    inst = bytes([len(f"OEP {unit_id}")]) + f"OEP {unit_id}".encode() + b"\xc0\x0c"   # instance . (pointer to svc)
    ptr = _rr(svc, discovery.T_PTR, inst)
    ptr_inst = struct.pack(">H", 0xC000 | inst_at)
    host = discovery.encode_name(f"oep-{unit_id}.local")
    srv = _rr(ptr_inst, discovery.T_SRV, struct.pack(">HHH", 0, 0, port) + host)
    txt_entry = f"unit_id={unit_id}".encode()
    txt = _rr(ptr_inst, discovery.T_TXT, bytes([len(txt_entry)]) + txt_entry + b"\x09extra=yes")
    a = _rr(host, discovery.T_A, socket.inet_aton(ip))
    return head + ptr + srv + txt + a


def test_a_response_is_read_into_unit_id_host_port_address():
    b = discovery.Browser()
    assert b.questions() == [(discovery.SERVICE, discovery.T_PTR)]
    b.feed(announcement())
    (f,) = b.found()
    assert (f.unit_id, f.host, f.port, f.addresses) == ("fafe00000003", "oep-fafe00000003.local.", 7450, ["192.168.1.23"])
    assert f.instance == "OEP fafe00000003" and f.txt["extra"] == "yes" and f.target == "tcp://192.168.1.23:7450"
    assert b.questions() == [(discovery.SERVICE, discovery.T_PTR)]          # nothing missing
    q = discovery.query([(discovery.SERVICE, discovery.T_PTR)], 7)
    assert q[:12] == struct.pack(">HHHHHH", 7, 0, 1, 0, 0, 0) and q.endswith(struct.pack(">HH", 12, 0x8001))
    assert discovery.records(q) == []                                     # a query is no answer
    assert discovery.records(announcement()[:40]) == []                   # cut short: nothing half-read


def test_missing_records_are_asked_for():
    b = discovery.Browser()
    b.instances.add("OEP x._oep._tcp.local.")
    assert (("OEP x._oep._tcp.local.", discovery.T_SRV) in b.questions()
            and ("OEP x._oep._tcp.local.", discovery.T_TXT) in b.questions())
    b.srv["oep x._oep._tcp.local."] = ("oep-x.local.", 7450)
    assert ("oep-x.local.", discovery.T_A) in b.questions()
    (f,) = b.found()
    assert f.unit_id is None and f.target == "tcp://oep-x.local:7450"


def test_tcp_targets_without_a_port_and_by_unit_id(monkeypatch):
    found = [discovery.Found("OEP fafe00000003", "fafe00000003", "oep-fafe00000003.local.", 7451, ["127.0.0.1"])]
    monkeypatch.setattr(discovery, "browse", lambda timeout=2.0, engine="auto": found)
    assert link.tcp_address("tcp://oep-fafe00000003.local") == ("oep-fafe00000003.local", 7451)
    assert link.tcp_address("tcp://oep-fafe00000003") == ("oep-fafe00000003", 7451)
    assert link.tcp_address("tcp://127.0.0.1") == ("127.0.0.1", 7451)
    assert link.tcp_address("tcp://10.0.0.5:99") == ("10.0.0.5", 99)
    assert link.tcp_address("tcp://[::1]:7450") == ("::1", 7450)
    with pytest.raises(LookupError, match="tcp://10.0.0.9:PORT"):
        link.tcp_address("tcp://10.0.0.9")                                # no port is fixed (transports §3)
    assert discovery.find_unit("FAFE00000003", verified=False).port == 7451
    with pytest.raises(LookupError, match=r"is verified as that OEP probe .*tcp://127.0.0.1:7451 .*no TCP connection"):
        discovery.find_unit("FAFE00000003")                                # nothing listens there (host guide §4.1)
    with pytest.raises(LookupError):
        discovery.find_unit("0000")


@pytest.fixture
def tcp_bench():
    proc = subprocess.Popen([sys.executable, "-m", "oep_client.virtual_bench_serve", "--tcp", "0", "--framing", "length",
                             "--profile", "esp32-v003"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    port = int(re.search(r"PORT (\d+)", proc.stdout.readline()).group(1))
    yield port
    proc.stdin.close()
    proc.wait(5)


def test_tcp_unit_id_opens_the_probe_dns_sd_names_and_checks_describe(tcp_bench, monkeypatch):
    port = tcp_bench
    unit = "fafe00000003"                                                  # the esp32-v003 profile's unit_id
    monkeypatch.setattr(discovery, "browse", lambda timeout=2.0, engine="auto":
                        [discovery.Found("OEP x", unit, "oep-x.local.", port, ["127.0.0.1"])])
    hst = link.open_host(f"tcp:{unit}", keep_session=False)
    try:
        assert hst.link.transport == "tcp" and linktest.default_timeout(hst) == linktest.TCP_TIMEOUT
        cfg = settings.Settings(hst)
        core.take(hst, 3000)
        assert cfg.set_named(settings.CORE_CATEGORY, {"channel.GPIO24.label": "nrst"}) == 2   # a session over the found link
        hst.end()
    finally:
        hst.link.close()
    monkeypatch.setattr(discovery, "browse", lambda timeout=2.0, engine="auto":
                        [discovery.Found("OEP y", "0000aaaa", "oep-y.local.", port, ["127.0.0.1"])])
    with pytest.raises(LookupError, match="says unit_id 'fafe00000003'"):  # TXT says 0000aaaa, describe does not:
        link.open_host("tcp:0000aaaa", keep_session=False)                 # dropped by find_unit's verify
    monkeypatch.setattr(discovery, "find_unit", lambda unit_id, *a, **k:   # and open_host checks it on its own link
                        discovery.Found("OEP y", "0000aaaa", "oep-y.local.", port, ["127.0.0.1"]))
    with pytest.raises(link.UnitIdMismatch):
        link.open_host("tcp:0000aaaa", keep_session=False)
    hst = link.open_host(f"tcp://127.0.0.1:{port}", keep_session=False)   # explicit address and port
    hst.link.close()


def test_oep_find_lists_what_dns_sd_says(monkeypatch, capsys):
    found = [discovery.Found("OEP fafe00000003", "fafe00000003", "oep-fafe00000003.local.", 7450, ["192.168.1.23"])]
    monkeypatch.setattr(discovery, "browse", lambda timeout=2.0, engine="auto": found)
    assert cli.main(["find", "--no-verify"]) == 0
    assert cli.main(["find", "--json", "--no-verify"]) == 0
    out = capsys.readouterr().out
    assert "fafe00000003  oep-fafe00000003.local.  port 7450  192.168.1.23  tcp://192.168.1.23:7450" in out
    assert '"target": "tcp://192.168.1.23:7450"' in out
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    closed = s.getsockname()[1]
    s.close()
    found[:] = [discovery.Found("OEP fafe00000003", "fafe00000003", "oep-fafe00000003.local.", closed, ["127.0.0.1"])]
    assert cli.main(["find"]) == 1                                         # verified by default: nothing listens there
    err = capsys.readouterr().err
    assert f"warning: dropped tcp://127.0.0.1:{closed} (OEP fafe00000003, TXT unit_id fafe00000003): not verified" in err
    assert "no TCP connection" in err and "no verified probe: 1 instance(s)" in err
    assert cli.main(["find", "--json"]) == 0 and json.loads(capsys.readouterr().out) == []
    monkeypatch.setattr(discovery, "browse", lambda timeout=2.0, engine="auto": [])
    assert cli.main(["find", "--timeout", "0.1"]) == 1


def test_linktest_waits_longer_on_tcp_only():
    class L:
        transport = "serial"
    hst = type("H", (), {"link": L()})()
    assert linktest.default_timeout(hst) == linktest.SERIAL_TIMEOUT == 0.3
    L.transport = "tcp"
    assert 2.0 <= linktest.default_timeout(hst) <= 5.0
    assert cli.main.__module__ and "--timeout" in subprocess.run(
        [sys.executable, "-m", "oep_client", "linktest", "--help"], capture_output=True, text=True).stdout


# ---- KeptSession at interpreter exit ---------------------------------------------------------------------------------

def test_kept_session_del_raises_nothing_when_imports_fail(tmp_path, monkeypatch):
    import builtins
    k = kept_session.KeptSession(tmp_path)
    fd = __import__("os").open(tmp_path / "u.session", __import__("os").O_RDWR | __import__("os").O_CREAT)
    assert kept_session._lock(fd)
    k.fd = fd

    def no_import(*a, **kw):
        raise ImportError("sys.meta_path is None, Python is likely shutting down")
    monkeypatch.setattr(builtins, "__import__", no_import)
    k.__del__()                                                            # unlocks with the module's own fcntl
    monkeypatch.undo()
    assert k.fd is None


def test_kept_session_at_interpreter_exit_prints_nothing(tmp_path):
    code = ("import os, sys; from oep_client import kept_session as ks\n"
            f"k = ks.KeptSession({str(tmp_path)!r}); fd = os.open({str(tmp_path / 'u.session')!r}, os.O_RDWR | os.O_CREAT)\n"
            "ks._lock(fd); k.fd = fd; k.cycle = k\n")                     # a cycle: collected late at exit
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0 and "Exception ignored" not in r.stderr and "ImportError" not in r.stderr
