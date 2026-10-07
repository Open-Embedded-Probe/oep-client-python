"""One board's run: the connection to its probe, what each test measured and decided, and the results file
tests/hw/results/<board>-<firmware>-<client>-<started>.json, one per run - a later run never overwrites an earlier one
(release-testing.ja.md §3: every measurement and verdict, small summaries only; the raw logs stay out)."""
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
from oep_client import catalog, config, core, host as h, link, message as m, registry as reg

from . import boards

HERE = pathlib.Path(__file__).resolve().parent
RESULTS = HERE / "results"
DESCRIBE = reg.CORE.tlv["describe"]
COMMON = reg.DESCRIBE_COMMON
WRITE_TIMEOUT_S = 5.0
RESTORE_CONNECT_S = 5.0          # putting the settings back: the wait for a probe that is not open (more with OEP_HW_REOPEN_S)
RESTORE_LOCK_WAIT_S = 35.0       # ... and for the lock: a lost connection's session lapses within its 30 s lease
_ITEM_KINDS = {config.Label: "label", config.Idle: "idle", config.Disable: "disable"}


def reopen_s() -> float:
    """OEP_HW_REOPEN_S: how long more a probe that is not back within its restart_max_ms is opened again for (the
    user's reopen, Host.restart_probe's reopen_s; a WSL host where usbipd attaches the re-enumerated device). 0: none."""
    return float(os.environ.get("OEP_HW_REOPEN_S", "") or 0)


def _by_key(items) -> dict[tuple[str, int], tuple[int, bytes]]:
    """The label / idle / disable items (what tests/hw sets) as {(kind, channel): (tag, value)}."""
    out = {}
    for tag, value in items:
        it = config.decode(tag, value)
        kind = _ITEM_KINDS.get(type(it))
        if kind:
            out[(kind, it.channel)] = (tag, value)
    return out


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
    """What fn 0's describe says the probe is: firmware, model, unit_id, chip, transports, max_op_ms (and the
    confirm's boot_id, limits); link_fn and port_speed (True) when the probe offers oep.probe.link and its ops set
    port_speed; plan_fn when it lists oep.probe.plan; restart_fn and restart_max_ms when it lists oep.probe.restart."""
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
        elif t == DESCRIBE["max_op_ms"] and len(v) >= 4:
            info["max_op_ms"] = struct.unpack_from("<I", v)[0]
    try:                                    # port_speed is an op of oep.probe.link (oep-if-link §1), an optional interface
        link_fn = core.link_fn(hst)
    except LookupError:
        link_fn = None
    if link_fn is not None:
        info["link_fn"] = link_fn
        if core.LINK_PORT_SPEED in (core.ops(hst, link_fn) or ()):
            info["port_speed"] = True       # absent: no oep.probe.link, or its ops do not set port_speed
    for key, name in (("plan_fn", core.PLAN_NAME), ("restart_fn", core.RESTART_NAME)):
        fns = core.find_all(hst, name)
        if fns:
            info[key] = fns[0]
    if "restart_fn" in info:
        info["restart_max_ms"] = core.restart_max_ms(hst)
    return info


def declared(hst: h.Host, fn: int) -> dict:
    """An interface's describe (core §7.4), the common tags decoded: role_channels {role: [channels]}, channel_groups
    [(group, [(role, channel)])], max_clock_hz, min_clock_hz, max_length, features, ops; `own`: the
    interface's own tags (0x40 and up) raw, {tag: [value, ...]} in the order declared."""
    out: dict = {"role_channels": {}, "channel_groups": [], "own": {}}
    for tag, v in core.describe(hst, fn):
        t = tag & 0x7F
        if t == COMMON["role_channels"] and len(v) >= 3:
            base = struct.unpack_from("<H", v, 1)[0]
            out["role_channels"].setdefault(v[0], []).extend(catalog.bitmap_to_channels(base, v[3:]))
        elif t == COMMON["channel_group"] and len(v) >= 2:
            out["channel_groups"].append(catalog.unpack_channel_group(v))
        elif t in (COMMON["max_clock_hz"], COMMON["min_clock_hz"], COMMON["features"]) and len(v) >= 4:
            name = next(k for k, c in COMMON.items() if c == t)
            out[name] = struct.unpack_from("<I", v)[0]
        elif t == COMMON["max_length"] and len(v) >= 2:
            out["max_length"] = struct.unpack_from("<H", v)[0]
        elif t == COMMON["ops"]:
            out.setdefault("ops", []).extend(sorted(catalog.unpack_ops(v)))   # core §7.4
        elif t >= 0x40:
            out["own"].setdefault(t, []).append(v)
    return out


