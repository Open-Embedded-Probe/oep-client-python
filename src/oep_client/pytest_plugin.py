"""OEP for pytest-embedded-arduino-cli: loaded by itself (`pytest11`) wherever oep-client-python is installed with the
`pytest` extra, and quiet unless a test's port is an OEP probe or a test asks for its fixtures.

- **The lock** (`pytest_arduino_cli_device_lock_key`): `probe:<unit_id>` for `oep://<unit_id>/...` (any route) and for an
  OEP probe's own serial port (the project's VID:PID, serial = unit_id). Every port and target behind a probe is one
  lock: the probe has one (core §6.1).
- **The port's record** (`port_memo`, `peer_port_memo(name)`, `port_memo_of(config, port)`): what is wired to the port,
  as the project that owns it writes it (the ini value `oep_memo_owner`). `.env`'s `OEP_PORT_MEMO_<id>` (a probe without
  ports) or `OEP_PORT_MEMO_<id>_<PORT>` (upper case, `-` as `_`) as a whole, else the probe's memo object
  `<owner>/port/<port>` (oep-if-memo §4), else {}. When both are there and differ, a warning. The record's shape is the
  owner's: this plugin does not check it.
- **The console**: pyserial opens `oep://...` (oep_client.pyserial) - an RP2040's SEGGER RTT over the probe's SWD.
- **The upload** (`pytest_arduino_cli_upload`): a port `oep://<probe>/<port>` whose profile is an RP2040 board is
  written here over the probe's SWD (oep_client.rp2040_swd); anything else is left to arduino-cli.
- **The session** (`oep_host`): an OEP session on the probe behind the DUT's port, sharing a broker's lock when one
  serves it. When the DUT's console runs through the platform's own monitor (pytest-embedded-arduino-cli wraps the port
  for it), that monitor holds the probe and publishes its broker a moment later: the fixture waits for it, up to the
  ini value `oep_broker_wait` seconds (default 10). `oep_host.host` is the Host; `oep_host.touch()` keeps the lease.
- **The UART** (`oep_uart`): `oep_uart.open(baud)` -> a channel with expect / expect_exact / write (pexpect, like the
  Dut), an oep.fixture.uart planned onto the port's lines labelled `<port>.rx` / `<port>.tx` (host guide §5.3).

A broker that serves the probe is joined (oep_client.brokers, host guide §7).
"""

from __future__ import annotations

import json
import os
import urllib.parse
import warnings
from pathlib import Path

import pytest
import serial

from . import link

if "oep_client.pyserial" not in serial.protocol_handler_packages:
    serial.protocol_handler_packages.append("oep_client.pyserial")

PROBE_KEY = "probe:"
MEMO_ENV = "OEP_PORT_MEMO_"
OWNER_INI = "oep_memo_owner"
WAIT_INI = "oep_broker_wait"
LEASE_MS = 60000
KEEPALIVE_S = 20.0
EXPECT_TIMEOUT_S = 30.0            # pytest-embedded's Dut default


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addini(OWNER_INI, "The owner of the port records port_memo reads (<owner>/port/<port> on the probe, oep-if-memo "
                             "§4.1), e.g. io.github.example.mylib", default="")
    parser.addini(WAIT_INI, "Seconds oep_host waits for the broker of a probe the platform's monitor holds", default="10")


# ---- addresses --------------------------------------------------------------------------------------------------

def _bare(port: str) -> str:
    """The port as written, without pytest-embedded-arduino-cli's monitor wrapper."""
    try:
        from pytest_embedded_arduino_cli import MonitorTarget, is_monitor_url
    except ImportError:
        return port
    return MonitorTarget.from_url(port).address if is_monitor_url(port) else port


def oep_parts(port: str) -> tuple[str, str | None] | None:
    """`oep://<unit>[/<port>[/<target>]][?via=...]` -> (unit in lower case, port or None); None for other addresses."""
    u = urllib.parse.urlparse(_bare(port))
    if u.scheme != "oep" or not u.netloc:
        return None
    parts = [p for p in u.path.split("/") if p]
    return u.netloc.lower(), (parts[0] if parts else None)


def _probe_tty_serial(path: str) -> str | None:
    """The USB serial (= unit_id) of an OEP probe's own serial port, read from the OS's device list (not opened)."""
    if "://" in path or not os.path.exists(path):
        return None
    from serial.tools import list_ports
    want = os.path.realpath(path)
    for info in list_ports.comports():
        if os.path.realpath(info.device) == want:
            if (info.vid, info.pid) in link.PROJECT_VID_PIDS and info.serial_number:
                return info.serial_number.lower()
            return None
    return None


