"""Host side of the v1 session rules, over any `send(request bytes) -> result bytes` transport.

The host picks a random u32 session id for every open (never a counter: after a probe reboot a counter would
start again and match an old process's id). A one-shot CLI keeps the id between commands; a request that goes
through with the same id proves nobody else operated the probe in between (oep-spec session-and-exclusivity).

oep-core §4.1: role 0x81 (a session id in the header) goes only to a probe whose confirm answered revision 1 or more; a
v0 probe drops the unknown role without an answer. The host confirms before its first open or session request.
oep-core §6.5 / §9: when the probe's boot_id changes (confirm, open, heartbeat), when the lease lapsed (rejected
expired, open answering resumed = 2) and when another session came in between, by force or not (rejected no_session), every
connection, stream and the plan this session had are gone: `epoch` counts those losses, so a client holding a
connection can tell. An expired session is never re-opened behind the caller's back: `Expired` is raised and the caller
opens again (host guide §2.5).
"""

from __future__ import annotations

import random
import struct
from dataclasses import dataclass, field
from typing import Callable

from . import message as m, registry as reg
from .message import OepError, ProtocolError, ShortPayload  # noqa: F401  (re-exported: callers use host.*)

MIN_REVISION = MAX_REVISION = 1          # the v1 shapes this client speaks
OWNER = 0x01                             # open's owner TLV, and the same tag after lock_state / rejected locked


class Rejected(OepError):
    """The probe refused the request (resolution rejected): unknown fn / op, malformed, unavailable, locked..."""

    def __init__(self, result: m.Result):
        super().__init__(result.describe())
        self.result = result


class Failed(OepError):
    """The probe ran the request and it did not work (completed, outcome failed or partial), or answered with a
    resolution or outcome this host does not know (a failure too, oep-core §2.4)."""

    def __init__(self, result: m.Result | None, why: str = ""):
        super().__init__(why or (result.describe() if result is not None else "failed"))
        self.result = result


class NotV1(OepError):
    """The probe does not speak v1 (confirm answered revision 0, or refused the ranged confirm as malformed)."""


class Locked(Rejected):
    @property
    def remaining_ms(self) -> int:
        return struct.unpack("<I", self.result.payload[:4])[0]

    @property
    def owner(self) -> str | None:
        """The holder's owner text, when its open gave one (oep-core §6.4)."""
        value = m.Tail.parse(self.result.payload[4:]).get(OWNER)
        return value.decode("utf-8", "replace") if value is not None else None

    def __str__(self) -> str:
        who = f" by {self.owner}" if self.owner else ""
        return f"locked{who} ({self.remaining_ms} ms of its lease left)"


class InUse(OepError):
    """The lock stayed with another session: its holder kept its lease going (named when it gave an owner)."""


class NoSession(Rejected):
    pass


class Expired(Rejected):
    """rejected expired (core §6.2, §9): this session's lease lapsed and the probe swept its resources (plan,
    connections, streams). Nothing is re-opened silently: the caller opens again (resumed = 2 then) and rebuilds what
    it had. `lease_ms`: the lease the session had (None when unknown). A session whose lock another id took by force
    sees locked while that one holds it, then no_session (the probe remembers the last id only) - never expired."""

    def __init__(self, result: m.Result, lease_ms: int | None = None):
        super().__init__(result)
        self.lease_ms = lease_ms

    def __str__(self) -> str:
        lease = f" (lease {self.lease_ms} ms)" if self.lease_ms is not None else ""
        return f"session expired{lease}: the lease lapsed and the probe swept this session's resources - open again"


class Busy(Rejected):
    pass


class NoConnection(Rejected):
    """The probe does not know the connection (never attached, probe restarted, lost to a wire or target reset):
    attach again."""


class Unsupported(Rejected):
    """A critical TLV (`tag`, as received) or a fixed-part value (tag None; the wire says 0x00) the probe cannot handle
    (core §4.3). `tlvs`: what follows, saying which element (channel, index) when the probe knows."""

    @property
    def tag(self) -> int | None:
        p = self.result.payload
        return p[0] if p and p[0] != m.TAG_FIXED else None

    @property
    def tlvs(self) -> list[tuple[int, bytes]]:
        try:
            return m.split_tlvs(self.result.payload[1:])
        except ValueError:
            return []


