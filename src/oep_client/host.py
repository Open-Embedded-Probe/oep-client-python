"""Host side of the v1 session rules, over any `send(request bytes) -> result bytes` transport.

The host picks a random u32 session id for every open (never a counter: after a probe reboot a counter would start
again and match an old process's id). Every request carries a session_id in its header (core §4.1): this session's
id for an op that needs the lock (and for any request while a session is open and `locked` is asked), 0 for a
lock-free request sent outside a session.

No resume (core §6.2, §6.4, §9): when a session's lock ends - by end, by lease expiry, or by another session's
force - the probe releases everything the session created (its plan, its shares of connections and streams, its
subscriptions); nothing passes to the next session and an ended session never continues. A request of an ended
session is rejected no_session (`NoSession`) while the lock is free, locked while another holds it: the caller opens a
new session and builds again. What lasts between sessions is the probe's settings (oep.probe.config) and what the
interfaces keep readable: an attach on a live combination returns the slot's connection, a console open on the same
place and mechanism returns its stream with position and marks.

`epoch` counts the losses of everything this host's session had (core §6.5, §9): an end, a no_session, a boot_id
that changed (confirm, open, a heartbeat the link read) - a client holding a connection can tell. A reboot also drops
the remembered name -> fn mapping and the describes, so they are listed again.

A probe whose confirm answer is outside core §7.1's bounds (max_frame under 64, window under max_frame, max_inflight 0;
C-20), or whose fn 0 describe declares a max_op_ms outside 1..600000 (core §4.4, §7.5; C-47), is not used: `NotUsable`
is raised with the values, and nothing more is sent through this host.
"""

from __future__ import annotations

import contextlib
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


class NotUsable(OepError):
    """The probe declared values a conforming probe never does (core §7.1 confirm's bounds, C-20; §7.5 max_op_ms,
    C-47): this host sends nothing more to it. The message reports the values."""


MAX_OP_MS_MAX = reg.LIMITS["max_op_ms_max"]   # fn 0 describe max_op_ms is 1 to this (core §7.5)


def check_confirm(max_frame: int, window: int, max_inflight: int) -> str:
    """core §7.1 (C-20): max_frame >= min_max_frame (64), window >= max_frame, max_inflight >= 1. -> "" or why not."""
    if max_frame < reg.MIN_MAX_FRAME or window < max_frame or max_inflight < 1:
        return (f"confirm answered max_frame {max_frame}, window {window}, max_inflight {max_inflight}: outside core "
                f"§7.1 (max_frame >= {reg.MIN_MAX_FRAME}, window >= max_frame, max_inflight >= 1); the transport is not used")
    return ""


def check_max_op_ms(value: int) -> str:
    """core §4.4 / §7.5 (C-47): max_op_ms is 1 to max_op_ms_max (600000). -> "" or why the probe is not used."""
    if not 1 <= value <= MAX_OP_MS_MAX:
        return (f"describe of fn 0 declares max_op_ms {value}: outside 1..{MAX_OP_MS_MAX} (core §7.5), so the probe "
                "does not conform and is not used")
    return ""


class Locked(Rejected):
    @property
    def remaining_ms(self) -> int:
        return struct.unpack("<I", self.result.payload[:4])[0]

    @property
    def owner(self) -> str | None:
        """The holder's owner text, when its open gave one (oep-core §6.4)."""
        value = m.Tail.parse(self.result.payload[4:]).get(OWNER)
        return m.shown(value) if value is not None else None      # control characters replaced (core §2.1)

    def __str__(self) -> str:
        who = f" by {self.owner}" if self.owner else ""
        return f"locked{who} ({self.remaining_ms} ms of its lease left)"


class InUse(OepError):
    """The lock stayed with another session: its holder kept its lease going (named when it gave an owner)."""