@pytest.hookimpl(optionalhook=True)
def pytest_arduino_cli_device_lock_key(config: pytest.Config, port: str) -> str | None:
    """The probe's lock: `probe:<unit_id>` for every port and route of one probe."""
    parts = oep_parts(port)
    if parts:
        return None if parts[0].startswith("x-") else PROBE_KEY + parts[0]    # an x- unit_id names no probe (core §7.5)
    serial_number = _probe_tty_serial(_bare(port))
    return PROBE_KEY + serial_number if serial_number else None


# ---- the port's record ------------------------------------------------------------------------------------------

def _env_name(probe_id: str, port_name: str | None) -> str:
    def up(s: str) -> str:
        return s.upper().replace("-", "_")
    return MEMO_ENV + up(probe_id) + (f"_{up(port_name)}" if port_name else "")


def _probe_id(config: pytest.Config, port: str) -> str | None:
    """The id in the port's lock name when it is a probe's (`probe:<id>`), from whichever plugin knows the address."""
    key = config.hook.pytest_arduino_cli_device_lock_key(config=config, port=port)
    names = key if isinstance(key, list) else [key]
    for name in names:
        if isinstance(name, str) and name.startswith(PROBE_KEY):
            return name[len(PROBE_KEY):]
    return None


def _memo_on_probe(unit: str, port_name: str, owner: str) -> dict | None:
    """The memo object `<owner>/port/<port>` on the probe (a lock-free read; through its broker when one serves it)."""
    from . import brokers, memo
    b = brokers.find(unit)
    h = brokers.connect(b) if b else None
    if h is None:
        try:
            h = link.open_host(f"oep://{unit}")
        except Exception:
            return None
    try:
        mo = memo.Memo(h)
        name = f"{owner}/port/{port_name.lower()}".encode()
        if name not in {e.name for e in mo.list(f"{owner}/port/")}:
            return None
        doc = json.loads(mo.read(name).decode("utf-8"))
        return doc if isinstance(doc, dict) else None
    except Exception:
        return None
    finally:
        h.link.close()


def port_memo_of(config: pytest.Config, port: str | None) -> dict:
    """The record of `port` (see the module): .env, else the probe's memo, else {}."""
    if not port:
        return {}
    port = _bare(port)
    probe_id = _probe_id(config, port)
    if probe_id is None:
        return {}
    parts = oep_parts(port)
    port_name = parts[1] if parts else None
    owner = config.getini(OWNER_INI)
    env = os.environ.get(_env_name(probe_id, port_name))
    from_env = None
    if env is not None:
        try:
            from_env = json.loads(env)
        except ValueError as e:
            raise pytest.UsageError(f"{_env_name(probe_id, port_name)} is not JSON ({e}); in .env, put it in single "
                                    f"quotes") from None
        if not isinstance(from_env, dict):
            raise pytest.UsageError(f"{_env_name(probe_id, port_name)} is not a JSON object")
    on_probe = _memo_on_probe(probe_id, port_name, owner) if (parts and port_name and owner) else None
    if from_env is not None:
        if on_probe is not None and on_probe != from_env:
            warnings.warn(f"{port}: {_env_name(probe_id, port_name)} differs from the probe's {owner}/port/"
                          f"{port_name.lower()}; the .env value is used (correct the memo or drop the variable)")
        return from_env
    return on_probe or {}


def _env_port(names: list[str]) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


def _up(s: str) -> str:
    return s.upper().replace("-", "_")


def _primary_port(request: pytest.FixtureRequest) -> str | None:
    """The DUT's port: --port, else TEST_SERIAL_PORT_<PROFILE>, else TEST_SERIAL_PORT (the plugin's guide)."""
    port = request.config.getoption("port", None)
    if port:
        return port
    combination = _combination(request)
    profile = combination.primary if combination is not None else request.config.getoption("profile", None)
    return _env_port(([f"TEST_SERIAL_PORT_{_up(profile)}"] if profile else []) + ["TEST_SERIAL_PORT"])


def _peer_port(request: pytest.FixtureRequest, name: str) -> str | None:
    combination = _combination(request)
    profile = combination.peers.get(name) if combination is not None else None
    base = f"TEST_SERIAL_PORT_PEER_{_up(name)}"
    return _env_port(([f"{base}_{_up(profile)}"] if profile else []) + [base])


