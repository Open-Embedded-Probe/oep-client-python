"""oep_client.pytest_plugin: the probe's lock name, the port's record, the RP2040 upload (no probe, no
pytest-embedded-arduino-cli: the hooks are called as functions)."""
import json
import warnings

import pytest

from oep_client import pytest_plugin as pp


class Config:
    """The parts of pytest.Config the plugin reads: the ini owner and the lock-name hook."""

    def __init__(self, owner="io.github.example.lib"):
        self.owner = owner
        outer = self

        class Hook:
            @staticmethod
            def pytest_arduino_cli_device_lock_key(config, port):
                return pp.pytest_arduino_cli_device_lock_key(outer, port)
        self.hook = Hook()

    def getini(self, name):
        assert name == pp.OWNER_INI
        return self.owner


@pytest.mark.parametrize("port, key", [
    ("oep://30EDA0E31108/x035c8t6-s3", "probe:30eda0e31108"),
    ("oep://30eda0e31108/rp2040-x035?via=tcp:127.0.0.1:7450", "probe:30eda0e31108"),
    ("oep://30eda0e31108", "probe:30eda0e31108"),
    ("oep://x-1234/a", None),                            # an x- unit_id names no probe (core §7.5)
    ("wchlink://BC5F4B9807F7", None),                    # not this plugin's
    ("/no/such/tty", None),
])
def test_one_lock_name_per_probe(port, key):
    assert pp.pytest_arduino_cli_device_lock_key(None, port) == key


def test_the_env_record_is_used_whole_and_named_by_probe_and_port(monkeypatch):
    monkeypatch.setattr(pp, "_memo_on_probe", lambda unit, port, owner: None)
    monkeypatch.setenv("OEP_PORT_MEMO_30EDA0E31108_X035C8T6_S3", json.dumps({"parts": {"eth": {"to": "lan"}}}))
    assert pp.port_memo_of(Config(), "oep://30eda0e31108/x035c8t6-s3") == {"parts": {"eth": {"to": "lan"}}}
    assert pp.port_memo_of(Config(), "oep://30eda0e31108/x035f8u6-s3") == {}


def test_the_probe_memo_when_no_env(monkeypatch):
    seen = []
    monkeypatch.setattr(pp, "_memo_on_probe", lambda unit, port, owner: seen.append((unit, port, owner)) or {"a": 1})
    assert pp.port_memo_of(Config(), "oep://30EDA0E31108/V208WBU6") == {"a": 1}
    assert seen == [("30eda0e31108", "V208WBU6", "io.github.example.lib")]


def test_no_owner_reads_no_memo(monkeypatch):
    monkeypatch.setattr(pp, "_memo_on_probe", lambda *a: pytest.fail("read without an owner"))
    assert pp.port_memo_of(Config(owner=""), "oep://30eda0e31108/v208wbu6") == {}


def test_env_over_memo_warns_when_they_differ(monkeypatch):
    monkeypatch.setattr(pp, "_memo_on_probe", lambda unit, port, owner: {"parts": {}})
    monkeypatch.setenv("OEP_PORT_MEMO_30EDA0E31108_V208WBU6", '{"parts": {"eth": {"to": "lan"}}}')
    with pytest.warns(UserWarning, match="differs"):
        assert pp.port_memo_of(Config(), "oep://30eda0e31108/v208wbu6") == {"parts": {"eth": {"to": "lan"}}}
    monkeypatch.setattr(pp, "_memo_on_probe", lambda unit, port, owner: {"parts": {"eth": {"to": "lan"}}})
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        pp.port_memo_of(Config(), "oep://30eda0e31108/v208wbu6")


@pytest.mark.parametrize("text, why", [("{", "not JSON"), ("[1]", "not a JSON object")])
def test_a_bad_env_record_stops(monkeypatch, text, why):
    monkeypatch.setenv("OEP_PORT_MEMO_30EDA0E31108_V208WBU6", text)
    with pytest.raises(pytest.UsageError, match=why):
        pp.port_memo_of(Config(), "oep://30eda0e31108/v208wbu6")


def test_a_probe_another_plugin_names_gets_its_env_record(monkeypatch):
    """A WCH-Link (its lock name from -ch32rv) has no memo: its record is the .env one, by its id alone."""
    cfg = Config()
    cfg.hook.pytest_arduino_cli_device_lock_key = lambda config, port: "probe:bc5f4b9807f7"
    monkeypatch.setenv("OEP_PORT_MEMO_BC5F4B9807F7", '{"parts": {"eth": {"to": "lan"}}}')
    assert pp.port_memo_of(cfg, "wchlink://BC5F4B9807F7") == {"parts": {"eth": {"to": "lan"}}}


def test_upload_takes_an_rp2040_on_an_oep_port(monkeypatch, tmp_path):
    written = []
    import oep_client.rp2040_swd as rs
    monkeypatch.setattr(rs, "flash", lambda address, path, log=None: written.append((address, path)))
    sketch = tmp_path / "peer_pico"
    build = tmp_path / "build"
    build.mkdir()
    (build / "peer_pico.ino.elf").write_bytes(b"\x7fELF")
    args = dict(config=None, role="peer:pico", profile="pico", sketch_dir=sketch, build_path=build)
    assert pp.pytest_arduino_cli_upload(port="oep://80f1b2d835d6/rp2040-x035", fqbn="rp2040:rp2040:generic", **args)
    assert written == [("oep://80f1b2d835d6/rp2040-x035", str(build / "peer_pico.ino.elf"))]
    assert pp.pytest_arduino_cli_upload(port="/dev/ttyACM0", fqbn="rp2040:rp2040:generic", **args) is None
    assert pp.pytest_arduino_cli_upload(port="oep://80f1b2d835d6/x035c8t6-pico", fqbn="ch32:ch32rv:x035", **args) is None
