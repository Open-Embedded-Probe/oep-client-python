"""The port_speed record (host guide §7.4): which rates passed or failed on a serial port with a probe, so the next
session puts a passed rate first and leaves failed ones out until they expire.

Keyed by the port (the OS device path) and the probe's unit_id: the bridge chip belongs to the port, the probe to the
unit_id, so either one changing starts the record afresh. One JSON file, by default
`$XDG_CACHE_HOME/oep-client/link-speed.json` (`~/.cache/...`), entries older than EXPIRY_DAYS (30) dropped. A file
that cannot be read or written is not an error: the record is a cache (`SpeedRecord.error` says what went wrong).

  rec = SpeedRecord()                              # or SpeedRecord(path) - tests pass a temporary file
  passed, failed = rec.lookup("/dev/ttyUSB0", "0070070d9394")
  rec.note("/dev/ttyUSB0", "0070070d9394", 921600, passed=True)

`link.raise_speed(..., record=True)` (the `oep speed` CLI's default) reads and writes it; the library default is off.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
from pathlib import Path

EXPIRY_DAYS = 30


def default_path() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return Path(base) / "oep-client" / "link-speed.json"


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


class SpeedRecord:
    """The record as a dict {"<port>|<unit_id>": {"port", "unit_id", "rates": {"<rate>": {"passed", "at"}}}}."""

    def __init__(self, path: str | os.PathLike | None = None, expiry_days: int = EXPIRY_DAYS):
        self.path = Path(path) if path is not None else default_path()
        self.expiry = _dt.timedelta(days=expiry_days)
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
        """The entry's rates that have not expired: rate -> passed."""
        out = {}
        cutoff = _now() - self.expiry
        for rate, v in (entry.get("rates") or {}).items():
            try:
                at = _dt.datetime.fromisoformat(v["at"])
                if at.tzinfo is None:
                    at = at.replace(tzinfo=_dt.timezone.utc)
                if at >= cutoff:
                    out[int(rate)] = bool(v["passed"])
            except (KeyError, TypeError, ValueError):
                continue
        return out

    def lookup(self, port: str, unit_id: str) -> tuple[list[int], list[int]]:
        """-> (rates that passed, rates that failed) on this port with this probe, within the expiry; each rate is in
        one list only (its latest note), fastest first."""
        rates = self._fresh(self.data.get(self.key(port, unit_id), {}))
        passed = sorted((r for r, ok in rates.items() if ok), reverse=True)
        failed = sorted((r for r, ok in rates.items() if not ok), reverse=True)
        return passed, failed

    def note(self, port: str, unit_id: str, rate: int, passed: bool) -> None:
        """Remember that `rate` passed (or failed) now, and save."""
        entry = self.data.setdefault(self.key(port, unit_id), {"port": port, "unit_id": unit_id, "rates": {}})
        entry["port"], entry["unit_id"] = port, unit_id
        entry.setdefault("rates", {})[str(int(rate))] = {"passed": bool(passed), "at": _now().isoformat(timespec="seconds")}
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