def _combination(request: pytest.FixtureRequest):
    """pytest-embedded-arduino-cli's arduino_combination (its public fixture), None without it."""
    try:
        return request.getfixturevalue("arduino_combination")
    except pytest.FixtureLookupError:
        return None


@pytest.fixture
def port_memo(request: pytest.FixtureRequest) -> dict:
    """The record of the DUT's port: .env's OEP_PORT_MEMO_*, else the probe's <oep_memo_owner>/port/<port>, else {}."""
    return port_memo_of(request.config, _primary_port(request))


@pytest.fixture
def peer_port_memo(request: pytest.FixtureRequest):
    """`peer_port_memo(name)`: the record of a peer's port, as port_memo."""
    return lambda name: port_memo_of(request.config, _peer_port(request, name))


# ---- the upload -------------------------------------------------------------------------------------------------

@pytest.hookimpl(optionalhook=True)
def pytest_arduino_cli_upload(config, role, port, profile, fqbn, sketch_dir, build_path):
    """An RP2040 board on `oep://<probe>/<port>`: its ELF written over the probe's SWD. Anything else: None."""
    if not port or not fqbn or not str(fqbn).startswith("rp2040:"):
        return None
    parts = oep_parts(port)
    if not parts or not parts[1]:
        return None
    elf = Path(build_path) / f"{Path(sketch_dir).name}.ino.elf"
    if not elf.exists():
        raise FileNotFoundError(f"{elf}: the build to write over SWD")
    from . import rp2040_swd
    rp2040_swd.flash(_bare(port), str(elf), log=lambda msg: None)
    return True


# ---- the session and the UART -----------------------------------------------------------------------------------

class OepHost:
    """`oep_host`: a Host in a session on the probe (`host`), sharing a broker's lock when one serves it. `touch()`
    sends a keepalive when the session has been quiet for a while; a test quiet for longer than the lease (60 s) loses
    its session."""

    def __init__(self, unit: str, owner: str):
        import time
        from . import brokers
        self.host = brokers.open_host(unit, owner, lease_ms=LEASE_MS)
        self._last = time.monotonic()

    def touch(self) -> None:
        import time
        if time.monotonic() - self._last > KEEPALIVE_S:
            self.host.keepalive()
        self._last = time.monotonic()

    def close(self) -> None:
        try:
            self.host.end()
        except Exception:
            pass
        finally:
            self.host.link.close()


def _wait_for_broker(unit: str, seconds: float) -> None:
    """Until the broker of `unit` answers (host guide §7.2), at most `seconds`."""
    import time
    from . import brokers
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        b = brokers.find(unit)
        h = brokers.connect(b) if b else None
        if h is not None:
            h.link.close()
            return
        time.sleep(0.2)


@pytest.fixture
def oep_host(request: pytest.FixtureRequest):
    """An OEP session on the probe behind the DUT's port (see the module)."""
    port = _primary_port(request)
    parts = oep_parts(port or "")
    if not parts:
        raise pytest.UsageError(f"oep_host: the DUT's port is not an OEP probe ({port!r}); name it oep://<unit_id>/<port>")
    wrapped = port != _bare(port)
    if wrapped:
        request.getfixturevalue("dut")                  # its monitor starts first
        _wait_for_broker(parts[0], float(request.config.getini(WAIT_INI) or 0))
    session = OepHost(parts[0], owner="pytest oep_host")
    yield session
    session.close()


class _Stream:
    """pexpect over a read callback that returns what arrived without waiting (b"" for nothing yet)."""

    def __new__(cls, read, timeout: float):
        import time
        import pexpect

        class Stream(pexpect.spawnbase.SpawnBase):
            def read_nonblocking(self, size: int = 1, timeout: float | None = -1) -> bytes:
                if timeout == -1:
                    timeout = self.timeout
                end = None if timeout is None else time.monotonic() + timeout
                while True:
                    data = read()
                    if data:
                        self._log(data, "read")
                        return data
                    if end is not None and time.monotonic() >= end:
                        raise pexpect.TIMEOUT("Timeout exceeded.")
                    time.sleep(0.01)
        return Stream(timeout=timeout)