def own_u(decl: dict, tag: int, fmt: str = "<I"):
    """The first value of the interface's own tag `tag`, unpacked as `fmt` (a single number); None when not declared."""
    values = decl["own"].get(tag)
    if not values or len(values[0]) < struct.calcsize(fmt):
        return None
    return struct.unpack_from(fmt, values[0])[0]


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
    lost: str | None = None                          # the probe went away (not back after a restart): later tests skip
    # The probe's settings: what they were before tests/hw changed any, the items a test set that are not put back yet
    # ((kind, channel) -> the test), whether a test saved; what could not be put back (the summary prints how)
    settings_baseline: dict | None = None
    settings_pending: dict = field(default_factory=dict)
    settings_saved: bool = False
    settings_left: list = field(default_factory=list)

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
        if self.lost is not None:
            try:                                     # back since (a usbipd attach by hand): put the settings back first
                self.connect(timeout_s=1.0)
                self.lost = None
            except Exception:                        # noqa: BLE001
                pytest.skip(f"the probe is gone: {self.lost}")
            self.restore_settings("reconnected")
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

    # ---- the probe's settings: put back whatever a test changed -------------------------------------------------
    def settings_before(self, cfg: config.ProbeConfig) -> None:
        """The probe's settings before tests/hw changes any (once a run): the label / idle / disable items and the
        storage (state, saved hash) - what restore_settings puts back."""
        if self.settings_baseline is None:
            h0, items = cfg.get()
            st = cfg.state()
            self.settings_baseline = {"hash": h0, "items": _by_key(items), "storage": st.storage,
                                      "saved_hash": st.saved_hash}

    def settings_changing(self, test: str, kind: str, channel: int) -> None:
        """A test is about to set this item: restore_settings puts it back (removes it, or the value it had)."""
        self.settings_pending.setdefault((kind, channel), test)

    def settings_saving(self) -> None:
        """A test is about to save: restore_settings puts the storage back as it was."""
        self.settings_saved = True

    def _host_for_restore(self) -> h.Host:
        """The host with the lock, for putting the settings back: the run's own, or the probe opened again (it went
        away: not back after a restart) - waiting out a lost session's lease."""
        hst = self.hst if self.lost is None else None
        if hst is not None:
            try:
                if hst.session is not None:
                    hst.keepalive()
                    return hst
                core.take(hst, 30000, owner="oep tests/hw", wait_s=RESTORE_LOCK_WAIT_S)
                return hst
            except Exception:                        # noqa: BLE001 - the link is gone: open it again below
                pass
        self.connect(timeout_s=max(reopen_s(), RESTORE_CONNECT_S))
        self.lost = None
        core.take(self.hst, 30000, owner="oep tests/hw", wait_s=RESTORE_LOCK_WAIT_S)
        return self.hst

    def restore_settings(self, where: str) -> bool:
        """Put the probe's settings back as settings_before found them: every pending item removed (or its value from
        before set again), and after a save the storage as it was (saved again, or erased when nothing was saved).
        Runs in a test's finally and at the end of the run - also after a failure or a probe that went away (opened
        again first). -> True when nothing is left; else what is left goes to settings_left (the summary prints the
        commands that remove it) and a later call tries again."""
        if not self.settings_pending and not self.settings_saved:
            return True
        entry = self.tests.setdefault("_settings", {})
        base = self.settings_baseline or {"items": {}, "storage": None, "saved_hash": None}
        try:
            hst = self._host_for_restore()
            cfg = config.ProbeConfig(hst)
            now = _by_key(cfg.get()[1])
            unsets = [key for key in self.settings_pending if key not in base["items"] and key in now]
            sets = [m.tlv(*base["items"][key]) for key in self.settings_pending
                    if key in base["items"] and now.get(key) != base["items"][key]]
            if unsets:
                cfg.unset(unsets)
            if sets:
                cfg.set(sets)
            done = {"where": where, "removed": [f"{k} {c}" for k, c in unsets], "set_back": len(sets)}
            if self.settings_saved:
                st = cfg.state()
                if base["storage"] == "applied" and st.saved_hash != base["saved_hash"]:
                    done["saved"] = cfg.save()       # the live settings now: as before the tests, but for unsaved ones
                elif base["storage"] == "none" and st.storage != "none":
                    cfg.erase()
                    done["erased"] = True
            self.settings_pending.clear()
            self.settings_saved = False
            self.settings_left = []
            entry.setdefault("restored", []).append(done)
            return True
        except Exception as e:                       # noqa: BLE001 - gone or refused: say what is left and how to clean up
            self.settings_left = self._cleanup_steps(base)
            entry.setdefault("not_restored", []).append({"where": where, "error": f"{type(e).__name__}: {e}"})
            entry["left_on_probe"] = self.settings_left
            return False

    def _cleanup_steps(self, base: dict) -> list[str]:
        """The `oep` commands that put the settings back by hand (the probe never came back during the run)."""
        probe = self.port or self.board.port or "<probe>"
        steps = []
        for (kind, channel), test in self.settings_pending.items():
            if (kind, channel) in base["items"]:
                was = config.decode(*base["items"][(kind, channel)])
                steps.append(f"{kind} {channel} (set by {test}) had {was} before: set it again with oep config")
            else:
                steps.append(f"oep config remove {probe} {kind} {channel}")
        if self.settings_saved:
            steps.append(f"oep config save {probe}" if base["storage"] != "none" else f"oep config erase {probe}")
        return steps

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
        when = re.sub(r"[^0-9T]", "", self.started[:19])         # 2026-10-06T20:31:21+09:00 -> 20261006T203121
        path = where / (f"{safe(self.board.id)}-{safe(self.firmware_tag())}-{safe(self.client['version'])}"
                        f"-{when}.json")                       # the run's start: every run keeps its own file
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
        if self.lost:
            lines.append(f"  PROBE GONE: {self.lost}"[:118])
        if self.settings_left:
            lines.append("  SETTINGS LEFT ON THE PROBE by tests/hw - once it is back, put them back:")
            lines += [f"    {step}" for step in self.settings_left]
        return lines


def _short(v) -> str:
    if isinstance(v, float):
        return f"{v:.3g}"
    if isinstance(v, (list, dict)):
        return f"[{len(v)}]"
    s = str(v)
    return s if len(s) <= 24 else s[:21] + "..."
