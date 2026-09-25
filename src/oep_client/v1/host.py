"""Host side of the v1 session rules, over any `send(request bytes) -> result bytes` transport.

The host picks a random u32 session id for every open (never a counter: after a probe reboot a counter would
start again and match an old process's id). A one-shot CLI keeps the id between commands; a request that goes
through with the same id proves nobody else operated the probe in between (oep-spec session-and-exclusivity).

v1 wire §2: role 0x81 (a session id in the header) goes only to a probe whose confirm answered revision 1 or more; a
v0 probe drops the unknown role without an answer. The host confirms before its first open or session request.
§3: when the probe's boot_id changes (open, heartbeat), or a probe with boot_id 0 answers no session, every
connection and the plan are gone: `epoch` counts those losses, so a client holding a connection can tell.
"""

from __future__ import annotations

import random
import struct
from dataclasses import dataclass, field
from typing import Callable

from . import message as m
from .message import OepError, ProtocolError, ShortPayload  # noqa: F401  (re-exported: callers use host.*)

MIN_REVISION = MAX_REVISION = 1          # the v1 shapes this client speaks


class Rejected(OepError):
    """The probe refused the request (resolution rejected): unknown fn / op, malformed, unavailable, locked..."""

    def __init__(self, result: m.Result):
        super().__init__(result.describe())
        self.result = result


class Failed(OepError):
    """The probe ran the request and it did not work (completed, outcome failed or partial), or answered with a
    resolution or outcome this host does not know (a failure too, v1 wire §0)."""

    def __init__(self, result: m.Result | None, why: str = ""):
        super().__init__(why or (result.describe() if result is not None else "failed"))
        self.result = result


class NotV1(OepError):
    """The probe does not speak v1 (confirm answered revision 0, or refused the ranged confirm as malformed)."""


class Locked(Rejected):
    @property
    def remaining_ms(self) -> int:
        return struct.unpack("<I", self.result.payload[:4])[0]


class NoSession(Rejected):
    pass


class Busy(Rejected):
    pass


class NoConnection(Rejected):
    """The probe does not know the connection (never attached, probe restarted, lost to a wire or target reset):
    attach again."""


class Unsupported(Rejected):
    """A critical TLV (`tag`) or a fixed-part value (tag None) the probe cannot handle."""

    @property
    def tag(self) -> int | None:
        return self.result.payload[0] if self.result.payload else None


_REJECTS = {m.LOCKED: Locked, m.NO_SESSION: NoSession, m.BUSY: Busy, m.NO_CONNECTION: NoConnection,
            m.UNSUPPORTED: Unsupported}