class Unavailable(Rejected):
    """rejected unavailable (core §4.3): the payload's TLVs say why (each may be missing): cause, the channels it met,
    who holds them (holder_fn, holder_kind). `tlvs` has every TLV, the interface's own (0x40 and up) too."""
    CAUSES = {v: k for k, v in reg.CORE.enum["unavailable_cause"].items()}
    KINDS = {v: k for k, v in reg.CORE.enum["holder_kind"].items()}
    _T = reg.CORE.tlv["unavailable_payload"]

    @property
    def tlvs(self) -> list[tuple[int, bytes]]:
        try:
            return m.split_tlvs(self.result.payload)
        except ValueError:
            return []

    def _first(self, tag: int) -> bytes | None:
        return next((v for t, v in self.tlvs if t & 0x7F == tag), None)

    @property
    def cause(self) -> str | None:
        v = self._first(self._T["cause"])
        return self.CAUSES.get(v[0], str(v[0])) if v else None

    @property
    def channels(self) -> list[int]:
        return [struct.unpack_from("<H", v)[0] for t, v in self.tlvs if t & 0x7F == self._T["channel"] and len(v) >= 2]

    @property
    def holder_fn(self) -> int | None:
        v = self._first(self._T["holder_fn"])
        return struct.unpack_from("<H", v)[0] if v and len(v) >= 2 else None

    @property
    def holder_kind(self) -> str | None:
        v = self._first(self._T["holder_kind"])
        return self.KINDS.get(v[0], str(v[0])) if v else None


_REJECTS = {m.LOCKED: Locked, m.NO_SESSION: NoSession, m.BUSY: Busy, m.NO_CONNECTION: NoConnection,
            m.UNSUPPORTED: Unsupported, m.UNAVAILABLE: Unavailable, m.EXPIRED: Expired}


def rejection(result: m.Result, lease_ms: int | None = None) -> Rejected:
    if result.detail == m.EXPIRED:
        return Expired(result, lease_ms)
    return _REJECTS.get(result.detail, Rejected)(result)


RESUMED = reg.CORE.enum["resumed"]       # open's resumed: 0 new, 1 resumed (resources kept), 2 swept (core §6.4)


@dataclass
class Opened:
    lease_ms: int
    boot_id: int
    resumed: int                 # 0 a new session, 1 the same id with its resources, 2 the same id after a sweep

    @property
    def swept(self) -> bool:
        """The same session id came back after its lease lapsed: its resources are gone (core §9)."""
        return self.resumed == RESUMED["swept"]


