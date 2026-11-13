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


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addini(OWNER_INI, "The owner of the port records port_memo reads (<owner>/port/<port> on the probe, oep-if-memo "
                             "§4.1), e.g. io.github.example.mylib", default="")


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
