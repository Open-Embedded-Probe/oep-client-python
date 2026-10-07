"""virtual_bench_serve --announce (virtual_bench_mdns): the virtual bench announces its TCP port by DNS-SD `_oep._tcp`
over mDNS (oep-spec transports §3), and this machine's own query finds it - the way another host's CI tests its
discovery with the querier and the responder on one host. Each responder (minimal; python-zeroconf when the mdns extra
is installed) against each browser (`oep find`, discovery.find_unit, tcp:UNIT_ID; both engines), plus the raw legacy
unicast and multicast answers. Skipped where multicast does not loop back on this host."""

import json
import secrets
import select
import socket
import struct
import subprocess
import sys
import time

import pytest

from oep_client import __main__ as cli, discovery, link, virtual_bench_mdns as mdns

ENGINES = ["minimal", pytest.param("zeroconf", marks=pytest.mark.skipif(not discovery.have_zeroconf(),
                                                                        reason="python-zeroconf not installed"))]


def _no_multicast() -> str | None:
    """Why multicast cannot be tested here (None: it can): a datagram to the mDNS group, on a port of its own, must
    come back to a socket joined to the group on this host, and port 5353 must be shareable."""
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        rx.bind(("", 0))
        rx.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                      socket.inet_aton(discovery.MDNS_GROUP) + socket.inet_aton("0.0.0.0"))
        tx.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
        token = secrets.token_bytes(8)
        tx.sendto(token, (discovery.MDNS_GROUP, rx.getsockname()[1]))
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            if select.select([rx], [], [], max(0.0, deadline - time.monotonic()))[0] and rx.recv(64) == token:
                break
        else:
            return "multicast does not loop back on this host"
    except OSError as e:
        return f"no IPv4 multicast here ({e})"
    finally:
        rx.close()
        tx.close()
    try:
        mdns.MinimalResponder(mdns.Announcement("probe", 1, ["127.0.0.1"])).sock.close()   # (no goodbye: a probe)
    except OSError as e:
        return str(e)
    return None


@pytest.fixture(scope="module")
def multicast():
    why = _no_multicast()
    if why:
        pytest.skip(why)


