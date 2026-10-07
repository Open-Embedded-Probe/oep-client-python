"""The wifi item (oep-spec probe.config §1.4, §3.3; host guide §15.1), `oep config wifi`, TCP discovery (transports §3,
host guide §4.1: `discovery`, `oep find`, tcp://HOST and tcp:UNIT_ID), linktest's TCP wait and KeptSession at exit."""

import re
import socket
import struct
import subprocess
import sys

import pytest

from oep_client import (__main__ as cli, config, core, discovery, endpoint, host as h, kept_session, link, linktest,
                        message as m, virtual_bench)

PASS = "correct horse 1"


class Clock:
    t = 0

    def __call__(self):
        return self.t


def open_v003():
    clock = Clock()
    ep = endpoint.Endpoint(virtual_bench.esp32_v003(), clock)
    hst = h.Host(lambda b: ep.handle(b, 0))
    hst.open(3000)
    return ep, hst, clock, config.ProbeConfig(hst)


def rejected(fn) -> m.Result:
    with pytest.raises(h.Rejected) as e:
        fn()
    return e.value.result


# ---- the item --------------------------------------------------------------------------------------------------------

def test_the_passphrase_is_write_only_and_get_sent_back_keeps_it():
    ep, hst, clock, cfg = open_v003()
    decl = cfg.describe()
    assert config.ITEM["wifi"] in decl.items and decl.wifi_max == 4
    h0 = cfg.get()[0]
    h1 = cfg.set([config.Wifi(index=0, ssid="lab", passphrase=PASS), config.Wifi(index=1, ssid="cafe")])
    raw = b"".join(v for _, v in cfg.get()[1])
    assert PASS.encode() not in raw and h1 != h0
    items = cfg.items()
    assert items == [config.Wifi(index=0, ssid="lab", passphrase=config.KEEP), config.Wifi(index=1, ssid="cafe")]
    assert PASS not in repr(items) and "passphrase=set" in repr(items[0]) and "passphrase=none" in repr(items[1])
    assert cfg.set(items) == h1                                           # get's items sent back: nothing changes
    assert ep.wifi_entries()[0] == (0, b"lab", PASS.encode())             # the probe kept it
    assert cfg.set([config.Wifi(index=0, ssid="lab2", passphrase=config.KEEP)]) != h1   # ssid only: passphrase kept
    assert ep.wifi_entries()[0] == (0, b"lab2", PASS.encode())
    h2 = cfg.get()[0]
    assert cfg.set([config.Wifi(index=0, ssid="lab2", passphrase="another one")]) != h2   # a new passphrase moves it
    # same_items compares without the passphrase (host guide §15.1)
    assert config.same_items(cfg.items(), [config.Wifi(index=0, ssid="lab2", passphrase="whatever!"),
                                           config.Wifi(index=1, ssid="cafe")])
    assert not config.same_items(cfg.items(), [config.Wifi(index=0, ssid="lab2"), config.Wifi(index=1, ssid="cafe")])
    assert cfg.unset([("wifi", 0), ("wifi", 1)]) and cfg.items() == []


def test_refusals():
    ep, hst, clock, cfg = open_v003()
    assert rejected(lambda: cfg.set([config.Wifi(index=1, ssid="x", passphrase=config.KEEP)])).detail == m.MALFORMED
    r = rejected(lambda: cfg.set([config.Wifi(index=4, ssid="x")]))
    assert r.detail == m.UNSUPPORTED and r.payload == bytes([config.ITEM["wifi"]])        # index past wifi_max
    r = rejected(lambda: cfg.set([m.tlv(config.ITEM["wifi"], bytes([0, 2]) + b"a\x00" + b"\x00", critical=True)]))
    assert r.detail == m.UNSUPPORTED and r.payload == bytes([config.ITEM["wifi"] | 0x80])  # the tag as received
    for value in (bytes([0, 0, 0]), bytes([0, 1]) + b"a" + bytes([7]) + b"1234567",
                  bytes([0, 1]) + b"a" + bytes([8]) + b"1234567\x7f", bytes([0, 1]) + b"a" + bytes([64]) + b"g" * 64):
        assert rejected(lambda: cfg.set([m.tlv(config.ITEM["wifi"], value)])).detail == m.MALFORMED
    cfg.set([config.Wifi(index=0, ssid="psk", passphrase="0123456789abcdef" * 4)])       # 64 hex digits: a key
    with pytest.raises(ValueError) as e:
        config.Wifi(index=0, ssid="x", passphrase="short").value()
    assert "short" not in str(e.value)
    with pytest.raises(ValueError):
        config.Wifi(index=0, ssid="x" * 33).value()
    # a probe without the wifi item: unsupported, no wifi TLV in state
    ep2 = endpoint.Endpoint(virtual_bench.p4_bench(), Clock())
    hst2 = h.Host(lambda b: ep2.handle(b, 1))
    hst2.open(3000)
    cfg2 = config.ProbeConfig(hst2)
    assert cfg2.describe().wifi_max == 0 and cfg2.state().wifi is None
    assert rejected(lambda: cfg2.set([config.Wifi(index=0, ssid="x")])).detail == m.UNSUPPORTED


