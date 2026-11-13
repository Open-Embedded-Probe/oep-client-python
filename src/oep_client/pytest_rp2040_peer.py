"""pytest-embedded-arduino-cli peers that are RP2040 boards on an OEP probe's SWD (oep_client.rp2040_swd), enabled by
`pytest_plugins = ["oep_client.pytest_rp2040_peer"]` in a conftest.py.

A peer `peer_<name>/` is such a peer when its address, the environment variable `TEST_SERIAL_PORT_PEER_<NAME>` (the
plugin's own), is `oep://<probe>/<port>`, and its profile uploads with arduino-pico's `uploadmethod=picoprobe_cmsis_dap`.
Then:

- its console: pyserial opens `oep://...` as the RTT console over SWD (oep_client.pyserial is registered);
- its upload: `interface/cmsis-dap.cfg` and `target/rp2040.cfg` are generated in the peer's directory (keep them out of
  git), so arduino-pico's OpenOCD hands the ELF to `python -m oep_client.rp2040_swd flash` instead of a CMSIS-DAP;
- its device lock: its own (`rp2040-swd:<address>`), not the probe's - it shares the probe with the DUT through ch32rv's
  broker;
- the broker: when the DUT is ch32rv's (pytest-embedded-arduino-cli-ch32rv's `ch32rv` fixture), its ch32rv goes to the
  writer as `CH32RV`, which finds the broker the DUT's monitor started and shares its lock.
"""

from __future__ import annotations

import os
import pathlib

import pytest
import serial

from . import rp2040_swd

if "oep_client.pyserial" not in serial.protocol_handler_packages:
    serial.protocol_handler_packages.append("oep_client.pyserial")


def _env_name(peer: str) -> str:
    return "TEST_SERIAL_PORT_PEER_" + peer.upper().replace("-", "_")


def _peer_addresses() -> set[str]:
    return {v for k, v in os.environ.items() if k.startswith("TEST_SERIAL_PORT_PEER_") and v.startswith("oep://")}


def pytest_collection_modifyitems(session, config, items):
    """The OpenOCD hook in every RP2040 peer directory beside the collected tests."""
    seen = set()
    for item in items:
        for peer_dir in pathlib.Path(item.path).parent.glob("peer_*"):
            if peer_dir in seen or not peer_dir.is_dir():
                continue
            seen.add(peer_dir)
            name = peer_dir.name[len("peer_"):]
            address = os.environ.get(_env_name(name), "")
            yaml = peer_dir / "sketch.yaml"
            if address.startswith("oep://") and yaml.exists() and "picoprobe_cmsis_dap" in yaml.read_text():
                rp2040_swd.write_openocd_hook(str(peer_dir), _env_name(name))


def pytest_arduino_cli_device_lock_key(config, port):
    if port and port in _peer_addresses():
        return f"rp2040-swd:{port}"
    return None


@pytest.fixture(autouse=True)
def _oep_rp2040_peer_broker(request, monkeypatch):
    """The DUT's ch32rv (when there is one) for the peer's writer, which runs under OpenOCD apart from pytest."""
    if not _peer_addresses():
        return
    try:
        monkeypatch.setenv("CH32RV", str(request.getfixturevalue("ch32rv")))
    except pytest.FixtureLookupError:
        pass
