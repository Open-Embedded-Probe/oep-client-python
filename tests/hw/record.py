"""One board's run: the connection to its probe, what each test measured and decided, and the results file
tests/hw/results/<board>-<firmware>-<client>.json (release-testing.ja.md §3: every measurement and verdict, small
summaries only; the raw logs stay out)."""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import platform
import re
import struct
import subprocess
import sys
import time
from dataclasses import dataclass, field

import oep_client
from oep_client import core, host as h, link, registry as reg

from . import boards

HERE = pathlib.Path(__file__).resolve().parent
RESULTS = HERE / "results"
DESCRIBE = reg.CORE.tlv["describe"]
WRITE_TIMEOUT_S = 5.0


def _open_serial_with_write_timeout(port: str, baud: int = link.BASE_BAUD):
    """The client opens serial ports with no write timeout; a USB CDC probe whose firmware stopped draining its OUT
    endpoint then blocks a write for ever. In a test run that is a failure to report, not a hang: a bounded write."""
    stream = _open_serial(port, baud)
    stream.write_timeout = WRITE_TIMEOUT_S
    return stream


_open_serial = link.open_serial
link.open_serial = _open_serial_with_write_timeout


def client_state() -> dict:
    repo = HERE.parents[1]
    out = {"version": oep_client.__version__, "python": platform.python_version()}
    try:
        out["commit"] = subprocess.run(["git", "-C", str(repo), "rev-parse", "--short", "HEAD"], capture_output=True,
                                       text=True, check=True).stdout.strip()
        out["dirty"] = bool(subprocess.run(["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=no"],
                                           capture_output=True, text=True, check=True).stdout.strip())
    except (subprocess.CalledProcessError, FileNotFoundError):
        out["commit"] = None
    return out


def probe_info(hst: h.Host) -> dict:
    """What oep.core's describe says the probe is: firmware, model, unit_id, chip (and the confirm's boot_id, limits)."""
    limits = hst.confirmed()
    info = {"boot_id": limits["boot_id"], "revision": limits["revision"], "max_frame": limits["max_frame"],
            "max_inflight": limits["max_inflight"], "window": limits["window"]}
    for tag, v in core.describe(hst):
        t = tag & 0x7F
        if t == DESCRIBE["firmware"]:
            info["firmware"] = v.decode("utf-8", "replace")
        elif t == DESCRIBE["model"]:
            info["model"] = v.decode("utf-8", "replace")
        elif t == DESCRIBE["unit_id"]:
            info["unit_id"] = v.decode("ascii", "replace") if re.fullmatch(rb"[0-9a-fA-F]+", v) else v.hex()
        elif t == DESCRIBE["chip"]:
            info["chip"] = v.decode("utf-8", "replace")
        elif t == DESCRIBE["transport"]:
            info.setdefault("transports", []).append({"index": v[0], "kind": v[1]})
        elif t == DESCRIBE["port_speed"] and v:
            info["port_speed"] = bool(v[0])
        elif t == DESCRIBE["max_op_ms"] and len(v) >= 4:
            info["max_op_ms"] = struct.unpack_from("<I", v)[0]
    return info