class NoSession(Rejected):
    """rejected no_session (core §6.2): the request carries a session_id while no session holds the lock - this
    session ended (end, lease expiry, another session's force and then its end) and the probe released everything it
    created. Nothing is re-opened silently: the caller opens a new session and builds again (host guide §9)."""


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

    @property
    def supported(self) -> tuple[int, int] | None:
        """confirm's refusal (core §7.1, C-15): (min, max) of the protocol revisions the probe handles (TLV 0x01
        supported after tag 0x00); None for any other refusal."""
        if self.tag is not None:
            return None
        v = next((v for t, v in self.tlvs if t == SUPPORTED), None)
        return (v[0], v[1]) if v is not None and len(v) >= 2 else None


SUPPORTED = reg.CORE.tlv["unsupported_payload"]["supported"]
CONFIRM_TRANSPORT = reg.CORE.tlv["confirm_answer"]["transport"]


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
            m.UNSUPPORTED: Unsupported, m.UNAVAILABLE: Unavailable}


def rejection(result: m.Result) -> Rejected:
    return _REJECTS.get(result.detail, Rejected)(result)


OWNER_MAX = reg.LIMITS["owner_max_bytes"]


def owner_text(owner: str) -> bytes:
    """open's owner as it may go (core §2.1, §6.4): control characters replaced by '?', then cut to 32 bytes on a
    character boundary (never half a UTF-8 sequence). An owner with nothing left is refused here."""
    clean = "".join("?" if ord(c) < 0x20 or ord(c) == 0x7F else c for c in owner)
    raw = clean.encode("utf-8")
    while len(raw) > OWNER_MAX:
        clean = clean[:-1]
        raw = clean.encode("utf-8")
    if not raw:
        raise ValueError("owner: 1 to 32 bytes of text (core §6.4)")
    return raw


@dataclass
class Opened:
    """open's answer (core §6.4): the lease the probe gave and its boot_id."""
    lease_ms: int
    boot_id: int


