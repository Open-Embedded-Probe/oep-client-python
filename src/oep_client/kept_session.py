"""The session id a host that may stop and run again keeps per probe (host guide §5, transports §3).

A probe keeps a session when its transport closes (transports §3): a CLI that ended without end (killed, crashed, a
lost line), a test run that stopped half way, leaves its lock and what it held until the lease runs out. So a host
keeps the id of the session it has open in a file per probe, keyed by the probe's unit_id (fn 0's describe, core
§7.5), and the next run, before its first open, opens that id and ends it at once - releasing what the previous run
held - then opens as usual. An open with the id of the session holding the lock is taken as a resend of its open
(core §6.2: nothing released, the resend table dropped), so the end that follows is a new request of that session.

The file is `<directory>/<unit_id>.session`: the id as 8 hex digits, empty when no session is open. The directory is
$OEP_SESSION_DIR, else the user's runtime directory ($XDG_RUNTIME_DIR/oep-client), else the user's cache directory
(%LOCALAPPDATA%\\oep-client\\sessions on Windows, $XDG_CACHE_HOME or ~/.cache /oep-client/sessions elsewhere).

While a host keeps the file it holds an exclusive lock on it (flock; msvcrt.locking on Windows), dropped when the link
closes or the process ends however it ends. A file another live process holds is left alone: that host is running and
its session is its own - this host does not keep its id then (it opens as usual and meets that host's lock). A probe
whose unit_id begins `x-` names no unit (core §7.5): nothing is kept for it.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from . import message as m, registry as reg

# Imported here, not when a file is let go: __del__ may run at interpreter exit, when an import raises ImportError
if sys.platform == "win32":
    import msvcrt
    fcntl = None
else:
    import fcntl
    msvcrt = None

ENV = "OEP_SESSION_DIR"
_UNIT_ID = reg.CORE.tlv["describe"]["unit_id"]
_SAFE = re.compile(r"[a-z0-9-]{1,32}")               # core §7.5's unit_id grammar: safe as a file name
_WIN_LOCK_AT = 1 << 20                              # Windows: the byte locked, past any content


def default_dir() -> Path:
    env = os.environ.get(ENV)
    if env:
        return Path(env)
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return Path(runtime) / "oep-client"
    if sys.platform == "win32" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "oep-client" / "sessions"
    cache = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return Path(cache) / "oep-client" / "sessions"


def _lock(fd: int) -> bool:
    if msvcrt is not None:
        os.lseek(fd, _WIN_LOCK_AT, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(fd: int) -> None:
    try:
        if msvcrt is not None:
            os.lseek(fd, _WIN_LOCK_AT, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


class KeptSession:
    """One host's kept session id (`Host.kept`). Host.open calls `before_open` (once: find the file by unit_id, lock
    it, end the session it names) and `opened`; Host.end calls `ended`; the link's close calls `release`.
    `previous`: the id found from the run before (None: none); `ended_previous`: whether it was opened and ended."""

    def __init__(self, directory: str | os.PathLike | None = None):
        self.directory = Path(directory) if directory is not None else None
        self.path: Path | None = None
        self.fd: int | None = None
        self.done = False                            # before_open ran (kept or not)
        self.previous: int | None = None
        self.ended_previous = False
        self.why_not = ""                            # why nothing is kept, if so

    @staticmethod
    def unit_id(hst) -> str | None:
        from . import core
        value = next((v for tag, v in core.describe(hst, m.CORE_FN) if tag & 0x7F == _UNIT_ID), None)
        return value.decode("ascii", "replace") if value else None

    def before_open(self, hst, owner: str | None = None) -> None:
        if self.done:
            return
        self.done = True
        unit = self.unit_id(hst)
        if not unit or unit.startswith("x-") or not _SAFE.fullmatch(unit):
            self.why_not = f"unit_id {unit!r} names no unit"
            return
        directory = self.directory or default_dir()
        try:
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            path = directory / f"{unit}.session"
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as e:
            self.why_not = f"cannot keep it: {e}"
            return
        if not _lock(fd):
            os.close(fd)
            self.why_not = f"{path} is kept by another running host"
            return
        self.path, self.fd = path, fd
        os.lseek(fd, 0, os.SEEK_SET)
        text = os.read(fd, 64).decode("ascii", "replace").strip()
        try:
            self.previous = (int(text, 16) or None) if text else None
        except ValueError:
            self.previous = None
        if self.previous is not None and self.previous != hst.session:
            self.ended_previous = hst.end_previous(self.previous, owner)

    def _write(self, text: str) -> None:
        if self.fd is None:
            return
        try:
            os.lseek(self.fd, 0, os.SEEK_SET)
            os.ftruncate(self.fd, 0)
            os.write(self.fd, text.encode("ascii"))
        except OSError:
            pass

    def opened(self, sid: int) -> None:
        self._write(f"{sid:08x}\n")

    def ended(self) -> None:
        self._write("")

    def release(self) -> None:
        """Let the file go (the id stays in it for the next run when a session is still open)."""
        if self.fd is not None:
            _unlock(self.fd)
            os.close(self.fd)
            self.fd = None
        self.done = False

    def __del__(self):
        # at interpreter exit module globals may already be gone: the process's end drops the lock and the fd anyway
        try:
            self.release()
        except Exception:      # noqa: BLE001
            pass