@pytest.fixture(scope="module", params=ENGINES)
def bench(request, multicast):
    """A virtual bench announcing itself (the responder engine the param names) under a unit_id of its own."""
    unit = secrets.token_hex(6)
    proc = subprocess.Popen([sys.executable, "-m", "oep_client.virtual_bench_serve", "--tcp", "0", "--announce",
                             "--announce-engine", request.param, "--profile", "esp32-v003", "--unit-id", unit],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    line = proc.stdout.readline()
    if not line.startswith("PORT "):
        proc.stdin.close()
        err = proc.stderr.read()
        proc.wait(5)
        pytest.fail(f"virtual_bench_serve --announce did not start: {err}")
    try:
        yield request.param, unit, int(line.split()[1])
    finally:
        proc.stdin.close()
        proc.wait(10)


@pytest.mark.parametrize("engine", ENGINES)
def test_find_unit_finds_the_announcing_virtual_bench(bench, engine):
    _, unit, port = bench
    f = discovery.find_unit(unit, timeout=3.0, engine=engine)
    assert (f.unit_id, f.port, f.target) == (unit, port, f"tcp://127.0.0.1:{port}")
    assert f.host == f"oep-virtual-{unit}-{port}.local." and f.instance == f"OEP virtual {unit} {port}"
    assert discovery.port_of(f.host, timeout=3.0, engine=engine) == port


@pytest.mark.parametrize("engine", ENGINES)
def test_oep_find_lists_the_announcing_virtual_bench(bench, engine, capsys):
    _, unit, port = bench
    assert cli.main(["find", "--engine", engine, "--timeout", "3", "--json"]) == 0
    mine = [f for f in json.loads(capsys.readouterr().out) if f["unit_id"] == unit]
    assert len(mine) == 1 and mine[0]["port"] == port and mine[0]["target"] == f"tcp://127.0.0.1:{port}"


def test_tcp_unit_id_opens_it(bench):
    _, unit, port = bench
    hst = link.open_host(f"tcp:{unit}", keep_session=False)        # find_unit, connect, describe's unit_id checked
    try:
        assert hst.link.transport == "tcp"
    finally:
        hst.link.close()


def _query_for_ptr(sock: socket.socket, unicast: bool) -> int:
    qid = secrets.randbits(16)
    sock.sendto(discovery.query([(discovery.SERVICE, discovery.T_PTR)], qid, unicast=unicast),
                (discovery.MDNS_GROUP, discovery.MDNS_PORT))
    return qid


def _ours(packet: bytes, unit: str, port: int) -> bool:
    recs = discovery.records(packet)
    return any(t == discovery.T_PTR and d == f"OEP virtual {unit} {port}.{discovery.SERVICE}" for _, t, d in recs)


def _wait_for(sock: socket.socket, pred, timeout: float = 3.0) -> bytes:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if select.select([sock], [], [], max(0.0, deadline - time.monotonic()))[0]:
            packet = sock.recv(9000)
            if pred(packet):
                return packet
    raise AssertionError("no answer")


def _ttls(packet: bytes) -> list[int]:
    """Every record's TTL (questions skipped)."""
    _, _, qd, an, ns, ar = struct.unpack_from(">HHHHHH", packet)
    at, out = 12, []
    for _ in range(qd):
        at = discovery._read_name(packet, at)[1] + 4
    for _ in range(an + ns + ar):
        at = discovery._read_name(packet, at)[1]
        _, _, ttl, rdlen = struct.unpack_from(">HHIH", packet, at)
        out.append(ttl)
        at += 10 + rdlen
    return out


def test_a_legacy_unicast_query_is_answered_to_its_port(bench):
    """A one-shot query from an ephemeral port (RFC 6762 §6.7): the answer comes to that port, with the query's ID and
    question, TTL at most 10 - what ch32rv's discovery sends."""
    responder, unit, port = bench
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        s.bind(("", 0))
        qid = _query_for_ptr(s, unicast=False)
        packet = _wait_for(s, lambda p: _ours(p, unit, port))
        assert struct.unpack_from(">HHH", packet) [0] == qid and struct.unpack_from(">H", packet, 4)[0] == 1
        rtypes = {t for _, t, _ in discovery.records(packet)}
        assert {discovery.T_PTR, discovery.T_SRV, discovery.T_TXT, discovery.T_A} <= rtypes
        assert all(t <= 10 for t in _ttls(packet)) or responder == "zeroconf"
    finally:
        s.close()


def test_a_multicast_query_from_5353_is_answered_on_the_group(bench):
    """A full mDNS querier (port 5353, no QU bit): the answer comes on the group."""
    _, unit, port = bench
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        s.bind(("", discovery.MDNS_PORT))
        s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                     socket.inet_aton(discovery.MDNS_GROUP) + socket.inet_aton("0.0.0.0"))
        _query_for_ptr(s, unicast=False)
        packet = _wait_for(s, lambda p: _ours(p, unit, port))
        txt = [d for _, t, d in discovery.records(packet) if t == discovery.T_TXT]
        assert {"unit_id": unit} in txt
    finally:
        s.close()


# ---- the records, without a network ----------------------------------------------------------------------------------

def test_the_records_and_the_two_answer_forms():
    ann = mdns.Announcement("fafe00000003", 7450, ["192.168.1.23", "127.0.0.1"])
    q = discovery.query([(discovery.SERVICE, discovery.T_PTR)], 0x1234, unicast=True)
    qid, questions, end = mdns.parse_query(q)
    assert qid == 0x1234 and questions == [(discovery.SERVICE, discovery.T_PTR, True)] and end == len(q)
    answers, extra = ann.answer(questions)
    legacy = mdns.response(answers, extra, qid, q[12:end], 1, legacy=True)
    multi = mdns.response(answers, extra)
    for packet in (legacy, multi):
        b = discovery.Browser()
        b.feed(packet)
        assert [(f.unit_id, f.port, f.host, f.addresses) for f in b.found()] == \
            [("fafe00000003", 7450, "oep-virtual-fafe00000003-7450.local.", ["192.168.1.23", "127.0.0.1"])]
    assert struct.unpack_from(">HH", legacy) == (0x1234, 0x8400) and set(_ttls(legacy)) == {10}
    assert struct.unpack_from(">HHH", multi) == (0, 0x8400, 0) and set(_ttls(multi)) == {120}
    assert mdns.parse_query(multi) is None                                   # a response is no query
    assert ann.answer([("_other._tcp.local.", discovery.T_PTR, False)]) == ([], [])
    assert ann.answer([(ann.host.upper(), discovery.T_A, False)])[0] == ann.a()          # names: case ignored
    meta = ann.answer([(mdns.META, discovery.T_PTR, False)])[0]
    assert meta == [ann.meta()]
    assert mdns.reachable_addresses("127.0.0.1", ["127.0.0.1", "10.0.0.2"]) == ["127.0.0.1"]
    assert mdns.reachable_addresses("0.0.0.0", ["127.0.0.1", "10.0.0.2"]) == ["10.0.0.2", "127.0.0.1"]