class Channel:
    """A byte stream shaped like pytest-embedded's Dut for what a bench test does with it: expect / expect_exact
    (pexpect's, with the Dut's keywords expect_all, not_matching, return_what_before_match; the default timeout 30 s;
    pexpect.TIMEOUT when nothing matched) and write."""

    def __init__(self, read, write, name: str):
        self._write, self.name = write, name
        self._proc = _Stream(read, EXPECT_TIMEOUT_S)

    def write(self, data) -> None:
        self._write(data.encode() if isinstance(data, str) else data)

    def expect(self, pattern, **kwargs):
        return self._expect(self._proc.expect, pattern, **kwargs)

    def expect_exact(self, pattern, **kwargs):
        return self._expect(self._proc.expect_exact, pattern, **kwargs)

    def _expect(self, func, pattern, *, expect_all: bool = False, not_matching=(), return_what_before_match: bool = False,
                **kwargs):
        import re
        import pexpect
        if return_what_before_match and expect_all:
            raise ValueError("`return_what_before_match` and `expect_all` cannot be `True` at the same time.")
        patterns = list(pattern) if isinstance(pattern, (list, tuple)) else [pattern]
        res = []
        while patterns:
            try:
                index = func(patterns, **kwargs)
            except (pexpect.EOF, pexpect.TIMEOUT) as e:
                tail = bytes((self._proc.before or b"")[-200:])
                raise e.__class__(f'{self.name}: not found "{pattern!s}"; last bytes: {tail!r}') from e
            proc = self._proc
            res.append(proc.before.rstrip() if proc.match in (pexpect.EOF, pexpect.TIMEOUT) else proc.match)
            for nm in (not_matching if isinstance(not_matching, (list, tuple)) else [not_matching]):
                rx = re.compile(nm.encode() if isinstance(nm, str) else
                                nm.pattern.encode() if isinstance(nm.pattern, str) else nm.pattern)
                if rx.search(proc.before):
                    raise ValueError(f"The pattern {rx} should not have been matched.")
            if not expect_all:
                break
            patterns.pop(index)
        if return_what_before_match:
            return self._proc.before
        return res[0] if len(res) == 1 else res


class OepUart:
    """`oep_uart`: `open(baud)` (again to change the rate) plans an oep.fixture.uart onto the port's rx / tx lines and
    returns its Channel. The plan is the oep_host session's."""

    def __init__(self, session: OepHost, port_name: str | None, address: str):
        self._session, self._port, self._address = session, port_name, address
        self.channel: Channel | None = None
        self._fn = None

    def open(self, baud: int) -> Channel:
        from . import core, fixture, host as oh, registry, settings
        self.close()
        h = self._session.host
        RX, TX = (registry.FIXTURE_UART.enum["role"][r] for r in ("rx", "tx"))
        labels = settings.labels(h)
        ports = settings.ports_named(labels)
        if self._port is None and len(ports) > 1:
            raise RuntimeError(f"the probe names ports {', '.join(ports)}: give the test's port as oep://<probe>/<port>")
        port_name = self._port if self._port is not None else (ports[0] if ports else None)
        rx, tx = (settings.line_from_labels(labels, line, port_name) for line in ("rx", "tx"))
        if rx is None or tx is None:
            where = f"{port_name}." if port_name else ""
            raise RuntimeError(f"no single {where}rx / {where}tx line in the probe's labels (rx {rx}, tx {tx}; ports: "
                               f"{', '.join(ports) or 'none'})")
        last = None
        for fn in core.find_all(h, "oep.fixture.uart"):
            io = fixture.FixtureUartIO(h, fn)
            try:
                core.plan_apply(h, [(fn, RX, rx), (fn, TX, tx)])
            except oh.Rejected as e:
                last = e
                continue
            try:
                io.configure(baud)
            except oh.Rejected as e:
                last = e
                core.plan_release(h, [fn])
                continue

            def read(io=io):
                self._session.touch()
                return io.read(io.MAX_READ)

            def write(data: bytes, io=io):
                self._session.touch()
                io.write(data)
            self._fn = fn
            self.channel = Channel(read, write, f"uart {self._address} @{baud} (fn {fn}, rx {rx}, tx {tx})")
            return self.channel
        raise RuntimeError(f"no oep.fixture.uart on this probe takes rx {rx} / tx {tx} at {baud} baud ({last})")

    def close(self) -> None:
        if self._fn is not None:
            from . import core
            try:
                core.plan_release(self._session.host, [self._fn])
            except Exception:
                pass
        self._fn, self.channel = None, None


@pytest.fixture
def oep_uart(request: pytest.FixtureRequest, oep_host):
    """The DUT's UART as the probe sees it (see the module)."""
    port = _bare(_primary_port(request) or "")
    uart = OepUart(oep_host, oep_parts(port)[1], port)
    yield uart
    uart.close()