@dataclass
class Run:
    board: boards.Board
    started: str = field(default_factory=lambda: dt.datetime.now().astimezone().isoformat(timespec="seconds"))
    client: dict = field(default_factory=client_state)
    firmware: dict = field(default_factory=dict)     # source, expected, before, after, flashed
    tests: dict = field(default_factory=dict)        # test name -> {verdict, ...measurements}
    hst: h.Host | None = None
    port: str = ""
    flash_failed: str | None = None                  # set when the firmware could not be put on: later tests skip
    fake: subprocess.Popen | None = None
    t0: float = field(default_factory=time.monotonic)

    # ---- the connection --------------------------------------------------------------------------------------------
    def peek(self, timeout_s: float = 4.0) -> dict | None:
        """Lock-free: what the board says it runs now (None when nothing answers). Closed again after."""
        try:
            hst = link.open_host(self.port, timeout=1.0)
        except Exception as e:      # noqa: BLE001 - no port, busy, ...
            return {"error": f"{type(e).__name__}: {e}"}
        try:
            deadline = time.monotonic() + timeout_s
            while True:
                try:
                    hst.confirm()
                    return probe_info(hst)
                except Exception as e:      # noqa: BLE001 - not an OEP probe (yet)
                    if time.monotonic() > deadline:
                        return {"error": f"{type(e).__name__}: {e}"}
                    time.sleep(0.3)
        finally:
            hst.link.close()

    def connect(self, timeout_s: float = 30.0) -> h.Host:
        """Open the probe's port and wait for its confirm (a board that is still booting answers nothing for a
        moment; a USB probe may not be enumerated yet)."""
        self.close_host()
        deadline = time.monotonic() + timeout_s
        last = None
        while True:
            try:
                self.port = boards.resolve_port(self.board) if self.board.kind != "fake" else self.port
                hst = link.open_host(self.port, timeout=1.0)
            except Exception as e:      # noqa: BLE001
                last = e
                hst = None
            if hst is not None:
                try:
                    hst.confirm()
                    hst.link.timeout = 3.0
                    self.hst = hst
                    return hst
                except Exception as e:      # noqa: BLE001
                    last = e
                    hst.link.close()
            if time.monotonic() > deadline:
                raise TimeoutError(f"{self.board.id}: no confirm on {self.port or self.board.port} within {timeout_s:.0f} s "
                                   f"({type(last).__name__}: {last})")
            time.sleep(0.5)

    def close_host(self) -> None:
        if self.hst is not None:
            try:
                if self.hst.session is not None:
                    self.hst.end()
            except Exception:           # noqa: BLE001
                pass
            try:
                self.hst.link.close()
            finally:
                self.hst = None

    def take(self, lease_ms: int = 30000) -> h.Host:
        """The lock for a test: keep the session going, or open one (after a reboot, a lapse or an end)."""
        hst = self.require()
        if hst.session is not None:
            try:
                hst.keepalive()
                return hst
            except (h.Rejected, h.OepError):
                hst.session = None
        core.take(hst, lease_ms, owner="oep tests/hw", force=hst.session is None and self.board.kind == "fake")
        return hst

    def require(self) -> h.Host:
        """The host for a test after the flash step: skip when the firmware never got on or nothing answers."""
        import pytest
        if self.flash_failed:
            pytest.skip(f"flashing failed: {self.flash_failed.splitlines()[0]}")
        if self.hst is None:
            pytest.skip("no connection to the probe (the flash step did not run or failed)")
        return self.hst

    def wait_reboot(self, timeout_s: float = 20.0) -> dict:
        """After a reset: wait for a confirm with a new boot_id on the same link."""
        hst = self.require()
        before = hst.limits["boot_id"] if hst.limits else None
        saved, hst.link.timeout = hst.link.timeout, 0.5
        deadline = time.monotonic() + timeout_s
        t0 = time.monotonic()
        try:
            while True:
                try:
                    limits = hst.confirm()
                    if limits["boot_id"] != before:
                        hst.session = None                  # the probe forgot it
                        return {"boot_id_before": before, "boot_id_after": limits["boot_id"],
                                "seconds": round(time.monotonic() - t0, 2)}
                except Exception:   # noqa: BLE001 - booting
                    pass
                if time.monotonic() > deadline:
                    raise TimeoutError(f"no confirm with a new boot_id within {timeout_s:.0f} s after the reset")
                time.sleep(0.2)
        finally:
            hst.link.timeout = saved

    # ---- the record ------------------------------------------------------------------------------------------------
    def record(self, test: str, **values) -> dict:
        entry = self.tests.setdefault(test, {})
        entry.update(values)
        return entry

    def verdict(self, test: str, outcome: str, why: str = "") -> None:
        entry = self.tests.setdefault(test, {})
        entry["verdict"] = outcome
        if why:
            entry["why"] = why

    def firmware_tag(self) -> str:
        after = self.firmware.get("after") or {}
        tag = after.get("firmware") or "unknown"
        src = self.firmware.get("source") or {}
        if src.get("kind") == "local" and src.get("commit"):
            tag += f"+{src['commit']}" + ("-dirty" if src.get("dirty") else "")
        return tag

    def to_json(self) -> dict:
        return {
            "schema": 1,
            "board": {k: v for k, v in self.board.__dict__.items() if v not in (None, "", (), (0, 0), 0)},
            "client": self.client,
            "firmware": self.firmware,
            "started": self.started,
            "seconds": round(time.monotonic() - self.t0, 1),
            "host": {"platform": platform.platform(), "python": sys.version.split()[0]},
            "environment": {k: v for k, v in os.environ.items() if k.startswith("OEP_")},
            "tests": self.tests,
        }

    def write(self, where: pathlib.Path = RESULTS) -> pathlib.Path:
        where.mkdir(parents=True, exist_ok=True)
        safe = lambda s: re.sub(r"[^A-Za-z0-9._+-]", "_", s)  # noqa: E731
        path = where / f"{safe(self.board.id)}-{safe(self.firmware_tag())}-{safe(self.client['version'])}.json"
        path.write_text(json.dumps(self.to_json(), indent=2, default=str) + "\n", encoding="utf-8")
        return path

    def summary(self) -> list[str]:
        fw = self.firmware
        before = (fw.get("before") or {}).get("firmware", "?")
        after = (fw.get("after") or {}).get("firmware", "?")
        src = fw.get("source") or {}
        src_text = {"local": f"local {src.get('dir', '')} @{src.get('commit')}{' dirty' if src.get('dirty') else ''}",
                    "release": f"release {src.get('version')}", "on-board": "already on the board"}.get(src.get("kind"), "?")
        lines = [f"{self.board.id} ({self.board.kind}, {self.board.profile}) client {self.client['version']}",
                 f"  firmware {before} -> {after}  [{src_text}]"]
        for name, t in self.tests.items():
            if name.startswith("_"):
                continue
            keys = [k for k in t if k not in ("verdict", "why")]
            short = ", ".join(f"{k}={_short(t[k])}" for k in keys[:6])
            lines.append(f"  {t.get('verdict', '?'):7} {name:12} {short}"[:118])
            if t.get("why"):
                lines.append(f"          {str(t['why']).splitlines()[0][:100]}")
        return lines


def _short(v) -> str:
    if isinstance(v, float):
        return f"{v:.3g}"
    if isinstance(v, (list, dict)):
        return f"[{len(v)}]"
    s = str(v)
    return s if len(s) <= 24 else s[:21] + "..."