def test_announce_options_are_checked():
    from oep_client import virtual_bench_serve as vbs
    assert vbs.parse(["--tcp", "0", "--announce"]).framing == "length"         # announcing: length frames
    assert vbs.parse(["--tcp", "0"]).framing == "length"                         # TCP: length frames by default
    assert vbs.parse(["--tcp", "0", "--framing", "cobs"]).framing == "cobs"    # a serial port over the socket, asked for
    assert vbs.parse([]).framing == "cobs"                                     # the pty
    for argv in (["--announce"], ["--tcp", "0", "--announce", "--framing", "cobs"], ["--tcp", "0", "--unit-id", "UP"],
                 ["--tcp", "0", "--announce", "--announce-on", "eth0"]):
        with pytest.raises(SystemExit):
            vbs.parse(argv)
    ep = vbs.build(vbs.parse(["--tcp", "0", "--profile", "esp32-v003", "--unit-id", "0123456789ab"]))
    from oep_client import virtual_bench
    assert virtual_bench.unit_id_of(ep.probe) == "0123456789ab"


# ---- the minimal query goes out of every interface ------------------------------------------------------------------

class _RecordingSocket:
    def __init__(self, refuse=()):
        self.refuse, self.ifs, self.sent = set(refuse), [], []

    def setsockopt(self, level, opt, value):
        if opt == socket.IP_MULTICAST_IF:
            self.ifs.append(socket.inet_ntoa(value))

    def sendto(self, packet, where):
        if self.ifs[-1] in self.refuse:
            raise OSError("no route")
        self.sent.append((self.ifs[-1], where))


def test_send_query_goes_out_of_each_interface():
    s = _RecordingSocket(refuse={"10.0.0.9"})
    group = (discovery.MDNS_GROUP, discovery.MDNS_PORT)
    assert discovery.send_query(s, b"q", ["192.168.1.5", "10.0.0.9", "127.0.0.1"]) == 2
    assert s.sent == [("192.168.1.5", group), ("127.0.0.1", group)]      # one refusing interface skipped
    s = _RecordingSocket(refuse={"10.0.0.9"})
    assert discovery.send_query(s, b"q", ["10.0.0.9"]) == 1 and s.sent == [("0.0.0.0", group)]   # none: default route
    ifs = discovery.interface_addresses()
    assert "127.0.0.1" in ifs and all(socket.inet_aton(ip) for ip in ifs) and len(set(ifs)) == len(ifs)


@pytest.fixture(scope="module")
def loopback_bench(multicast):
    """A virtual bench answering on the loopback interface only - a probe on an adapter the default route does not
    take (ch32rv's Windows case: the query left by WSL's vEthernet, the probe was on the Wi-Fi adapter)."""
    unit = secrets.token_hex(6)
    proc = subprocess.Popen([sys.executable, "-m", "oep_client.virtual_bench_serve", "--tcp", "0", "--announce",
                             "--announce-engine", "minimal", "--announce-on", "127.0.0.1", "--profile", "esp32-v003",
                             "--unit-id", unit], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True)
    line = proc.stdout.readline()
    if not line.startswith("PORT "):
        proc.stdin.close()
        err = proc.stderr.read()
        proc.wait(5)
        pytest.skip(f"no multicast on loopback here: {err.strip()}")
    try:
        yield unit, int(line.split()[1])
    finally:
        proc.stdin.close()
        proc.wait(10)


def test_the_minimal_query_reaches_a_probe_on_an_interface_off_the_default_route(loopback_bench, monkeypatch):
    unit, port = loopback_bench
    ifs = discovery.interface_addresses()
    assert discovery.find_unit(unit, timeout=2.0, engine="minimal").port == port     # every interface: found
    assert "127.0.0.1" in ifs
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect((discovery.MDNS_GROUP, discovery.MDNS_PORT))
        default = probe.getsockname()[0]                       # where a multicast sent once leaves
    except OSError:
        default = "0.0.0.0"
    finally:
        probe.close()
    if default not in ("0.0.0.0",) and not default.startswith("127."):
        monkeypatch.setattr(discovery, "interface_addresses", lambda: [default])   # the old one send: not found
        assert all(f.unit_id != unit for f in discovery.browse(1.5, engine="minimal"))