@dataclass
class Host:
    send: Callable[[bytes], bytes]
    rng: random.Random = field(default_factory=random.SystemRandom)
    session: int | None = None
    # The link's pipelining (SerialLink.exchange bound to the probe's in-flight / window limits); None: one at a time.
    exchange: Callable[[list[bytes]], list[bytes]] | None = None
    revision: int | None = None            # confirm's answer; None until asked
    limits: dict | None = None             # confirm's answer as a dict
    lease_ms: int | None = None            # the lease the last open gave (named by Expired)
    epoch: int = 0                         # +1 whenever every connection and the plan are lost (core §6.5, §9)
    subscriptions: set = field(default_factory=set)   # fns subscribed in this session (a resync stops them blind)
    # Called with every capture segment read (capture.CaptureRecord): the hook a run recorder hangs on.
    on_capture: list = field(default_factory=list)
    _corr: int = 0
    _fns: dict = field(default_factory=dict)   # interface name -> fn, valid until the probe reboots (boot_id)
    _revisions: dict = field(default_factory=dict)   # fn -> interface revision from list
    _describes: dict = field(default_factory=dict)   # fn -> its describe TLVs (declarations: valid for one boot_id)
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
            raise rejection(result, self.lease_ms)
        return result

    def _rejected(self, result: m.Result) -> None:
        if result.detail in (m.NO_SESSION, m.EXPIRED):
            # expired: the lease lapsed and the probe swept this session's resources (core §9). no_session: another
            # session opened in between (a force among them) and took them over. Either way they are not ours.
            self._swept()

    def _swept(self) -> None:
        """This session's resources (plan, connections, streams, subscriptions) are gone; the probe is the same."""
        self.epoch += 1
        self.subscriptions.clear()

    def _lost(self) -> None:
        """The probe restarted: the resources, and the fn numbers with them."""
        self._swept()
        self._fns.clear()                               # a rebooted probe may number its interfaces differently
        self._revisions.clear()
        self._describes.clear()

    def boot_id_seen(self, boot_id: int) -> None:
        """A boot_id from confirm, an open result or a heartbeat: a change means the probe restarted (core §6.5)."""
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
                raise rejection(r, self.lease_ms)
            if not r.succeeded:
                raise Failed(r)
        return results

    # ---- confirm (§5, §2) -----------------------------------------------------------------------
    def confirm(self, min_rev: int = MIN_REVISION, max_rev: int = MAX_REVISION) -> dict:
        """Ask for a revision in [min_rev, max_rev]. -> {"revision", "flags", "max_frame", "window", "max_inflight",
        "boot_id"}. The boot_id is the probe's for this boot (core §6.5, §7.1): a host without the lock learns of a
        restart from it. A v0 probe answers revision 0 in the v0 shape; one that refuses the ranged confirm as malformed
        is not v1 either (revision 0 is recorded and the rejection raised). No revision in the range: rejected
        unsupported."""
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
        boot_id = None
        if revision == 0:                                   # v0: max_frame(16) window(16) max_inflight(8) flags(8)
            max_frame, window, inflight = rd.take("HHB")
            flags = rd.u8() if rd.at < len(rd.data) else 0
            tail = m.Tail()
        else:
            if not min_rev <= revision <= max_rev:
                raise ProtocolError(f"confirm answered revision {revision}, outside the {min_rev}..{max_rev} asked")
            flags, max_frame, window, inflight, boot_id = rd.take("BHIBI")
            tail = rd.tail()
            self.boot_id_seen(boot_id)
        self.revision = revision
        self.limits = {"magic": magic, "revision": revision, "flags": flags, "max_frame": max_frame, "window": window,
                       "max_inflight": inflight, "boot_id": boot_id, "tail": tail}
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
    def open(self, lease_ms: int = 0, *, force: bool = False, session: int | None = None,
             owner: str | None = None) -> Opened:
        """A new random id unless `session` is given (a one-shot CLI resuming its saved id). lease_ms 0 = the probe's
        default; 1000..60000 are taken as asked. owner: who holds the lock (1-32 bytes), shown to other hosts.
        Opened.resumed: 0 a new session, 1 the same id with its resources kept, 2 the same id after its lease lapsed
        swept them (core §6.4: the host rebuilds its plan and connections; `epoch` moved)."""
        self.require_v1()
        sid = session if session is not None else self.rng.randrange(1, 1 << 32)
        tail = m.tlv(OWNER, owner.encode()[:32]) if owner else b""
        r = self.request(m.CORE_FN, m.OP_OPEN, struct.pack("<IIB", sid, lease_ms, int(force)) + tail, locked=False)
        if sid != self.session:
            self.subscriptions.clear()
        self.session = sid
        rd = m.Reader(r.payload)
        lease, boot_id, resumed = rd.take("IIB")
        rd.tail()
        self.boot_id_seen(boot_id)
        self.lease_ms = lease
        if resumed == RESUMED["swept"]:
            self._swept()
        elif resumed != RESUMED["resumed"]:
            self.subscriptions.clear()
        return Opened(lease, boot_id, resumed)

    def end(self) -> None:
        self.request(m.CORE_FN, m.OP_END)
        self.subscriptions.clear()

    def keepalive(self) -> None:
        self.request(m.CORE_FN, m.OP_KEEPALIVE)

    def lock_state(self) -> tuple[bool, int]:
        rd = m.Reader(self.request(m.CORE_FN, m.OP_LOCK_STATE, locked=False).payload)
        locked, remaining = rd.take("BI")
        rd.tail()
        return bool(locked), remaining

    def lock_owner(self) -> tuple[bool, int, str | None]:
        """lock_state with the holder's owner text (None: it gave none)."""
        p = self.request(m.CORE_FN, m.OP_LOCK_STATE, locked=False).payload
        locked, remaining = m.Reader(p).take("BI")
        value = m.Tail.parse(p[5:]).get(OWNER)
        return bool(locked), remaining, value.decode("utf-8", "replace") if value is not None else None

    def take(self, lease_ms: int = 3000, *, owner: str | None = None, only_way_in: bool = False,
             wait_s: float = 5.0, force: bool = False) -> Opened:
        """open() the way host guide §2 takes the lock. only_way_in: this link is the probe's only transport and a
        serial port opened exclusively - whoever held the lock cannot be there any more, so it is taken by force at
        once. Otherwise the holder's lease is waited out (up to wait_s); a holder that keeps it going raises InUse,
        naming it. force: take it anyway (the user said so)."""
        import time
        if force or only_way_in:
            return self.open(lease_ms, force=True, owner=owner)
        deadline = time.monotonic() + wait_s
        while True:
            try:
                return self.open(lease_ms, owner=owner)
            except Locked as e:
                left = deadline - time.monotonic()
                if left <= 0 or e.remaining_ms / 1000 > left:
                    who = e.owner or "another session"
                    raise InUse(f"the probe is in use by {who} (lease {e.remaining_ms} ms left, kept going)") from e
                time.sleep(min(left, e.remaining_ms / 1000 + 0.05))

    # ---- notifications (§4.5) -------------------------------------------------------------------
    def subscribe(self, fn: int, min_bytes: int = 0, max_delay_ms: int = 0) -> None:
        """Events and data pushes from `fn` (fn 0: heartbeats every max_delay_ms, 0 = 1000 ms). Send when min_bytes are
        ready or max_delay_ms (u32) after the first byte (0, 0: as soon as there is anything). Ends with the lock; an fn
        that emits nothing is rejected unsupported (core §11.3)."""
        self.call(m.CORE_FN, m.OP_SUBSCRIBE, struct.pack("<HHI", fn, min_bytes, max_delay_ms))
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

