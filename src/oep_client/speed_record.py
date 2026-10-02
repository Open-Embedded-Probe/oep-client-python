"""The port_speed record (host guide §7.4): which rates passed or failed on a serial port with a probe, so the next
session puts a passed rate first and leaves failed ones out until they expire.

Keyed by the port (the OS device path) and the probe's unit_id: the bridge chip belongs to the port, the probe to the
unit_id, so either one changing starts the record afresh. One JSON file, by default
`$XDG_CACHE_HOME/oep-client/link-speed.json` (`~/.cache/...`). A pass expires after `pass_ttl` (PASS_TTL_S, 30 days),
a failure after `fail_ttl` (FAIL_TTL_S, 1 day: a bridge that broke once may pass tomorrow, and a day-old failure is
cheap to measure again), an unknown after `fail_ttl` too. A file that cannot be read or written is not an error: the
record is a cache (`SpeedRecord.error` says what went wrong).

The file: {"<port>|<unit_id>": {"port", "unit_id", "rates": {"<rate>": {"result", "passed", "phase", "at"}}}} -
`result` "passed" / "failed" / "unknown" (measured within settle_s of a breakdown at another rate: neither, host
guide §7.4), `passed` true / false / null (the same, for a reader of the older shape), `phase` where it was decided
("try", "confirm", "verify", "probation", "in_use"), `at` ISO 8601 UTC. An entry without `result` is read from
`passed`. Not a released format: oep-client-js's speedrecord.js reads and writes the same.

  rec = SpeedRecord()                              # or SpeedRecord(path) - tests pass a temporary file
  passed, failed = rec.lookup("/dev/ttyUSB0", "fafe00000003")
  rec.note("/dev/ttyUSB0", "fafe00000003", 921600, passed=True, phase="verify")

`link.raise_speed(..., record=True)` (the `oep speed` CLI's default) reads and writes it; the library default is off.
A unit_id starting with `x-` names no unit (core §7.5, C-24: a probe with neither a unique number nor storage): nothing
is kept or found under it, so another unit on the same port inherits nothing.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
from pathlib import Path

PASS_TTL_S = 30 * 86400    # a pass is kept this long
FAIL_TTL_S = 86400         # a failure (and an unknown) this long
RESULTS = ("passed", "failed", "unknown")


def default_path() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return Path(base) / "oep-client" / "link-speed.json"


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _result(v: dict) -> str:
    """An entry's result: `result`, else from `passed` (true / false / null)."""
    r = v.get("result")
    if r in RESULTS:
        return r
    p = v["passed"]
    return "unknown" if p is None else "passed" if p else "failed"


class SpeedRecord:
    """The record as a dict {"<port>|<unit_id>": {"port", "unit_id", "rates": {"<rate>": {...}}}} (module doc)."""

    def __init__(self, path: str | os.PathLike | None = None, *, pass_ttl: float = PASS_TTL_S,
                 fail_ttl: float = FAIL_TTL_S):
        self.path = Path(path) if path is not None else default_path()
        self.pass_ttl = _dt.timedelta(seconds=pass_ttl)
        self.fail_ttl = _dt.timedelta(seconds=fail_ttl)
        self.error: str | None = None
        self.data: dict = {}
        try:
            self.data = json.loads(self.path.read_text("utf-8"))
            if not isinstance(self.data, dict):
                self.data = {}
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            self.error = f"{self.path}: {e}"
            self.data = {}

    @staticmethod
    def key(port: str, unit_id: str) -> str:
        return f"{port}|{unit_id}"

    def _fresh(self, entry: dict) -> dict:
        """The entry's rates that have not expired: rate -> "passed" / "failed" / "unknown"."""
        out = {}
        now = _now()
        for rate, v in (entry.get("rates") or {}).items():
            try:
                result = _result(v)
                at = _dt.datetime.fromisoformat(v["at"])
                if at.tzinfo is None:
                    at = at.replace(tzinfo=_dt.timezone.utc)
                if at >= now - (self.pass_ttl if result == "passed" else self.fail_ttl):
                    out[int(rate)] = result
            except (KeyError, TypeError, ValueError, AttributeError):
                continue
        return out

    @staticmethod
    def names_a_unit(unit_id: str) -> bool:
        """False for an `x-` unit_id (core §7.5): it names no unit, and nothing is keyed by it."""
        return not unit_id.startswith("x-")

    def lookup(self, port: str, unit_id: str) -> tuple[list[int], list[int]]:
        """-> (rates that passed, rates that failed) on this port with this probe, within their expiry; each rate is in
        one list only (its latest note), fastest first. An unknown is in neither. An `x-` unit_id: nothing."""
        if not self.names_a_unit(unit_id):
            return [], []
        rates = self._fresh(self.data.get(self.key(port, unit_id), {}))
        passed = sorted((r for r, v in rates.items() if v == "passed"), reverse=True)
        failed = sorted((r for r, v in rates.items() if v == "failed"), reverse=True)
        return passed, failed

    def results(self, port: str, unit_id: str) -> dict[int, str]:
        """Every rate within its expiry -> "passed" / "failed" / "unknown"."""
        if not self.names_a_unit(unit_id):
            return {}
        return self._fresh(self.data.get(self.key(port, unit_id), {}))

    def note(self, port: str, unit_id: str, rate: int, passed: bool | None, phase: str = "") -> None:
        """Remember that `rate` passed (True), failed (False) or is unknown (None: measured while the line was still
        settling) now, decided at `phase`, and save. An `x-` unit_id: nothing is kept (core §7.5)."""
        if not self.names_a_unit(unit_id):
            return
        entry = self.data.setdefault(self.key(port, unit_id), {"port": port, "unit_id": unit_id, "rates": {}})
        entry["port"], entry["unit_id"] = port, unit_id
        result = "unknown" if passed is None else "passed" if passed else "failed"
        entry.setdefault("rates", {})[str(int(rate))] = {
            "result": result, "passed": None if passed is None else bool(passed), "phase": phase,
            "at": _now().isoformat(timespec="seconds")}
        self.save()

    def save(self) -> bool:
        """Write the file (expired rates dropped). False (and `error` set) when it cannot be written."""
        for key in list(self.data):
            entry = self.data[key]
            fresh = self._fresh(entry) if isinstance(entry, dict) else {}
            if not fresh:
                del self.data[key]
                continue
            entry["rates"] = {str(r): v for r, v in entry["rates"].items() if int(r) in fresh}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(self.data, indent=1, sort_keys=True) + "\n", "utf-8")
            os.replace(tmp, self.path)
            return True
        except OSError as e:
            self.error = f"{self.path}: {e}"
            return False