# ---- verifying what is found (host guide §4.1: `oep` is not a registered service name) ------------------------------

class _NotOep:
    """A TCP service that is no OEP probe: it answers whatever comes with an HTTP error line and closes."""

    def __init__(self):
        import threading
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(8)
        self.port = self.srv.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            try:
                c.settimeout(2.0)
                c.recv(512)
                c.sendall(b"HTTP/1.0 400 Bad Request\r\n\r\n")
            except OSError:
                pass
            finally:
                c.close()

    def close(self):
        self.srv.close()


@pytest.fixture(scope="module")
def impostors(bench):
    """Three more `_oep._tcp` instances on this host (minimal responders, in-process): a non-OEP service under a
    unit_id of its own, one under the bench's unit_id, and the bench's own port under a TXT unit_id that describe
    does not say."""
    import threading
    _, unit, port = bench
    other = _NotOep()
    alien, liar = secrets.token_hex(6), secrets.token_hex(6)
    anns = [mdns.Announcement(alien, other.port, ["127.0.0.1"], instance=f"Not OEP {alien}"),
            mdns.Announcement(unit, other.port, ["127.0.0.1"], instance=f"Not OEP {unit}"),
            mdns.Announcement(liar, port, ["127.0.0.1"], instance=f"Wrong TXT {liar}")]
    responders = [mdns.MinimalResponder(a) for a in anns]
    stop = threading.Event()

    def serve():
        while not stop.is_set():
            socks = [s for r in responders for s in r.watch()]
            readable = select.select(socks, [], [], 0.05)[0]
            for r in responders:
                r.poll(readable)

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    try:
        yield {"alien": alien, "liar": liar, "other_port": other.port}
    finally:
        stop.set()
        t.join(2)
        for r in responders:
            r.close()
        other.close()


@pytest.mark.parametrize("engine", ENGINES)
def test_oep_find_drops_what_it_cannot_verify(bench, impostors, engine, capsys):
    _, unit, port = bench
    assert cli.main(["find", "--engine", engine, "--timeout", "3", "--json"]) == 0
    out, err = capsys.readouterr()
    listed = {(f["unit_id"], f["port"]) for f in json.loads(out)}
    assert (unit, port) in listed
    assert not listed & {(impostors["alien"], impostors["other_port"]), (unit, impostors["other_port"]),
                         (impostors["liar"], port)}
    where = f"tcp://127.0.0.1:{impostors['other_port']}"
    assert f"dropped {where} (Not OEP {impostors['alien']}" in err and "no valid confirm answer" in err
    assert f"dropped tcp://127.0.0.1:{port} (Wrong TXT {impostors['liar']}" in err and repr(unit) in err
    assert cli.main(["find", "--engine", engine, "--timeout", "3", "--json", "--no-verify"]) == 0
    raw = {(f["unit_id"], f["port"]) for f in json.loads(capsys.readouterr().out)}
    assert {(unit, port), (impostors["alien"], impostors["other_port"]), (unit, impostors["other_port"]),
            (impostors["liar"], port)} <= raw


def test_find_unit_and_tcp_unit_id_pass_over_an_impostor_with_the_same_txt(bench, impostors):
    _, unit, port = bench
    for _ in range(3):                                   # whichever instance the browse lists first
        assert discovery.find_unit(unit, timeout=2.0).port == port
    hst = link.open_host(f"tcp:{unit}", keep_session=False)
    try:
        assert hst.link.transport == "tcp"
    finally:
        hst.link.close()
    with pytest.raises(LookupError, match="no announced instance with unit_id .* is verified"):
        discovery.find_unit(impostors["alien"], timeout=2.0)


def test_check_says_why(bench, impostors):
    _, unit, port = bench
    assert discovery.check(discovery.Found("b", unit, "h", port, ["127.0.0.1"])) is None
    assert "no valid confirm answer" in discovery.check(
        discovery.Found("x", unit, "h", impostors["other_port"], ["127.0.0.1"]))
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    closed = s.getsockname()[1]
    s.close()
    assert "no TCP connection" in discovery.check(discovery.Found("c", unit, "h", closed, ["127.0.0.1"]))
    assert "no unit_id" in discovery.check(discovery.Found("n", None, "h", port, ["127.0.0.1"]))
    ok, dropped = discovery.verify([discovery.Found("c", unit, "h", closed, ["127.0.0.1"]),
                                    discovery.Found("b", unit, "h", port, ["127.0.0.1"])])
    assert [f.instance for f in ok] == ["b"] and [f.instance for f, _ in dropped] == ["c"]