@dataclass
class Host:
    send: Callable[[bytes], bytes]
    rng: random.Random = field(default_factory=random.SystemRandom)
    session: int | None = None
    # The link's pipelining (SerialLink.exchange bound to the probe's in-flight / window limits); None: one at a time.
    exchange: Callable[[list[bytes]], list[bytes]] | None = None
    revision: int | None = None            # confirm's answer; None until asked
    limits: dict | None = None             # confirm's answer as a dict
    lease_ms: int | None = None            # the lease the last open gave
    epoch: int = 0                         # +1 whenever every connection and the plan are lost (core §6.5, §9)
    subscriptions: set = field(default_factory=set)   # fns subscribed in this session (a resync stops them blind)
    # Called with every capture segment read (capture.CaptureRecord): the hook a run recorder hangs on.
    on_capture: list = field(default_factory=list)
    _corr: int = 0
    _fns: dict = field(default_factory=dict)   # interface name -> fn, valid until the probe reboots (boot_id)
    _revisions: dict = field(default_factory=dict)   # fn -> interface revision from list
    _describes: dict = field(default_factory=dict)   # fn -> its describe TLVs (declarations: valid for one boot_id)
    _boot_id: int | None = None
    expect_ms: int = 0                     # how long the request going out now may take on the probe (`expecting`)
    # Called before a request takes its corr (the link's keepalive at a raised port_speed rate): what it sends first
    # must carry the lower corr, or the probe takes the request for an old one (core §4.1)
    before_request: Callable[[], None] | None = None
    # Called with confirm's limits after every confirm answer (the link's max_frame for the transfer time, core §4.4)
    on_limits: Callable[[dict], None] | None = None
    unusable: str = ""                     # why this probe is not used (C-20, C-47): set, nothing more is sent
    uptime_ns: int | None = None           # the last heartbeat's uptime (core §11.2, fn 0 kind 0x01)

    @contextlib.contextmanager
    def expecting(self, ms: int):
        """Requests sent inside take up to `ms` on the probe (a run's timeout_ms, a dmi list's waits, an attach's
        hold_ms, a capture's blocking, a save; core §6.1: the probe does not count the lease meanwhile). The link waits
        at least that and a margin for each answer - also at a raised port_speed rate, where an ordinary request waits
        a quarter of the lease. Nested: the longest holds."""
        saved = self.expect_ms
        self.expect_ms = max(saved, int(ms or 0))
        try:
            yield
        finally:
            self.expect_ms = saved

    def next_corr(self) -> int:
        self._corr = self._corr % 0xFFFF + 1
        return self._corr

    def call(self, fn: int, op: int, payload: bytes = b"", *, locked: bool = True, expect_ms: int = 0) -> m.Result:
        """request() that also raises Failed unless the probe says it worked: what every operation wants."""
        with self.expecting(expect_ms):
            r = self.request(fn, op, payload, locked=locked)
        if not r.succeeded:
            raise Failed(r)
        return r

    def _session_for(self, locked: bool) -> int:
        """The header's session_id (core §4.1): this session's id for a request sent `locked` while one is open, else
        0 (a lock-free request outside the session: no session check, the lease untouched)."""
        if not locked or self.session is None:
            return m.NO_SESSION_ID
        self.require_v1()
        return self.session

    def request(self, fn: int, op: int, payload: bytes = b"", *, locked: bool = True, expect_ms: int = 0,
                session: int | None = None) -> m.Result:
        """locked=True sends this session's id once a session is open; locked=False sends session_id 0 (a lock-free op
        only). `session`: the id to send instead (open: the id it opens). expect_ms: how long this request may take on
        the probe (`expecting`). Rejections raise; any other answer is returned (Result.succeeded / .ran say what it
        was)."""
        self.require_usable()
        if self.before_request is not None:
            self.before_request()
        sid = session if session is not None else self._session_for(locked)
        req = m.Request(self.next_corr(), fn, op, payload, sid)
        with self.expecting(expect_ms):
            result = m.Result.unpack(self.send(req.pack()))
        if result.corr != req.corr:
            raise ProtocolError(f"result for correlation {result.corr}, expected {req.corr}")
        if result.resolution == m.REJECTED:
            self._rejected(result)
            raise rejection(result)
        return result

    def _rejected(self, result: m.Result) -> None:
        if result.detail == m.NO_SESSION and self.session is not None:
            # no session holds the lock: this one ended (lease expiry, or a force and then the forcing session's end)
            # and the probe released what it created (core §6.2, §9). The host is out of a session.
            self.session = None
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

    def heartbeat_seen(self, boot_id: int, uptime_ns: int) -> None:
        """fn 0's heartbeat event (core §11.2: boot_id, uptime_ns), as the link reads it: the boot_id is watched like
        confirm's and open's, the uptime kept (`uptime_ns`)."""
        self.boot_id_seen(boot_id)
        self.uptime_ns = uptime_ns

    def not_usable(self, why: str) -> None:
        """Stop using this probe (C-20, C-47): every later request raises NotUsable with `why`."""
        self.unusable = why
        raise NotUsable(why)

    def require_usable(self) -> None:
        if self.unusable:
            raise NotUsable(self.unusable)

    def pipeline(self, requests: list[tuple[int, int, bytes]], exchange: Callable[[list[bytes]], list[bytes]] | None = None,
                 *, locked: bool = True) -> list[m.Result]:
        """Several requests in flight (`exchange` keeps the probe's in-flight and window limits); results in
        order, rejects NOT raised - the caller looks at each result. Without `exchange`, one at a time."""
        self.require_usable()
        session = self._session_for(locked)
        if self.before_request is not None:
            self.before_request()
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
    def confirm_range(self) -> tuple[int, int]:
        """The revisions a confirm asks for (core §7.1, C-15): the range this client handles before the first confirm,
        then min_rev = max_rev = the revision in use, in every later confirm (a resync, probing again) - so a later
        probe never switches the revision in the middle of a session."""
        if self.revision:
            return self.revision, self.revision
        return MIN_REVISION, MAX_REVISION

    def confirm_body(self) -> bytes:
        """The confirm request's payload with `confirm_range()` (what the link's own confirms send)."""
        return m.CONFIRM_REQUEST + bytes(self.confirm_range())

    def confirm(self, min_rev: int | None = None, max_rev: int | None = None) -> dict:
        """Ask for a revision in [min_rev, max_rev] (default `confirm_range()`). -> {"revision", "flags", "max_frame",
        "window", "max_inflight", "boot_id", "transport", "tail"}. The boot_id is the probe's for this boot (core §6.5,
        §7.1): a host without the lock learns of a restart from it. transport: the index (fn 0's describe) of the
        transport this confirm came on (core §7.1, C-05; 0xFF from a relaying broker; None when the probe sent none).
        A v0 probe answers revision 0 in the v0 shape; one that refuses the ranged confirm as malformed is not v1 either
        (revision 0 is recorded and the rejection raised). No revision in the range: rejected unsupported (Unsupported:
        `.supported` says the probe's range)."""
        lo, hi = self.confirm_range()
        min_rev = lo if min_rev is None else min_rev
        max_rev = hi if max_rev is None else max_rev
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
            why = check_confirm(max_frame, window, inflight)
            if why:
                self.not_usable(why)                        # sends nothing more and reports the values (C-20)
            self.boot_id_seen(boot_id)
        self.revision = revision
        where = tail.get(CONFIRM_TRANSPORT)
        self.limits = {"magic": magic, "revision": revision, "flags": flags, "max_frame": max_frame, "window": window,
                       "max_inflight": inflight, "boot_id": boot_id, "transport": where[0] if where else None,
                       "tail": tail}
        if self.on_limits is not None:
            self.on_limits(self.limits)
        return self.limits

    def confirmed(self) -> dict:
        """confirm()'s answer, asked once per host."""
        return self.limits if self.limits is not None else self.confirm()

    def require_v1(self) -> None:
        """Before anything in the v1 shapes (a session's requests, open): the probe must have confirmed revision >= 1."""
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
    def open(self, lease_ms: int = 0, *, force: bool = False, owner: str | None = None) -> Opened:
        """Open a new session under a new random id (core §6.4; the id goes in the header, never 0). lease_ms 0 = the
        probe's default; 1000..60000 are taken as asked. owner: who holds the lock (1-32 bytes), shown to other hosts.
        force: take the lock from another session (its resources are released first). A session this host had open
        is left behind: it ended, or the probe refuses this open locked while it holds the lock - end it first. The
        answer's boot_id is watched (core §6.5: a reboot drops the fn mapping)."""
        self.require_v1()
        sid = self.rng.randrange(1, 1 << 32)                # random, never 0 (core §6.1)
        tail = m.tlv(OWNER, owner_text(owner)) if owner else b""
        r = self.request(m.CORE_FN, m.OP_OPEN, struct.pack("<IB", lease_ms, int(force)) + tail, session=sid)
        rd = m.Reader(r.payload)
        lease, boot_id = rd.take("II")
        rd.tail()
        rebooted = self._boot_id is not None and boot_id != self._boot_id
        if self.session is not None and not rebooted:
            self._swept()                                   # nothing of the last session is ours (core §9)
        self.subscriptions.clear()
        self.session = sid
        self.boot_id_seen(boot_id)                          # a reboot: one loss, and the names listed again
        self.lease_ms = lease
        return Opened(lease, boot_id)

    def end(self) -> None:
        """End the session: the probe releases the lock and everything the session created (core §6.4, §9) - its
        plan, its shares of connections and streams, its subscriptions. A resent end is answered from the probe's
        resend table (core §5.2)."""
        self.request(m.CORE_FN, m.OP_END)
        self.session = None
        self._swept()

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
        return bool(locked), remaining, m.shown(value) if value is not None else None

    def take(self, lease_ms: int = 3000, *, owner: str | None = None, only_way_in: bool = False,
             wait_s: float = 5.0, force: bool = False) -> Opened:
        """open() the way host guide §6 takes the lock. only_way_in: this link is the probe's only transport and a
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
        self.session = None                                 # the blind end ends it: nothing of it lasts (core §9)
        self.epoch += 1
        return out