def rejection(result: m.Result) -> Rejected:
    return _REJECTS.get(result.detail, Rejected)(result)


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
    # The link's pipelining (SerialLink.exchange bound to the probe's in-flight / window limits); None: one at a time.
    exchange: Callable[[list[bytes]], list[bytes]] | None = None
    revision: int | None = None            # confirm's answer; None until asked
    limits: dict | None = None             # confirm's answer as a dict
    epoch: int = 0                         # +1 whenever every connection and the plan are lost (§3)
    subscriptions: set = field(default_factory=set)   # fns subscribed in this session (a resync stops them blind)
    _corr: int = 0
    _fns: dict = field(default_factory=dict)   # interface name -> fn, valid until the probe reboots (boot_id)
    _revisions: dict = field(default_factory=dict)   # fn -> interface revision from list
    _boot_id: int | None = None

    def next_corr(self) -> int:
        self._corr = self._corr % 0xFFFF + 1
        return self._corr

    def call(self, fn: int, op: int, payload: bytes = b"", *, locked: bool = True) -> m.Result:
        """request() that also raises Failed unless the probe says it worked: what every operation wants."""
        r = self.request(fn, op, payload, locked=locked)
        if not r.succeeded:
            raise Failed(r)
        return r

    def _session_for(self, locked: bool) -> int | None:
        if not locked or self.session is None:
            return None
        self.require_v1()
        return self.session

    def request(self, fn: int, op: int, payload: bytes = b"", *, locked: bool = True) -> m.Result:
        """locked=True sends the session id (role 0x81) once a session is open; lock-free requests may leave it off.
        Rejections raise; any other answer is returned (Result.succeeded / .ran say what it was)."""
        req = m.Request(self.next_corr(), fn, op, payload, self._session_for(locked))
        result = m.Result.unpack(self.send(req.pack()))
        if result.corr != req.corr:
            raise ProtocolError(f"result for correlation {result.corr}, expected {req.corr}")
        if result.resolution == m.REJECTED:
            self._rejected(result)
            raise rejection(result)
        return result

    def _rejected(self, result: m.Result) -> None:
        if result.detail == m.NO_SESSION:
            self.subscriptions.clear()                  # the lock is gone, and the subscriptions with it
            if self._boot_id == 0:
                self._lost()                            # a probe that cannot tell its boots: assume it restarted

    def _lost(self) -> None:
        self.epoch += 1
        self._fns.clear()                               # a rebooted probe may number its interfaces differently
        self._revisions.clear()
        self.subscriptions.clear()

    def boot_id_seen(self, boot_id: int) -> None:
        """A boot_id from an open result or a heartbeat: a change means the probe restarted (§3)."""
        if self._boot_id is not None and boot_id != self._boot_id:
            self._lost()
        self._boot_id = boot_id

    def pipeline(self, requests: list[tuple[int, int, bytes]], exchange: Callable[[list[bytes]], list[bytes]] | None = None,
                 *, locked: bool = True) -> list[m.Result]:
        """Several requests in flight (`exchange` keeps the probe's in-flight and window limits); results in
        order, rejects NOT raised - the caller looks at each result. Without `exchange`, one at a time."""
        session = self._session_for(locked)
        reqs = [m.Request(self.next_corr(), fn, op, payload, session) for fn, op, payload in requests]
        packed = [r.pack() for r in reqs]
        exchange = exchange or self.exchange
        replies = exchange(packed) if exchange else [self.send(p) for p in packed]
        results = [m.Result.unpack(r) for r in replies]
        for req, res in zip(reqs, results):
            if res.corr != req.corr:
                raise ProtocolError(f"result for correlation {res.corr}, expected {req.corr}")
            if res.resolution == m.REJECTED:
                self._rejected(res)
        return results

    def pipeline_calls(self, requests: list[tuple[int, int, bytes]], *, locked: bool = True) -> list[m.Result]:
        """pipeline() for operations that must all work: raises at the first result that was not a success (the
        probe ran every request in order anyway)."""
        results = self.pipeline(requests, locked=locked)
        for r in results:
            if r.resolution == m.REJECTED:
                raise rejection(r)
            if not r.succeeded:
                raise Failed(r)
        return results

    # ---- confirm (§5, §2) -----------------------------------------------------------------------
    def confirm(self, min_rev: int = MIN_REVISION, max_rev: int = MAX_REVISION) -> dict:
        """Ask for a revision in [min_rev, max_rev]. -> {"revision", "flags", "max_frame", "window", "max_inflight"}.
        A v0 probe answers revision 0 in the v0 shape; one that refuses the ranged confirm as malformed is not v1
        either (revision 0 is recorded and the rejection raised). No revision in the range: rejected unsupported."""
        try:
            r = self.request(m.CORE_FN, m.OP_CONFIRM, m.CONFIRM_REQUEST + bytes([min_rev, max_rev]), locked=False)
        except Rejected as e:
            if e.result.detail == m.MALFORMED:
                self.revision = 0
            raise
        if not r.succeeded:
            raise Failed(r)
        rd = m.Reader(r.payload)
        magic, revision = rd.bytes(4), rd.u8()
        if magic != m.CONFIRM_RESULT:
            raise ProtocolError(f"confirm answered magic {magic!r}")
        if revision == 0:                                   # v0: max_frame(16) window(16) max_inflight(8) flags(8)
            max_frame, window, inflight = rd.take("HHB")
            flags = rd.u8() if rd.at < len(rd.data) else 0
            tail = m.Tail()
        else:
            if not min_rev <= revision <= max_rev:
                raise ProtocolError(f"confirm answered revision {revision}, outside the {min_rev}..{max_rev} asked")
            flags, max_frame, window, inflight = rd.take("BHIB")
            tail = rd.tail()
        self.revision = revision
        self.limits = {"magic": magic, "revision": revision, "flags": flags, "max_frame": max_frame, "window": window,
                       "max_inflight": inflight, "tail": tail}
        return self.limits

    def confirmed(self) -> dict:
        """confirm()'s answer, asked once per host."""
        return self.limits if self.limits is not None else self.confirm()

    def require_v1(self) -> None:
        """Before anything in the v1 shapes (role 0x81, open): the probe must have confirmed revision >= 1."""
        if self.revision is None:
            try:
                self.confirm()
            except Rejected as e:
                if self.revision == 0:
                    raise NotV1("the probe refused the ranged confirm (malformed): not a v1 probe") from e
                raise
        if self.revision < 1:
            raise NotV1(f"the probe speaks OEP revision {self.revision}; session requests need revision 1 or more")

    # ---- session --------------------------------------------------------------------------------
    def open(self, lease_ms: int = 0, *, force: bool = False, session: int | None = None) -> Opened:
        """A new random id unless `session` is given (a one-shot CLI resuming its saved id)."""
        self.require_v1()
        sid = session if session is not None else self.rng.randrange(1, 1 << 32)
        r = self.request(m.CORE_FN, m.OP_OPEN, struct.pack("<IIB", sid, lease_ms, int(force)), locked=False)
        if sid != self.session:
            self.subscriptions.clear()
        self.session = sid
        lease, boot_id, resumed = m.Reader(r.payload).take("IIB")
        self.boot_id_seen(boot_id)
        if not resumed:
            self.subscriptions.clear()
        return Opened(lease, boot_id, bool(resumed))

    def end(self) -> None:
        self.request(m.CORE_FN, m.OP_END)
        self.subscriptions.clear()

    def keepalive(self) -> None:
        self.request(m.CORE_FN, m.OP_KEEPALIVE)

    def lock_state(self) -> tuple[bool, int]:
        locked, remaining = m.Reader(self.request(m.CORE_FN, m.OP_LOCK_STATE, locked=False).payload).take("BI")
        return bool(locked), remaining

    # ---- notifications (§4.5) -------------------------------------------------------------------
    def subscribe(self, fn: int, min_bytes: int = 0, max_delay_ms: int = 0) -> None:
        """Events and data pushes from `fn` (fn 0: heartbeats every max_delay_ms, 0 = 1000 ms). Send when min_bytes are
        ready or max_delay_ms after the first byte (0, 0: as soon as there is anything). Ends with the lock."""
        self.call(m.CORE_FN, m.OP_SUBSCRIBE, struct.pack("<HHH", fn, min_bytes, max_delay_ms))
        self.subscriptions.add(fn)

    def unsubscribe(self, fn: int) -> None:
        self.call(m.CORE_FN, m.OP_UNSUBSCRIBE, struct.pack("<H", fn))
        self.subscriptions.discard(fn)

    def blind_stop(self) -> list[bytes]:
        """The requests a resync may send without confirming (§1): unsubscribe every subscription and end the session
        - both harmless when run twice - for when pushes keep the input from going quiet."""
        if self.session is None or not self.revision:
            return []
        out = [m.Request(self.next_corr(), m.CORE_FN, m.OP_UNSUBSCRIBE, struct.pack("<H", fn), self.session).pack()
               for fn in sorted(self.subscriptions)]
        out.append(m.Request(self.next_corr(), m.CORE_FN, m.OP_END, b"", self.session).pack())
        self.subscriptions.clear()
        return out

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
            progress.append(m.Reader(r.payload).take("II"))
            between()