def test_the_state_follows_a_simulated_link():
    ep, hst, clock, cfg = open_v003()
    assert cfg.state().wifi == config.WifiState("off", None, "none", None, None)
    ep.wifi_set_air({"lab": PASS, "open-net": None})
    cfg.set([config.Wifi(index=0, ssid="far away", passphrase=PASS), config.Wifi(index=1, ssid="lab", passphrase=PASS)])
    assert cfg.state().wifi == config.WifiState("connecting", 0, "none", None, None)
    clock.t += ep.wifi_join_ms
    st = cfg.state().wifi
    assert st == config.WifiState("connected", 1, "none", -55, "127.0.0.1") and "ip 127.0.0.1" in st.text()
    # a change to another entry keeps the link; the entry in use changing drops it (probe.config §1.4)
    cfg.set([config.Wifi(index=2, ssid="open-net")])
    assert cfg.state().wifi.entry == 1
    cfg.set([config.Wifi(index=1, ssid="lab", passphrase="wrong pass")])
    assert cfg.state().wifi.state == "connecting"
    clock.t += ep.wifi_join_ms
    assert cfg.state().wifi == config.WifiState("connected", 2, "none", -55, "127.0.0.1")   # 0 not found, 1 auth
    cfg.unset([("wifi", 2)])
    clock.t += ep.wifi_join_ms
    assert cfg.state().wifi == config.WifiState("waiting", None, "auth", None, None)
    cfg.unset([("wifi", 0), ("wifi", 1)])
    assert cfg.state().wifi.state == "off"


def test_saved_entries_come_back_after_a_reboot_with_their_passphrase():
    ep, hst, clock, cfg = open_v003()
    ep.wifi_set_air({"lab": PASS})
    cfg.set([config.Wifi(index=0, ssid="lab", passphrase=PASS)])
    cfg.save()
    assert not cfg.needs_save()
    ep.reboot()
    hst = h.Host(lambda b: ep.handle(b, 0))
    hst.open(3000)
    cfg = config.ProbeConfig(hst)
    assert not cfg.needs_save() and cfg.state().storage == "applied"
    clock.t += ep.wifi_join_ms
    assert cfg.state().wifi.state == "connected" and ep.wifi_entries() == [(0, b"lab", PASS.encode())]


def test_apply_sends_a_passphrase_only_for_an_entry_that_changes():
    ep, hst, clock, cfg = open_v003()
    lab = config.Wifi(index=0, ssid="lab", passphrase=PASS)
    assert cfg.apply([lab]) and not cfg.apply([lab])                    # the same ssid and presence: nothing sent
    n = len(ep.requests)
    assert cfg.apply([lab, config.Label(channel=4, text="x")])         # something else differs: lab goes as 0xFF
    sent = b"".join(r.payload for r in ep.requests[n:] if r.op == config.ProbeConfig.SET)
    assert PASS.encode() not in sent and m.tlv(config.ITEM["wifi"], bytes([0, 3]) + b"lab\xff") in sent


def test_from_env_reads_indexes_and_never_needs_printing():
    env = {"OEP_WIFI_SSID_0": "lab", "OEP_WIFI_PASS_0": PASS, "OEP_WIFI_SSID_2": "open", "OEP_WIFI_SSID_9": "far"}
    got = config.wifi_from_env(env, count=4)
    assert got == [config.Wifi(index=0, ssid="lab", passphrase=PASS), config.Wifi(index=2, ssid="open")]
    assert PASS not in repr(got)


# ---- the command -----------------------------------------------------------------------------------------------------

@pytest.fixture
def v003_cli(monkeypatch):
    ep, hst, clock, cfg = open_v003()

    class FakeLink:
        def close(self):
            pass
    hst.link = FakeLink()
    hst.end()
    monkeypatch.setattr(cli.link, "open_host", lambda target: hst)
    ep.wifi_set_air({"lab": PASS})
    return ep, hst, clock


