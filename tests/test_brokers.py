"""oep_client.brokers: finding a broker by the user's file or OEP_BROKER and joining it (OEP host guide §7.1, §7.2)."""
import json
import os
import subprocess
import sys

import pytest

from oep_client import brokers, core, link


def _private_dir(tmp_path):
    d = tmp_path / "brokers"
    d.mkdir(mode=0o700)
    os.chmod(d, 0o700)
    return d


def _write(d, unit, **doc):
    body = {"unit_id": unit, "endpoints": [{"kind": "tcp", "address": "127.0.0.1:1"}], "share": [brokers.SHARE],
            "pid": 1, "started": "2026-11-13T00:00:00Z", "implementation": {"name": "test", "version": "0"}}
    body.update(doc)
    (d / f"{unit}.json").write_text(json.dumps(body))


def test_the_file_names_the_endpoints_and_the_sharing_extensions(tmp_path):
    d = _private_dir(tmp_path)
    _write(d, "30eda0e31108", endpoints=[{"kind": "unix", "path": "/x"}, {"kind": "tcp", "address": "127.0.0.1:4242"}])
    b = brokers.find("30EDA0E31108", environ={}, base=d)
    assert b.unit_id == "30eda0e31108" and b.share == [brokers.SHARE]
    assert b.tcp() == [("127.0.0.1", 4242)]                      # an unknown kind is skipped


def test_oep_broker_comes_before_the_file(tmp_path):
    d = _private_dir(tmp_path)
    _write(d, "30eda0e31108")
    b = brokers.find("30eda0e31108", environ={brokers.ENV: "10.0.0.2:7000"}, base=d)
    assert b.tcp() == [("10.0.0.2", 7000)]


@pytest.mark.parametrize("case", ["no file", "other unit", "x- unit", "open directory", "not json"])
def test_no_broker(tmp_path, case):
    d = _private_dir(tmp_path)
    unit = "30eda0e31108"
    if case == "other unit":
        _write(d, unit, unit_id="30eda0e31451")
    elif case == "x- unit":
        unit = "x-1234"
        _write(d, unit)
    elif case == "open directory":
        _write(d, unit)
        os.chmod(d, 0o755)
    elif case == "not json":
        (d / f"{unit}.json").write_text("{")
    assert brokers.find(unit, environ={}, base=d) is None


def test_the_user_directory_follows_the_platform(monkeypatch):
    if sys.platform != "linux":
        pytest.skip("Linux's rule")
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    assert str(brokers.directory()) == "/run/user/1000/oep/brokers"
    monkeypatch.delenv("XDG_RUNTIME_DIR")
    monkeypatch.setenv("TMPDIR", "/var/tmp")
    assert str(brokers.directory()) == f"/var/tmp/oep-{os.getuid()}/brokers"


@pytest.fixture
def tcp_probe():
    proc = subprocess.Popen([sys.executable, "-m", "oep_client.virtual_bench_serve", "--tcp", "0", "--framing", "length",
                             "--profile", "esp32-v003"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
    _, port = proc.stdout.readline().decode().split()
    try:
        h = link.open_host(f"tcp://127.0.0.1:{port}", keep_session=False)
        unit = core.unit_id(h)
        h.link.close()
        yield unit, int(port)
    finally:
        proc.stdin.close()
        proc.wait(5)


def test_connect_checks_the_unit_id_in_describe(tcp_probe):
    unit, port = tcp_probe
    good = brokers.Broker(unit, [{"kind": "tcp", "address": f"127.0.0.1:{port}"}], [brokers.SHARE])
    h = brokers.connect(good)
    assert h is not None
    h.link.close()
    reused = brokers.Broker("30eda0e31451", good.endpoints, good.share)   # the port now answers for another probe
    assert brokers.connect(reused) is None


def test_open_host_falls_back_to_the_probe_without_a_broker(monkeypatch):
    opened = []

    class Direct:
        def open(self, lease_ms, owner=None):
            opened.append((lease_ms, owner))

    monkeypatch.setattr(brokers, "find", lambda unit, environ=None: None)
    monkeypatch.setattr(link, "open_host", lambda target: opened.append(target) or Direct())
    brokers.open_host("30eda0e31108", "me", direct="oep://30eda0e31108?via=usb")
    assert opened == ["oep://30eda0e31108?via=usb", (brokers.LEASE_MS, "me")]


def test_open_host_goes_through_the_broker_and_asks_to_share(tcp_probe, monkeypatch):
    from oep_client import host as oh, message as m
    unit, port = tcp_probe
    monkeypatch.setenv(brokers.ENV, f"127.0.0.1:{port}")
    h = link.open_host(f"oep://{unit}")
    try:
        assert h.kept is None and h.open_extensions == (brokers.SHARE,)
        with pytest.raises(oh.Unsupported) as e:                 # the virtual probe is no broker: it refuses the share
            h.open(3000, owner="cli")
        assert e.value.tag == m.TAG_NAMED_EXTENSION | m.TAG_CRITICAL
    finally:
        h.link.close()


def test_open_host_without_a_broker_is_unchanged(monkeypatch):
    monkeypatch.setattr(brokers, "find", lambda unit, environ=None: None)
    seen = []
    monkeypatch.setattr(link, "_open_address", lambda addr, timeout, baud: seen.append(addr) or (_ for _ in ()).throw(
        FileNotFoundError("no probe")))
    with pytest.raises(FileNotFoundError):
        link.open_host("oep://30eda0e31108")
    assert seen and seen[0].probe == "30eda0e31108"
