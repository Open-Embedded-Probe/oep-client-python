"""Host side of the draft v1 session rules, over any `send(request bytes) -> result bytes` transport.

The host picks a random u32 session id for every open (never a counter: after a probe reboot a counter would
start again and match an old process's id). A one-shot CLI keeps the id between commands; a request that goes
through with the same id proves nobody else operated the probe in between (oep-spec session-and-exclusivity).
"""

from __future__ import annotations

import random
import struct
from dataclasses import dataclass, field
from typing import Callable

from . import message as m


class Rejected(Exception):
    def __init__(self, result: m.Result):
        super().__init__(result.describe())
        self.result = result


class Locked(Rejected):
    @property
    def remaining_ms(self) -> int:
        return struct.unpack("<I", self.result.payload[:4])[0]


class NoSession(Rejected):
    pass


class Busy(Rejected):
    pass


_REJECTS = {m.LOCKED: Locked, m.NO_SESSION: NoSession, m.BUSY: Busy}


@dataclass
class Opened:
    lease_ms: int
    boot_id: int
    resumed: bool


@dataclass
class Host:
    send: Callable[[bytes], bytes]
    rng: random.Random = field(default_factory=random.SystemRandom)
    session: int | None = None
    _corr: int = 0

    def request(self, fn: int, op: int, payload: bytes = b"", *, locked: bool = True) -> m.Result:
        """locked=True sends the session id (role 0x81); lock-free requests may leave it off."""
        self._corr = self._corr % 0xFFFF + 1
        req = m.Request(self._corr, fn, op, payload, self.session if locked else None)
        result = m.Result.unpack(self.send(req.pack()))
        if result.corr != req.corr:
            raise ValueError(f"result for correlation {result.corr}, expected {req.corr}")
        if result.resolution == m.REJECTED:
            raise _REJECTS.get(result.detail, Rejected)(result)
        return result

    # ---- session --------------------------------------------------------------------------------
    def open(self, lease_ms: int = 0, *, force: bool = False, session: int | None = None) -> Opened:
        """A new random id unless `session` is given (a one-shot CLI resuming its saved id)."""
        sid = session if session is not None else self.rng.randrange(1, 1 << 32)
        r = self.request(m.CORE_FN, m.OP_OPEN, struct.pack("<IIB", sid, lease_ms, int(force)), locked=False)
        self.session = sid
        lease, boot_id, resumed = struct.unpack("<IIB", r.payload)
        return Opened(lease, boot_id, bool(resumed))

    def end(self) -> None:
        self.request(m.CORE_FN, m.OP_END)

    def keepalive(self) -> None:
        self.request(m.CORE_FN, m.OP_KEEPALIVE)

    def lock_state(self) -> tuple[bool, int]:
        locked, remaining = struct.unpack("<BI", self.request(m.CORE_FN, m.OP_LOCK_STATE, locked=False).payload)
        return bool(locked), remaining

    # ---- long operations ------------------------------------------------------------------------
    def status(self, activity: int) -> m.Result:
        return self.request(m.CORE_FN, m.OP_STATUS, struct.pack("<H", activity), locked=False)

    def cancel(self, activity: int) -> None:
        self.request(m.CORE_FN, m.OP_CANCEL, struct.pack("<H", activity))

    def wait(self, activity: int, between: Callable[[], None] = lambda: None) -> tuple[m.Result, list[tuple[int, int]]]:
        """Poll status until the activity completes; returns the final result and the progress seen."""
        progress = []
        while True:
            r = self.status(activity)
            if r.resolution != m.ACCEPTED:
                return r, progress
            progress.append(struct.unpack("<II", r.payload[:8]))
            between()