def test_oep_config_wifi_never_prints_the_passphrase(v003_cli, capsys, monkeypatch):
    ep, hst, clock = v003_cli
    monkeypatch.setenv("LAB_PASS", PASS)
    assert cli.main(["config", "wifi", "x", "--index", "0", "--ssid", "lab", "--pass-env", "LAB_PASS"]) == 0
    with pytest.raises(SystemExit):                                       # entry 1 does not exist: say how to give one
        cli.main(["config", "wifi", "x", "--index", "1", "--ssid", "x"])
    import getpass
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": "prompted pass")
    assert cli.main(["config", "wifi", "x", "--index", "1", "--ssid", "cafe", "--pass-prompt"]) == 0
    assert cli.main(["config", "wifi", "x", "--index", "1", "--ssid", "cafe2"]) == 0      # keeps the passphrase
    assert ep.wifi_entries()[1] == (1, b"cafe2", b"prompted pass")
    assert cli.main(["config", "wifi", "x", "--index", "3", "--ssid", "free", "--open", "--save"]) == 0
    clock.t += ep.wifi_join_ms
    assert cli.main(["config", "show", "x"]) == 0
    assert cli.main(["config", "state", "x"]) == 0
    assert cli.main(["config", "show", "x", "--json"]) == 0
    assert cli.main(["config", "state", "x", "--json"]) == 0
    out = capsys.readouterr().out
    assert PASS not in out and "prompted pass" not in out
    assert "wifi (up to 4): connected, entry 0, rssi -55 dBm, ip 127.0.0.1" in out
    assert "0 'lab': passphrase set  <- in use" in out and "3 'free': passphrase none" in out
    assert "wifi: connected, entry 0, rssi -55 dBm, ip 127.0.0.1" in out and '"passphrase": "set"' in out
    assert cli.main(["config", "wifi-unset", "x", "--index", "1", "--index", "3"]) == 0
    assert [e[0] for e in ep.wifi_entries()] == [0]


def test_oep_config_wifi_from_env_sends_only_what_differs(v003_cli, capsys, monkeypatch):
    ep, hst, clock = v003_cli
    monkeypatch.setenv("OEP_WIFI_SSID_0", "lab")
    monkeypatch.setenv("OEP_WIFI_PASS_0", PASS)
    monkeypatch.setenv("OEP_WIFI_SSID_1", "open")
    assert cli.main(["config", "wifi", "x", "--from-env", "--save"]) == 0
    n = len(ep.requests)
    assert cli.main(["config", "wifi", "x", "--from-env", "--save"]) == 0   # the same: no set, saved already
    assert not any(r.op == config.ProbeConfig.SET for r in ep.requests[n:])
    assert cli.main(["config", "wifi", "x", "--from-env", "--force"]) == 0
    assert any(r.op == config.ProbeConfig.SET for r in ep.requests[n:])
    out = capsys.readouterr().out
    assert PASS not in out and "wifi 0: unchanged" in out and "wifi 0: sent" in out


# ---- discovery -------------------------------------------------------------------------------------------------------

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
    assert discovery.find_unit("FAFE00000003").port == 7451
    with pytest.raises(LookupError):
        discovery.find_unit("0000")


@pytest.fixture
def tcp_bench():
    proc = subprocess.Popen([sys.executable, "-m", "oep_client.virtual_bench_serve", "--tcp", "0", "--framing", "length",
                             "--profile", "esp32-v003", "--wifi-air", f"lab={PASS}", "--wifi-join-ms", "0"],
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
        cfg = config.ProbeConfig(hst)
        core.take(hst, 3000)
        cfg.set([config.Wifi(index=0, ssid="lab", passphrase=PASS)])
        assert cfg.state().wifi.ipv4 == "127.0.0.1"
        hst.end()
    finally:
        hst.link.close()
    monkeypatch.setattr(discovery, "browse", lambda timeout=2.0, engine="auto":
                        [discovery.Found("OEP y", "0000aaaa", "oep-y.local.", port, ["127.0.0.1"])])
    with pytest.raises(link.UnitIdMismatch):                               # TXT says 0000aaaa, describe does not
        link.open_host("tcp:0000aaaa", keep_session=False)
    hst = link.open_host(f"tcp://127.0.0.1:{port}", keep_session=False)   # explicit address and port
    hst.link.close()


def test_oep_find_lists_what_dns_sd_says(monkeypatch, capsys):
    found = [discovery.Found("OEP fafe00000003", "fafe00000003", "oep-fafe00000003.local.", 7450, ["192.168.1.23"])]
    monkeypatch.setattr(discovery, "browse", lambda timeout=2.0, engine="auto": found)
    assert cli.main(["find"]) == 0
    assert cli.main(["find", "--json"]) == 0
    out = capsys.readouterr().out
    assert "fafe00000003  oep-fafe00000003.local.  port 7450  192.168.1.23  tcp://192.168.1.23:7450" in out
    assert '"target": "tcp://192.168.1.23:7450"' in out
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
