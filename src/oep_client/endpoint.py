"""The virtual bench's probe endpoint that speaks OEP v1, answering whole messages (no hardware).

This is the spec side's "working spec": ch32rv, the JS client, this client and the probe firmware are checked against
it. It wraps a `virtual_bench.VirtualProbe` (the static declarations, and what the probe keeps to itself: `own_channels`,
`Offered.inner`) and does what oep-spec docs/oep-core.ja.md, docs/oep-transports.ja.md and interfaces/*.ja.md
define (oep-spec a09fa45):

- the core (fn 0) has no name and is never listed (core §0, §7.2); its ops are exactly the eight mandatory ones:
  confirm, list, describe, clock, open, end, keepalive, lock_state (core §1.2, §12). list takes first(u16) alone and
  pages every interface (core §7.2); describe answers declarations only (§7.3). clock (core §7.6) answers boot_id and
  uptime_ns, read while handling the request - lock-free, and with session_id 0 it touches no session, lock or lease
- one request header of 10 bytes with session_id (0 = no session; core §4.1), TLVs as tag(u8) len(u16) value (§2.2),
  every sequence count x element with no element length and every fixed form closed (§2.3)
- request tails (core §2.3, `Take`): a tag this probe does not implement in the op's context is ignored, or refused
  unsupported (the tag as received) when critical; a tag it implements is checked the same with or without bit 7 - a
  value of another length (longer too) or one the definition excludes is malformed, a value the definition leaves
  unused or this probe does not handle is unsupported with the tag as received; a non-repeating tag sent twice: the
  first is used; TLV number 0 is malformed. `tail=` appends TLVs to every result that may carry them, so hosts can
  be checked to skip what they do not know
- refusals (core §4.3): the header (unknown_function, unknown_operation, session_required), then the resend table,
  then the session (no_session / locked + remaining ms + owner); after that every check runs before anything changes
  and the request is refused for one reason that applies (this virtual bench's handlers check the form, then the fns named, then
  what they do not handle, then the state and the resources). unsupported is always `tag [TLV]` (0x00 for a fixed-part
  value); unavailable carries cause, channel and (capture-group) fn; a resource number of the wrong kind is unavailable
  cause 6 - connections and streams share one u32 space, never re-used within a boot (core §9)
- confirm with a revision range (its answer carries the boot_id and the transport it came on, core §7.1);
  `revision=0` makes a v0 probe that answers in the v0 shape and drops requests with a session_id
- the ops tag (core §1.2, §7.4): every fn's describe carries it, and an op it does not set is unknown_operation
  (`offers`)
- the lock (core §6): a host-chosen session id, extended by every answer to its holder that passed the session check
  (rejected ones too), counted from when that request completed; no resume: end, a lapse and force release everything
  the session created (its plan, its shares of connections and streams, its subscriptions, its capture-group bind);
  lease 0 = the probe default, 1000-60000 ms taken as asked; open's force is a boolean (non-zero = true, §2.1); the
  owner TLV (1-32 bytes) from the open that takes the lock, shown by lock_state and by rejected locked
- replay (core §5.1): the last corr of the last successfully opened session, and a bounded record of its recent
  requests (the whole request bytes and the answer; longer than `remember_max` bytes are not kept -> result_lost). A
  remembered corr with other bytes is malformed; a corr not remembered is new when it is 1 to 32767 ahead of the last
  (mod 65536), else result_lost. Open goes through the same record; only a successful open of another id starts a new
  replay state, and end or a lapse keeps it
- oep.probe.plan (oep-if-plan: role_assignment, plan_roles), oep.probe.restart (oep-if-restart §2: the answer first,
  then nothing on any transport until the probe has restarted; the target is not reset), notifications (core §10:
  subscribe / unsubscribe are the emitting interface's ops; events at once, data batched by min_bytes / max_delay_ms;
  each fn's go to the connection its subscribe came on, and an open that takes the lock's session back from another
  connection moves them there: `handle(..., link=)`, `pushes_to`),
  oep.probe.link (oep-if-link: source len <= max_frame - 7, sink, and port_speed when the profile's ops offer it)
- port_speed (oep-if-link §3, the handshake): baud(u32) step(u8) verify_ms(u16) on the UART bridge the request came in
  on (another transport, or a try while a port is raised: unavailable cause 6); the nearest rate the virtual bench's UART makes
  (300..5000000 exactly, else the nearest end) within port_speed_tolerance_pct of the request, else unsupported; a try
  goes back after verify_ms without a commit, a commit after port_speed_idle_ms with no good frame (counted from a
  good frame or an answer sent), the session's end after its answer. The line is modelled by `broken_rates` (rate ->
  BrokenRate): frames at such a rate break, from a size and in the directions given, only both ways at once
  (`duplex`), only every Nth (`every`), only once `after` bytes have passed since the switch (`virtual_bench_serial` applies it)
- oep.wire.rvswd / swio (debug §1-§3): scan of the host's named candidates (count 0 or a pair twice malformed; tried
  and found per frame; a live connection's pins refused), attach naming method, pins and max_speed in its fixed part,
  with the reset TLV (an output-idle or disabled reset line unavailable cause 5); attach always makes a new connection
  (busy pins unavailable cause 1, past max_connections cause 2: nothing joined or closed), method 1 only with features
  attach_halt; connections (u32 ids, target_id scheme dmi_7f); attach and scan answer within max_op_ms - a target that
  restarts by itself after a reset (`VirtualTarget.restart_ms`) is waited for up to max_op_ms, then status line and no
  connection (`settle_log`); a target op whose exchanges go unanswered fails status line and leaves the lines undriven
  until one succeeds (`pin_state` "wire-free"); a high-level op whose cleanup fails (step_left, a block's restore:
  `VirtualTarget.restore_fails`) answers, then closes the connection and its streams
- oep.target.riscv-dm (debug §4) on one `VirtualTarget` per pin pair: dmi step lists, halt, resume, reset (status flags
  pc; ndmreset; within max_op_ms), read_block / write_block, run (stopped 0-3: 3 when the preparation fails -
  `VirtualTarget.fail_regs` - or the hart runs), step (step_left)
- oep.target.console (console §1-§3): a session-owned stream opened on a connection (one live stream a connection,
  u32 numbers never reused), released by close, the connection's release or the session's end (a closed number:
  no_resource); marks reset 0x40 (the reset op's ndmreset), restart 0x41, common lost / clear / host; a stream of a
  mechanism that carries host -> target bytes (DMDATA, dmseq) has a send queue of the probe's own size (`send_queue`,
  not declared): a write puts min(count, the free space) at its end (count 0: success; accepted 0 only when full; an
  SDI stream's write is unsupported); the probe hands the queue's head to the target, dmseq 2 bytes and DMDATA 3 a
  poll - one poll per ms while the hart runs, none while it is halted or a riscv-dm request of the connection runs - or
  all of it at `console_take`
- oep.fixture.gpio (fixture §1: modes 0-6, the lines outside through `gpio_world`; the output strength is the probe's),
  oep.fixture.uart (its stream made by the plan; status baud / format),
  oep.fixture.i2c-target (§3, one form: configure an address, a write with data is one frame cut at max_length, reads
  from preload_tx slots or 0xFF, stretch when the ops offer it; test hooks `i2c_write` / `i2c_read`) and
  oep.fixture.spi-target (§4: configure, arm, read_rx, status; errors at most 1 a transfer; `spi_transfer`),
  oep.fixture.logic / analog / capture-group (`virtual_bench_capture`)
- the settings (core §13), fn 0's get / set / unset / save / state / network: each fn's items from its describe, the
  values by (fn, item, key) with no origin, the initial values put in on a boot without a save (`initial`, core §13.4),
  the labels unique but for ASCII case (§13.8); a set checked whole (forms
  malformed, what this probe cannot take unsupported with the value's item index, the whole against what runs); the
  generation counts the changes of one boot from 1; the save by category and item name, applied at boot one value at a
  time (disable first, then idle): what fails is left out (partial, reason 2); a disabled channel is refused everywhere
  with unavailable cause 5 and never parked; `parked` records the free pins' states

Every other non-core fn gets two stand-in operations so the session rules can be exercised - VIRTUAL BENCH ONLY, they mean
nothing on a real probe:  0x01 write(u32) changes state, 0x02 read -> u32 needs no lock.
"""

from __future__ import annotations

import re
import secrets
import struct
import zlib
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Callable, Hashable

from . import catalog, virtual_bench, virtual_bench_capture, message as m, registry as reg
from . import registry as current_reg     # TRANSITIONAL: the common stream marks of the interfaces that follow a09fa45

TOY_WRITE, TOY_READ = 0x10, 0x11                    # 0x01 / 0x02: subscribe / unsubscribe in every op space
OK, WAIT, LINE, FAULT, TIMEOUT, STATE = (reg.STATUS[k] for k in ("ok", "wait", "line", "fault", "timeout", "state"))
HARTSEL = 0x3FF << 16 | 0x3FF << 6                  # DMCONTROL.hartsello / hartselhi
_RV, _CON, _GPIO, _UART = reg.TARGET_RISCV_DM, reg.TARGET_CONSOLE, reg.FIXTURE_GPIO, reg.FIXTURE_UART
STEP = _RV.enum["dmi_step"]
STEP_ARGS = {STEP["write"]: "BI", STEP["read"]: "B", STEP["poll_reads"]: "BIIH", STEP["wait_us"]: "I",
             STEP["poll_us"]: "BIII"}
MARK = reg.COMMON.enum["mark_kind"]                       # the common stream marks: lost 1, clear 2, host 3 (common §1.3)
UART_MARK = MARK
CON_MARK = _CON.enum["mark_kind"]                          # the console's own: reset 0x40, restart 0x41 (console §2)
MARK_RESET, MARK_RESTART = _CON.enum["mark_detail_reset"], _CON.enum["mark_detail_restart"]
ATTACH_FLAGS = reg.WIRE_RVSWD.enum["attach_flags"]
RUN_STOPPED = _RV.enum["run_stopped"]
RESET_MODE = _RV.enum["reset_mode"]
READ_FROM = reg.COMMON.enum["read_from"]                  # position, oldest, now, last_mark (common §1.2)
STEP_LEFT = _RV.tlv["step_answer"]["step_left"]            # step's answer: the hart could not be halted again (§4.2)
# the settings (core §13): fn 0's ops, the items' forms, key kinds and origins, the core's and the wires' choices
_SET_OP = reg.CORE.op
FORM, KEY_KIND = reg.CORE.enum["setting_form"], reg.CORE.enum["setting_key_kind"]
SETTING_SECRET = reg.CORE.enum["setting_flag"]["secret"]
ITEM_TLV = reg.CORE.tlv["unsupported_payload"]["item"]
IDLE_MODE = reg.CORE.enum["channel_idle"]
REST_MODE = reg.WIRE_RVSWD.enum["rest"]
IPV4_MODE = reg.CORE.enum["ipv4_mode"]
IPV4_ITEMS = ("ipv4.{}.mode", "ipv4.{}.address", "ipv4.{}.prefix", "ipv4.{}.gateway", "ipv4.{}.dns")
NET_STATE, NET_REASON = reg.CORE.enum["network_state"], reg.CORE.enum["network_reason"]


@dataclass(frozen=True)
class SettingItem:
    """An item as its owner's describe declares it (setting, setting_choice: core §13.2)."""
    name: str
    form: int
    key_kind: int
    keys: int
    max_len: int
    flags: int
    choices: dict = field(default_factory=dict)        # value -> name (enum)


def setting_items(decl: list[tuple[int, bytes]]) -> list[SettingItem]:
    """An fn's items from its describe TLVs, in order (the k-th is number k)."""
    items: list[SettingItem] = []
    for tag, v in decl:
        if tag == virtual_bench.SETTING:
            form, kind, keys, max_len, flags = struct.unpack_from("<BBHBB", v)
            items.append(SettingItem(v[6:].decode(), form, kind, keys, max_len, flags, {}))
        elif tag == virtual_bench.SETTING_CHOICE and 1 <= v[0] <= len(items):
            items[v[0] - 1].choices[v[1]] = v[2:].decode()
    return items
WIRES = ("oep.wire.rvswd", "oep.wire.swio")
OWNER = reg.CORE.tlv["open"]["owner"]
ROLE_ASSIGNMENT = reg.PROBE_PLAN.tlv["plan_apply"]["role_assignment"]   # the tag number (0x10); hosts send it critical
OP_PLAN_APPLY, OP_PLAN_RELEASE = reg.PROBE_PLAN.op["plan_apply"], reg.PROBE_PLAN.op["plan_release"]
OP_RESTART = reg.PROBE_RESTART.op["restart"]
CONFIRM_TRANSPORT = reg.CORE.tlv["confirm_answer"]["transport"]   # confirm's answer: the transport it came on (§7.1)
UNSUPPORTED_SUPPORTED = reg.CORE.tlv["unsupported_payload"]["supported"]   # confirm's refusal: the revisions handled
PIN_ROLE_RESET = reg.WIRE_RVSWD.enum["pin_role"]["reset"]   # the channels an attach's reset TLV may take
TARGET_ID_SCHEME = reg.COMMON.enum["target_id_scheme"]     # one space for the whole probe (oep-if-debug §1)
_I2C, _SPI = reg.FIXTURE_I2C_TARGET, reg.FIXTURE_SPI_TARGET
I2C_FEATURES, SPI_FEATURES = _I2C.enum["features"], _SPI.enum["features"]
TARGET_ROLES = {"oep.fixture.gpio": {1}, "oep.fixture.uart": {1, 2}, _I2C.name: set(_I2C.enum["role"].values()),
                _SPI.name: set(_SPI.enum["role"].values())}   # the plan roles each fixture takes
UART_FORMAT_MASK = 0x1F                                    # the defined format bits (fixture §2)
_T_SCAN, _T_ATTACH, _T_ATTACH_ANSWER, _T_CONNECTIONS_ANSWER = (
    reg.WIRE_RVSWD.tlv[k] for k in ("scan", "attach", "attach_answer", "connections_answer"))
_IDLE_CLOCK = reg.WIRE_RVSWD.enum["idle_clock"]


def rvswd_rest(target_id: int | None) -> int:
    """The rest an rvswd attach without idle_clock uses (debug §3.1): low for a family in the registry's
    idle_low_family (target_id >> target_id_family.shift), high otherwise and when no target_id was read."""
    if target_id is None:
        return _IDLE_CLOCK["high"]
    family = target_id >> reg.WIRE_RVSWD.enum["target_id_family"]["shift"]
    return _IDLE_CLOCK["low"] if family in reg.WIRE_RVSWD.enum["idle_low_family"].values() else _IDLE_CLOCK["high"]
_WIRE_INDEX = reg.WIRE_RVSWD.tlv["unsupported_payload"]["index"]   # scan's candidate index (debug §1)
ATTACH_METHOD = reg.WIRE_RVSWD.enum["attach_method"]
GPIO_MODE_MAX = max(_GPIO.enum["mode"].values())           # 6: the modes revision 1 defines are 0-6 (fixture §1)
OUTPUT_MODES = (_GPIO.enum["mode"]["output_low"], _GPIO.enum["mode"]["output_high"])   # the modes a strength applies to
LINK_SOURCE_OVERHEAD = m.RESULT_HEADER + 2                 # source's len <= max_frame - 7: header and len (oep-if-link §2)
REQUEST_HEADER = m.REQUEST_HEADER                          # role corr fn op session_id: 10 bytes (core §4.1)


def _any_ops(v: bytes) -> set[int]:
    """The ops an ops value's bits name, whatever its encoding (the virtual bench serves a test's broken value as given)."""
    if not v:
        return set()
    return {v[0] + i * 8 + b for i, byte in enumerate(v[1:]) for b in range(8) if byte >> b & 1 and v[0] + i * 8 + b <= 0xFF}


def _role_channels(v: bytes) -> list[int]:
    """The channels of a role_channels value: role(u8) base(u16) bitmap (core §7.4)."""
    return catalog.bitmap_to_channels(struct.unpack_from("<H", v, 1)[0], v[3:])


class _Unanswered(Exception):
    """A target op's exchange got no answer from the wire (debug §2)."""

    def __init__(self, conn):
        self.conn = conn


class Reject(Exception):
    """A rejection. unsupported's payload is always `tag(u8) [TLV]` (core §4.3): without one, 0x00 = a fixed-part value."""

    def __init__(self, reason: int, payload: bytes = b""):
        if reason == m.UNSUPPORTED and not payload:
            payload = bytes([m.TAG_FIXED])
        self.reason, self.payload = reason, payload


def unsupported_fixed(*tlvs: bytes) -> Reject:
    """rejected unsupported for a fixed-part value, with TLVs saying which (channel, index: core §4.3)."""
    return Reject(m.UNSUPPORTED, bytes([m.TAG_FIXED]) + b"".join(tlvs))


_UNA = reg.CORE.tlv["unavailable_payload"]
UNAVAILABLE_CAUSE = reg.CORE.enum["unavailable_cause"]
# fn 0's ops no admission policy refuses (core §4.3): confirm, list, describe, clock, lock_state
PUBLIC_OPS = frozenset((m.CORE_FN, reg.CORE.op[k]) for k in ("confirm", "list", "describe", "clock", "lock_state"))


def unavailable(cause: str | None = None, channel: int | None = None, extra: bytes = b"") -> Reject:
    """rejected unavailable with core §4.3's payload: why and the channel (each optional), then `extra`."""
    body = b""
    if cause:
        body += m.tlv(_UNA["cause"], bytes([UNAVAILABLE_CAUSE[cause]]))
    if channel is not None:
        body += m.tlv(_UNA["channel"], struct.pack("<H", channel))
    return Reject(m.UNAVAILABLE, body + extra)


def wrong_kind() -> Reject:
    """A resource number of another kind (a stream where a connection goes): rejected unavailable cause 6 (core §9)."""
    return unavailable("wrong_state")


def push_due(min_bytes: int, max_delay_ms: int, age_ms: int, pending: int, stopped: bool) -> bool:
    """Whether a streaming capture's pending data goes out now (core §10.3, capture §2.1 rule 2): min_bytes waiting, the
    oldest max_delay_ms old, both 0 (at once), or the capture stopped (its rest without waiting); never nothing."""
    return pending > 0 and (stopped or (min_bytes == max_delay_ms == 0) or (min_bytes > 0 and pending >= min_bytes)
                            or (max_delay_ms > 0 and age_ms >= max_delay_ms))


class Take:
    """Reads a request's fixed part; too short -> rejected malformed. `tail(known)` applies the request-tail rules of
    core §2.3:

    - a TLV past the end of the request -> malformed;
    - an unknown tag (one this probe does not implement in this (fn, op) context) ->
      critical: unsupported with the tag as received, raised once the whole tail has been read (so a malformed one
      later in it wins), or kept for `check()` when `defer` (the handler checks its own malformed cases first); not
      critical: ignored;
    - a known tag is the same with or without bit 7 (`critical` keeps which came with it, `received` gives it back):
      a non-repeating one twice -> the first is used; `fixed(got, tag, size)` checks its length (a value of another
      length -> malformed), `refuse(tag)` an unhandled value (-> unsupported with the tag as received)."""

    def __init__(self, payload: bytes):
        self.data, self.at = payload, 0
        self.critical: set[int] = set()
        self.repeated: list[tuple[int, bytes]] = []
        self.unknown_critical: int | None = None

    def take(self, fmt: str):
        size = struct.calcsize("<" + fmt)
        if self.at + size > len(self.data):
            raise Reject(m.MALFORMED)
        v = struct.unpack_from("<" + fmt, self.data, self.at)
        self.at += size
        return v if len(v) > 1 else v[0]

    def bytes(self, n: int) -> bytes:
        if self.at + n > len(self.data):
            raise Reject(m.MALFORMED)
        out = self.data[self.at:self.at + n]
        self.at += n
        return out

    def tail(self, known: set[int] = frozenset(), repeats: set[int] = frozenset(),
             defer: bool = False) -> dict[int, bytes]:
        """-> known tags without the critical bit -> value (the first of a non-repeating tag sent twice)."""
        rest, at, got = self.data[self.at:], 0, {}
        self.critical = set()                                      # known tags that came with the critical bit
        self.repeated = []                                         # every known TLV in order, critical bit kept
        while at < len(rest):
            if at + m.TLV_HEADER > len(rest):
                raise Reject(m.MALFORMED)
            tag, n = struct.unpack_from("<BH", rest, at)          # tag(u8) len(u16) (core §2.2)
            at += m.TLV_HEADER
            if at + n > len(rest):
                raise Reject(m.MALFORMED)
            value = rest[at:at + n]
            at += n
            number = tag & 0x7F
            if number == 0:
                raise Reject(m.MALFORMED)                          # TLV number 0, critical bit or not (core §2.2)
            if number == reg.TAG_NAMED_EXTENSION and (not value or value[0] + 1 > len(value)):
                raise Reject(m.MALFORMED)                          # a name_len past its value, critical or not (core §2.2)
            if number in known:
                self.repeated.append((tag, value))                 # later copies are checked too (core §2.3)
                if number in got and number not in repeats:
                    continue                                       # the first is used (core §2.3)
                got.setdefault(number, value)
                if tag & m.TAG_CRITICAL:
                    self.critical.add(number)
            elif tag & m.TAG_CRITICAL:
                if self.unknown_critical is None:
                    self.unknown_critical = tag
            # an unknown tag without the critical bit: ignored (core §2.3)
        self.at = len(self.data)
        if not defer:
            self.check()
        return got

    def check(self) -> None:
        """An unknown critical TLV the tail held: rejected unsupported, the tag as received (core §2.3)."""
        if self.unknown_critical is not None:
            raise Reject(m.UNSUPPORTED, bytes([self.unknown_critical]))

    def received(self, tag: int) -> int:
        """A known tag as the request sent it (the critical bit kept): what an unsupported payload names."""
        return tag | (m.TAG_CRITICAL if tag in self.critical else 0)

    def fixed(self, got: dict[int, bytes], tag: int, size: int) -> bytes | None:
        """A known TLV of a fixed `size`: a value of another length -> malformed (core §2.3: an implemented TLV is
        checked with or without bit 7); None when absent."""
        value = got.get(tag)
        if any(t & 0x7F == tag and len(v) != size for t, v in self.repeated):
            raise Reject(m.MALFORMED)                              # the first and every later copy (core §2.3)
        return value

    def refuse(self, tag: int, payload: bytes = b"") -> Reject:
        """A known TLV whose value this probe does not handle (a value the definition left unused, or one this probe
        does not declare): rejected unsupported with the tag as received (core §2.3), then `payload` (channel, index)."""
        return Reject(m.UNSUPPORTED, bytes([self.received(tag)]) + payload)


@dataclass
class VirtualTarget:
    """A RISC-V hart behind a debug module, as far as the riscv-dm operations see it."""
    halted: bool = False
    dpc: int = 0x100
    reset_vector: int = 0
    mem: dict = field(default_factory=dict)            # word address -> value
    dmi: dict = field(default_factory=dict)            # DMI address -> value (what a read returns)
    dmi_reads: dict = field(default_factory=dict)      # DMI address -> list of values the next reads return
    fail_write: set = field(default_factory=set)       # DMI addresses whose write fails on the line
    fault_at: set = field(default_factory=set)         # word addresses a block access faults on
    regs: dict = field(default_factory=dict)           # regno -> value
    havereset: bool = True
    resume_misses: int = 0                             # resumes that do not take (status state, dpc unchanged)
    present: bool = True                               # something answers on this pin pair (scan, attach)
    needs_drive: bool = False                          # loses its halt once the probe stops driving the line (a CH32L103)
    target_id: int | None = None                       # the dmi_7f target_id attach reports (None: none)
    # run(pc, regs) -> (stopped, dpc, elapsed_us); default: halts 0x10 past the start
    run_hook: Callable | None = None
    unstoppable: bool = False                          # run: the limit passes and the hart cannot be halted (stopped 2)
    fail_regs: set = field(default_factory=set)        # run: regnos whose write before the run fails on the line - the
                                                       # preparation fails, the hart is not run (stopped 3, debug §4.4)
    reset_line: int | None = None                      # the channel wired to its reset (None: any reset channel resets it)
    silent_until_reset: bool = False                   # answers nothing on the wire until a reset through its line
    version: int = 2                                   # DMSTATUS.version: "found" is 2 or more and not 15 (debug §1)
    dmstatus_bits: int = 0                             # more DMSTATUS bits a read shows (impebreak 0x400000, ...)
    restore_fails: bool = False                        # read_block / write_block: restoring DATA0 / DATA1 fails on the
                                                       # line - the answer, then the connection closes (debug §4)
    halt_stuck: bool = False                           # halt: allhalted never comes (status timeout, haltreq cleared)
    step_stuck: str | None = None                      # step: the hart does not come back - "halts" (to haltreq) / "runs"
    restart_ms: int = 0                                # after a reset's release it restarts by itself: the DM is silent
                                                       # this long (a bootloader on the way; debug §3, §4.3)
    haltreq: bool = False                              # what the probe left in DMCONTROL.haltreq
    dcsr_step: bool = False                            # what the probe left in dcsr.step
    # A debug link that drops (a CH32L103 after a change of hart state, through an RVSWD probe): from the DMI access
    # (each read and write of a dmi step, counted in dmi_accesses from 0) whose index is a key of drop_at, writes are
    # lost and reads give the last value read ("stale"), all ones ("ones"), or all ones with a DMSTATUS read failing
    # on the line ("ones_line": the reference probe takes a DMSTATUS of all ones for no answer). The drop lasts until
    # drop_requests later dmi requests have begun (the probe brings an idle link back before its next transaction);
    # within one request it never ends by itself. "glitch" is one access missed and the link up again at once (a link
    # coming back from a drop through a flicker): that write lost, or that read giving the last value read.
    # "glitch_parity" is the same, a write lost with cmderr 6 set in ABSTRACTCS when none is (QingKe: the module took the
    # frame for one with a bad parity).
    drop_at: dict = field(default_factory=dict)        # access index -> "stale" / "ones" / "ones_line" / "glitch" /
                                                       # "glitch_parity"
    drop_requests: int = 1
    dmi_accesses: int = 0
    dropped: str | None = None                         # the mode of the drop now in force (None: the link is up)
    drop_left: int = 0
    last_read: int = 0
    lost_writes: int = 0
    glitch: bool = False                               # this access is a "glitch" one
    glitch_parity: bool = False                        # ... a "glitch_parity" one
    # The DMI writes an rvswd / swio scan or attach makes before its answer (debug §1, §3), in order: the set-up pair
    # DMI 0x7E / 0x7D = 0x5AA50400 twice, dmactive when DMCONTROL does not read it, then (attach only, the speed check)
    # ABSTRACTAUTO = 0 and PROGBUF0 written, read back and given back its value. Kept apart from dmi_accesses.
    bringup: list = field(default_factory=list)        # (DMI address, value) per write

    def bring_up(self, scratch: bool) -> bool:
        """The DMI writes of debug §1 / §3 before the speed is checked, and with `scratch` the check itself on PROGBUF0
        (freed by ABSTRACTAUTO = 0, which is not given back). -> False when PROGBUF0's write does not take (its address
        in fail_write): no speed reads back what it wrote and the value cannot be given back - attach fails."""
        for _ in range(2):
            self.bringup += [(0x7E, 0x5AA50400), (0x7D, 0x5AA50400)]
        if not self.dmi.get(0x10, 1) & 1:                     # dmactive only when DMCONTROL does not read it (haltreq 0)
            self.dmi[0x10] = 1
            self.bringup.append((0x10, 1))
        if not scratch:
            return True
        self.bringup.append((0x18, 0))
        if 0x18 in self.dmi:
            self.dmi[0x18] = 0
        saved = self.dmi.get(0x20, 0)                          # read before the check, written back after it
        self.bringup.append((0x20, ~saved & 0xFFFFFFFF))
        if 0x20 in self.fail_write:
            return False
        self.bringup.append((0x20, saved))
        return True

    @property
    def answers(self) -> bool:
        """Something answers on the wire now (scan, attach): present, and not stuck until a reset."""
        return self.present and not self.silent_until_reset

    @property
    def found(self) -> bool:
        """scan's "found" (debug §1): DMSTATUS.version 2 or more and not 15."""
        return self.version >= 2 and self.version != 15

    def resets_through(self, channel: int) -> bool:
        return self.reset_line in (None, channel)

    def dmstatus(self) -> int:
        return (0x80 | (self.version & 0xF) | ((0x300 if self.halted else 0xC00)) | (0xC0000 if self.havereset else 0)
                | self.dmstatus_bits)

    def begin_dmi(self) -> None:
        """A dmi request begins: a drop in force counts it, and ends once drop_requests requests have begun."""
        if self.dropped:
            self.drop_left -= 1
            if self.drop_left <= 0:
                self.dropped = None

    def _access(self) -> None:
        mode = self.drop_at.get(self.dmi_accesses)
        self.dmi_accesses += 1
        self.glitch = mode in ("glitch", "glitch_parity")
        self.glitch_parity = mode == "glitch_parity"
        if mode and not self.glitch:
            self.dropped, self.drop_left = mode, self.drop_requests

    def read_dmi(self, address: int) -> int | None:
        """One DMI read: the register's value, or what a dropped link gives (None: the read failed on the line).
        DMSTATUS and DMCONTROL read as the module has them unless a test set them in `dmi`."""
        self._access()
        if self.glitch:
            return self.last_read
        if self.dropped:
            if self.dropped == "stale":
                return self.last_read
            if self.dropped == "ones_line" and address == 0x11:
                return None
            return 0xFFFFFFFF
        queue = self.dmi_reads.get(address)
        if queue:
            self.dmi[address] = queue.pop(0)
        if address == 0x11 and address not in self.dmi:
            value = self.dmstatus()
        else:
            value = self.dmi.get(address, 1 if address == 0x10 else 0)   # DMCONTROL: dmactive, hart 0
        self.last_read = value
        return value

    def write_dmi(self, address: int, value: int) -> None:
        """One DMI write: lost on a dropped link. DMCONTROL's haltreq / resumereq move the hart (and read back 0);
        ABSTRACTCS's cmderr is write-1-to-clear; COMMAND runs an access-register command (32 bits) on regs / dpc,
        ignored while cmderr is set, cmderr 4 (halt/resume) on a running hart."""
        self._access()
        if self.dropped or self.glitch:
            self.lost_writes += 1
            if self.glitch_parity and not (self.dmi.get(0x16, 0) >> 8) & 7:
                self.dmi[0x16] = self.dmi.get(0x16, 0) | (6 << 8)
            return
        if address == 0x10:
            if value & (1 << 31):
                self.halted = True
            elif value & (1 << 30):
                self.halted = False
            self.dmi[0x10] = value & ~0xD0000000                # haltreq, resumereq, ackhavereset read 0
            return
        if address == 0x16:
            cs = self.dmi.get(0x16, 0)
            self.dmi[0x16] = cs & ~(value & 0x700)
            return
        self.dmi[address] = value
        if address == 0x17:
            self.abstract(value)

    def abstract(self, command: int) -> None:
        cs = self.dmi.get(0x16, 0)
        if (cs >> 8) & 7 or command >> 24:                     # cmderr set: ignored; only access register here
            return
        if not self.halted:
            self.dmi[0x16] = cs | (4 << 8)
            return
        if not command & (1 << 17):                            # no transfer
            return
        regno = command & 0xFFFF
        if command & (1 << 16):                                # write
            value = self.dmi.get(0x04, 0)
            if regno == 0x07B1:
                self.dpc = value
            else:
                self.regs[regno] = value
        else:
            self.dmi[0x04] = self.dpc if regno == 0x07B1 else self.regs.get(regno, 0)


@dataclass
class I2cState:
    """One oep.fixture.i2c-target (fixture §3): what configure made, the frames waiting for read_rx, the tx slots."""
    state: int = 0                       # 0 not configured, 1 running
    address: int = 0
    queue: list = field(default_factory=list)   # (frame, ns)
    rx_frames: int = 0
    errors: int = 0
    tx: list = field(default_factory=list)      # preloaded, unread
    stretch_us: int = 0


@dataclass
class SpiState:
    """One oep.fixture.spi-target (fixture §4): what configure made, the armed transaction, the finished ones."""
    state: int = 0
    mode: int = 0
    bit_order: int = 0
    armed: tuple[int, bytes] | None = None      # (length, MISO bytes) of the one transaction it waits for
    queue: list = field(default_factory=list)   # (bits, MOSI bytes, ns)
    transactions: int = 0
    errors: int = 0


@dataclass
class Stream:
    """A position stream (common §1): bytes from position `base`, marks with serials; a console stream's connection
    and mechanism (console §1)."""
    data: bytearray = field(default_factory=bytearray)
    base: int = 0
    marks: list = field(default_factory=list)          # (serial, position, kind, time_ns, detail)
    serial: int = 0
    written: bytearray = field(default_factory=bytearray)
    queue: bytearray = field(default_factory=bytearray)  # console: the send queue, accepted bytes not handed on yet
    fed_ms: int = 0                                    # console: the last poll that handed queue bytes on (timers' clock)
    conn: int = 0                                      # console: the connection it is on
    mechanism: int = 0
    kinds: dict | None = None                          # the mark kinds this stream uses (None: the console's)

    @property
    def end(self) -> int:
        return self.base + len(self.data)

    def add_mark(self, kind: int, time_ns: int, detail: int = 0) -> None:
        self.marks.append((self.serial, self.end, kind, time_ns, detail))
        self.serial = (self.serial + 1) & 0xFFFFFFFF

    def drop_oldest(self, n: int) -> None:
        del self.data[:n]
        self.base += n


@dataclass
class Connection:
    """A debug connection (common §2): made by a wire's attach, the session's until detach, its end or a lost line."""
    fn: int
    pair: tuple[int, int]
    order: int                                         # creation order (connections lists them in it)
    speed: int = 4_000_000
    tid: int | None = None
    idle_clock: int = 0                                # rvswd: SWCLK while the line rests, 0 high / 1 low
    free: bool = False                                 # an exchange went unanswered: the lines rest undriven (debug §2)




@dataclass
class BrokenRate:
    """How a line rate breaks frames in the virtual bench (port_speed tests): frames of `min_size` bytes and more (on the wire)
    break, towards the host and / or towards the probe. A broken frame towards the probe is a candidate whose CRC does
    not match; towards the host its CRC is spoiled."""
    min_size: int = 0
    to_host: bool = True
    to_probe: bool = True
    duplex: bool = False       # only while both ways carry such frames at once (a request of min_size and more comes in
                               # while an answer of min_size and more is still unread): the request breaks (to_probe),
                               # the unread answer breaks (to_host)
    every: int = 1             # of the frames that would break, only every Nth does (1: all)
    seen: int = 0              # the frames that would have broken so far (`every` counts these)
    after: int = 0             # none breaks until this many bytes (every frame on the wire, both ways) have passed at
                               # the rate since the port last switched to it: a rate that passes a short verify and
                               # breaks later in use
    carried: int = 0           # the bytes passed at the rate since that switch (`after` counts these)

    def hit(self) -> bool:
        """One more frame that would break: True when this one does (`every`)."""
        self.seen += 1
        return self.seen % max(1, self.every) == 0


_LINK = reg.PROBE_LINK
OP_PORT_SPEED = _LINK.op["port_speed"]
OP_PORT_STATE = _LINK.op.get("port_state")
SPEED_STEP = _LINK.enum["port_speed_step"]
SPEED_IDLE_MS = reg.TIMING["port_speed_idle_ms"]   # committed: back to the boot speed after this with no good frame
SPEED_TOLERANCE_PCT = reg.LIMITS["port_speed_tolerance_pct"]   # the UART's nearest rate within this of the request (§3)
SPEED_RATES = (300, 5_000_000)                     # what the virtual bench's UART makes (anything between, exactly; outside it,
                                                   # the nearest end of the range)


class Endpoint:
    MARKS_PER_ANSWER = 3                     # small, so hosts must follow `more`

    def __init__(self, probe: virtual_bench.VirtualProbe, now_ms: Callable[[], int], boot_id: int = 0x1234ABCD,
                 lease_default_ms: int = 3000, lease_max_ms: int = 60000, revision: int = 1, tail: bytes = b"",
                 window: int = 1 << 18, max_inflight: int = 4, remember_max: int = 72, remember_count: int = 8,
                 now_ns: Callable[[], int] | None = None):
        """`now_ms`: the clock the timers run on (leases, retries, port_speed), since this boot. `now_ns`: the same clock
        in ns when it has that resolution (virtual_bench_serve: time.monotonic_ns); without it the probe's clock (core §2.6a)
        is `now_ms` in ns. confirm's limits are checked against core §7.1 (max_frame 64 or more, window max_frame or
        more, max_inflight 1 or more, C-20) and the declared max_op_ms against core §7.5 (1 to max_op_ms_max, C-47):
        a probe outside them is not built."""
        if probe.max_frame < reg.MIN_MAX_FRAME or window < probe.max_frame or max_inflight < 1:
            raise ValueError(f"confirm's limits out of core §7.1's bounds: max_frame {probe.max_frame}, window {window}, "
                             f"max_inflight {max_inflight}")
        self.current_core = None
        self.probe = probe
        self.now = now_ms
        self._clock_ns = now_ns
        self._origin_ns = 0                               # the probe clock's 0: this boot's start (core §2.6a)
        self._last_ns = 0                                 # the clock never goes back while the boot_id stays (C-31)
        self.current_core = None
        self.boot_id = boot_id
        self.lease_default_ms = lease_default_ms
        self.lease_max_ms = lease_max_ms
        self.revision = revision
        self.tail = tail
        self.window, self.max_inflight = window, max_inflight
        self.remember_max = remember_max
        self.remember_count = remember_count            # requests the replay record keeps (core §5.1: the probe's choice)
        self.policy: Callable[[m.Request], bool] | None = None   # TEST HOOK: an admission policy (core §4.3, optional)
        self.executions = 0                            # requests carried out (a completed answer made by an op)
        self._processing: Callable[[], None] | None = None   # process(): the time the request takes, spent once
        self.routes: dict[Hashable, list[list]] = {}   # route -> [request bytes, answer or None] (receive / process / sent)
        self.reads: dict[int, set[int]] = {fn: set(dict(o.inner).get("reads", ())) for o in probe.offered
                                           for fn in (o.fn,)}   # an fn's side-effect-free, lock-free read ops (stand-ins)
        self.names = {o.fn: o.name for o in probe.offered}
        self.decl = {o.fn: [m.split_tlvs(t)[0] for t in o.tlvs] for o in probe.offered}   # fn -> [(tag, value)]
        self.identity = {o.fn: (o.name, o.instance, o.revision) for o in probe.offered}   # what a saved item names
        self.fns = {name: fn for fn, name in sorted(self.names.items(), reverse=True)}   # first fn of each name
        self.static = {o.fn: o.tlvs for o in probe.offered}
        # channels the probe can only read (a test sets them): an idle of mode 3 / 4 there is unsupported (core §13.8)
        self.input_only: set[int] = set()
        # channels without a pull (a test sets them): channel -> the idle modes (1 pull-up / 2 pull-down) it cannot
        # make; such an idle is unsupported (core §13.8)
        self.no_pull: dict[int, set[int]] = {}
        self.channels = 0                               # fn 0 describe 0x43: channels 0 .. channels - 1; absent: none (core §7.5)
        self.own_channels: set[int] = set(probe.own_channels)   # the probe's own (never an interface's; not declared)
        self.transports: dict[int, int] = {}            # index -> kind, by the TLV's own index (core §7.5), not its order
        self.max_op_ms = virtual_bench.MAX_OP_MS
        # oep.probe.plan's plan_roles (oep-if-plan §1): the most role assignments at once (None: no limit declared)
        self.plan_roles: int | None = next((struct.unpack_from("<I", v)[0] for fn, name in self.names.items()
                                            if name == virtual_bench.PLAN for tag, v in self.decl[fn] if tag == virtual_bench.PLAN_ROLES_TAG),
                                           None)
        for tag, v in self.decl.get(0, ()):
            if tag == virtual_bench.CORE_TRANSPORT:
                self.transports[v[0]] = v[1]
            if tag == virtual_bench.CORE_MAX_OP_MS:
                self.max_op_ms = struct.unpack_from("<I", v)[0]
            if tag == virtual_bench.CORE_CHANNELS:
                self.channels = struct.unpack_from("<H", v)[0]
        if not 1 <= self.max_op_ms <= reg.LIMITS["max_op_ms_max"]:
            raise ValueError(f"max_op_ms {self.max_op_ms}: core §7.5 wants 1 to {reg.LIMITS['max_op_ms_max']}")
        self.serial_ports = {i for i, k in self.transports.items() if k in virtual_bench.SERIAL_KINDS}
        self.pairs: dict[int, list[tuple[int, int]]] = {}  # wire fn -> allowed (swdio, swclk), declared order
        self.max_connections: dict[int, int] = {}
        self.reset_channels: dict[int, set[int]] = {}     # wire fn -> channels an attach's reset TLV may take (role 3)
        self.pin_roles: dict[int, dict[int, set[int]]] = {}   # wire fn -> role -> candidates (role_channels wires)
        for fn, name in self.names.items():
            if name in WIRES:
                roles: dict[int, set[int]] = {}
                for tag, v in self.decl[fn]:
                    if tag == catalog.ROLE_CHANNELS and v[0] in (1, 2):
                        roles.setdefault(v[0], set()).update(_role_channels(v))
                if roles:
                    self.pin_roles[fn] = roles
                self.pairs[fn] = self._group_pairs(fn, name, roles)
                self.reset_channels[fn] = {
                    c for tag, v in self.decl[fn] if tag == catalog.ROLE_CHANNELS and v[0] == PIN_ROLE_RESET
                    for c in _role_channels(v)}
                self.max_connections[fn] = next((v[0] for tag, v in self.decl[fn] if tag == virtual_bench.MAX_CONNECTIONS), 1)
        self.targets: dict[tuple[int, tuple[int, int]], VirtualTarget] = {
            (fn, p): VirtualTarget() for fn in sorted(self.pairs) for p in self.pairs[fn]}
        for fn in sorted(f for f in self.pin_roles if not self.pairs[f]):   # any pair: one target, on the first pair
            first = self._role_pairs(fn)[0]
            self.targets[(fn, first)] = VirtualTarget()
        self.target = next(iter(self.targets.values()), VirtualTarget())
        self.inner = {o.fn: dict(o.inner) for o in probe.offered}   # what each fn keeps to itself (not declared)
        self.captures: dict[int, virtual_bench_capture.VirtualCapture] = {
            fn: self._capture_from(self.decl[fn], self.inner[fn]) for fn, name in self.names.items()
            if name in ("oep.fixture.logic", "oep.fixture.analog")}
        self.groups: dict[int, virtual_bench_capture.VirtualGroup] = {
            fn: self._group_from(self.decl[fn], self.inner[fn]) for fn, name in self.names.items()
            if name == "oep.fixture.capture-group"}
        self.mechanisms, self.mech_names = set(), []
        for fn, name in self.names.items():
            if name == "oep.target.console":
                self.mech_names = [v.decode() for tag, v in self.decl[fn] if tag == virtual_bench.MECHANISM]
                self.mechanisms |= set(range(1, len(self.mech_names) + 1))   # a named list: the place from 1 (core §7.7)
        self.block_max = {fn: self._own(fn, catalog.MAX_LENGTH, "H", 1 << 16)
                          for fn, name in self.names.items() if name == "oep.target.riscv-dm"}
        self.gpio_allowed = {fn: self._own(fn, virtual_bench.GPIO_MODES, "I", 0xFF)
                             for fn, name in self.names.items() if name == "oep.fixture.gpio"}
        self.uart_formats = {fn: next((set(v[1:1 + v[0]]) for tag, v in self.decl[fn] if tag == virtual_bench.UART_FORMATS), {0})
                             for fn, name in self.names.items() if name == "oep.fixture.uart"}
        self.uart_max_hz = {fn: self._own(fn, catalog.MAX_CLOCK_HZ, "I", 3_000_000)
                            for fn, name in self.names.items() if name == "oep.fixture.uart"}
        own = self._own
        # the fixture targets' declarations (fixture §3 / §4): max_length, features, queue_depth, max_stretch_us
        # (i2c-target only; 0 when not declared)
        self.target_decl = {fn: (own(fn, catalog.MAX_LENGTH, "H", 1), own(fn, catalog.FEATURES, "I", 0),
                                 own(fn, _I2C.tlv["describe"]["queue_depth"], "B", 1),
                                 own(fn, _I2C.tlv["describe"]["max_stretch_us"], "I", 0) if name == _I2C.name else 0)
                            for fn, name in self.names.items() if name in (_I2C.name, _SPI.name)}
        self.spi_modes = {fn: own(fn, virtual_bench.SPI_MODES, "B", 0x0F) for fn, name in self.names.items() if name == _SPI.name}
        self.target_fail_configure: set[int] = set()   # TEST HOOK: i2c / spi targets whose next accepted configure fails
        self.spi_fail_arm: set[int] = set()            # TEST HOOK: spi targets whose next arm cannot prepare MISO
        # i2c-target internal pull-ups (fixture §3): features bit2, else none
        self.i2c_pullups = {fn: bool(self.target_decl[fn][1] & I2C_FEATURES["internal_pullups"])
                            for fn, name in self.names.items() if name == _I2C.name}
        # the settings (core §13): each fn's items (its describe), the firmware's initial values (§13.4), the netifs
        self.setting_items = {fn: setting_items(decl) for fn, decl in self.decl.items() if setting_items(decl)}
        self.initial: dict[tuple[int, str, int], bytes] = {
            (fn, name, key): bytes(d) for fn, name, key, d in self.inner.get(0, {}).get("initial_values", ())}
        # the network (transports §7): the netifs, a named list (numbers from 1); around the bench: Wi-Fi access points
        # (ssid -> passphrase), the Ethernet cable, what DHCP gives; every drop and reconnect of a netif (a value in use changed)
        self.netif_names = [v.decode() for tag, v in self.decl.get(0, ()) if tag == virtual_bench.NETIF]
        self.netif_numbers = list(range(1, len(self.netif_names) + 1))
        self.aps: dict[bytes, bytes] = {b"lab": b"password1"}
        self.ether_link = True
        self.dhcp = {"wifi": (bytes([192, 168, 1, 23]), 24, bytes([192, 168, 1, 1]), bytes([192, 168, 1, 1])),
                     "ethernet": (bytes([10, 0, 0, 23]), 16, bytes([10, 0, 0, 1]), bytes([10, 0, 0, 1]))}
        self.tcp_port = 4242 if reg.CORE.enum["transport_kind"]["tcp"] in self.transports.values() else 0
        self.reconnects = {n: 0 for n in self.netif_numbers}
        self.storage_max = 4096                         # the save's bytes (the probe's own, not declared: core §13.7)
        # the console's send queue (console §2): its size is the probe's (not declared); what a poll hands to the target
        # per mechanism (dmseq 2 bytes, DMDATA 3; SDI carries nothing to the target, §3.1)
        con_fn = self.fns.get(_CON.name)
        self.send_queue = self.inner[con_fn].get("send_queue", virtual_bench.CONSOLE_SEND_QUEUE) if con_fn is not None else 0
        names = getattr(self, "mech_names", [])
        self.console_feed = {names.index(k) + 1: n for k, n in (("dmdata", 3), ("dmseq", 2)) if k in names}
        self.settle_log: list[int] = []                 # every wait for a silent DM after a reset (ms): riscv-dm reset,
                                                        # attach's reset TLV - at most max_op_ms (debug §3, §4.3)
        self.uart_accept = 256
        # the ops each fn's describe declares (core §1.2, §7.4): what `offers` answers (an fn without the tag: none)
        self.ops = {fn: set().union(*(_any_ops(v) for tag, v in decl if tag == catalog.OPS))
                    for fn, decl in self.decl.items()}
        # an ops tag a test gave outside core §7.4's form (a probe that does not conform): served as given
        self.broken_ops = {fn: v for fn, decl in self.decl.items() for tag, v in decl
                           if tag == catalog.OPS and catalog.check_ops(v)}
        # port_speed (oep-if-link §3): on when the profile's oep.probe.link offers it (its ops); the boot speed every revert
        # goes back to. A test turns it off (None) or on: the link fn's ops follow (`offers`, `_declarations`)
        self.link_fn = self.fns.get(_LINK.name)
        self.port_speed_base: int | None = (115200 if self.link_fn is not None and OP_PORT_SPEED in self.ops[self.link_fn]
                                            else None)
        self.broken_rates: dict[int, BrokenRate] = {}   # the line: rates that break frames (virtual_bench_serial applies it)
        # oep.probe.restart (oep-if-restart, optional): the fn the profile lists it at (None: the probe has none -
        # `virtual_bench.without(probe, virtual_bench.RESTART)`), and the restart_max_ms its describe declares (a test may change it)
        self.restart_fn = self.fns.get(virtual_bench.RESTART)
        self.restart_max_ms = next((struct.unpack_from("<I", v)[0] for tag, v in self.decl.get(self.restart_fn, ())
                                    if tag == virtual_bench.RESTART_MAX_MS_TAG), virtual_bench.RESTART_MAX_MS)
        self.reboots = 0                                # restarts so far (the restart op and reboot()): a transport
                                                        # drops what it had read for the old boot when this moves
        self._transport = 0                             # the transport the request being handled came in on
        self._via: Hashable = 0                         # and the connection on it (a TCP listener's accepted ones differ)
        # what the simulation is, not the probe's state (a reboot keeps them):
        self.capture_slipped = False                    # every capture segment says flags bit2 (a pace that fell behind)
        self.capture_fail_start: set[int] = set()       # TEST HOOK: these captures' start fails to initialise (capture §3.2)
        self.uart_clock_hz = 80_000_000                # the UARTs' divider clock (a test lowers it: the item's fallback)
        # the lines outside (a test hook): gpio_world(channel, mode) -> the level an input mode reads (None: the
        # default - gpio_inputs, a pull-up 1); reads see the world as the modes set it (gpio_modes)
        self.gpio_world: Callable[[int, int], int | None] | None = None
        self.config_get_tail = b""                      # TEST HOOK: TLVs after get's items (an extension, core §2.3)
        self._boot()

    def _boot(self) -> None:
        self.restarting = False              # restart answered (oep-if-restart §2): nothing more until reboot()
        self.unanswered = 0                  # requests that came while restarting (no answer, oep-if-restart §2)
        self.holder: int | None = None
        self.session_ports: set[int] = set()   # the transports the lock holder's requests came on (fixture §2.1)
        self.passthrough_out: dict[int, bytearray] = {}   # a serial port's pass-through bytes not sent yet
        self.resident: dict[int, bool] = {}       # console_passthrough's own connection per port: attached (config §1.2)
        self.holds: dict[tuple[int, tuple[int, int]], int] = {}   # (wire fn, pair) -> rest: lines held after a close (debug §2)
        self.last: int | None = None         # S: the id that holds or last held the lock (the resend table's, §5.2)
        self.owner: bytes | None = None      # the holder's owner, while the lock is held (core §6.4)
        self.lease_ms = self.lease_default_ms
        self.expires_ms = 0
        self.values: dict[int, int] = {}
        self.dropped = 0                     # requests a v0 endpoint dropped (any with a session_id)
        self.revision_in_use: dict[int, int] = {}   # transport -> the revision its last confirm chose (core §7.1)
        self.discarded = 0                   # messages discarded unanswered: not a request role, or short (core §2.4)
        self.requests: list[m.Request] = []
        # fn -> (min_bytes, max_delay_ms) of its subscription (core §10.3); ends with the lock
        self.subscribed: dict[int, tuple[int, int]] = {}
        self.push_link: dict[int, Hashable] = {}        # fn -> the connection its notifications go to (core §10.4)
        self.data_since_ms: dict[int, int] = {}         # fn -> when its oldest data not sent yet was there (max_delay_ms)
        self.push_seq: dict[int, int] = {}              # fn -> the next event / data seq (core §10.2)
        # fn -> +1 at every subscribe / unsubscribe: a transport drops what it holds of an older one unsent (§10.3)
        self.sub_epoch: dict[int, int] = getattr(self, "sub_epoch", {})
        self.outbox: list[bytes] = []                   # events and data frames waiting to go out (pushes())
        self.plan: set[tuple[int, int, int]] = set()   # (fn, role, channel), from plan_apply
        # corr -> (the whole request, its answer); None where it was too large to keep (core §5.1)
        self.resend: OrderedDict[int, tuple[bytes | None, bytes | None]] = OrderedDict()
        self.newest_corr: int | None = None
        self.conns: dict[int, Connection] = {}
        self.resources: dict[int, str] = {}            # live number -> "connection" / "stream": one u32 space (core §9)
        self._next_resource = 1
        self._order = 0
        self.streams: dict[int, Stream] = {}           # live console stream id -> stream
        self.stream_order: dict[int, int] = {}         # stream id -> creation order (the streams list's order)
        self.gpio_modes: dict[int, int] = {}
        self.gpio_inputs: dict[int, int] = {}
        self.gpio_log: list[tuple[int, int]] = []
        self.uarts: dict[int, Stream] = {}             # fn -> stream (while its plan has RX or TX)
        self.uart_carry: dict[int, tuple[int, int]] = {}   # fn -> (position, mark serial) a released stream left off at
        self.uart_baud: dict[int, tuple[int, int]] = {}   # fn -> (baud, format) in force (absent: not configured, status 0 0)
        self.uart_fail_configure: set[int] = set()     # TEST HOOK: fns whose next accepted configure fails to come up
        self.i2c: dict[int, I2cState] = {fn: I2cState() for fn, n in self.names.items() if n == _I2C.name}
        self.spi_selected: set[int] = set()            # spi-target fns whose CS is active now (spi_select)
        self.spi_ns = True                              # read_rx carries the optional TLV ns (fixture §4)
        self.spi: dict[int, SpiState] = {fn: SpiState() for fn, n in self.names.items() if n == _SPI.name}
        self.config: dict[tuple[int, str, int], bytes] = {}   # (fn, item, key) -> the user's value (core §13)
        self.generation = 1                            # the changes of this boot, from 1 (core §13.5)
        self.saved: dict | None = getattr(self, "saved", None)   # the stored items (they outlive a reboot)
        self.saved_unreadable = getattr(self, "saved_unreadable", False)   # a save left whose integrity is unknown (outlives a reboot)
        self.saved_generation = 0                      # state's saved_generation (0: none, unreadable or partial)
        self.storage_state, self.storage_reason = 0, 0
        self.storage_fault: str | None = None          # TEST HOOK: the next save / erase fails ("old" / "none" / "unreadable")
        self.parked: dict[int, int] = {}               # channel -> the idle mode the probe put the free pin in
        self.speed_state = "base"                      # port_speed: "base", "try" or "committed" (oep-if-link §3)
        self.speed_port: int | None = None             # the port off its boot speed
        self.speed_rate = 0                            # the rate it runs at
        self.speed_asked = 0                           # the baud the try asked (the commit names it again)
        self.speed_until_ms = 0                        # try: the commit's deadline (verify_ms)
        self.speed_good_ms = 0                         # committed: the last good frame on that port or answer sent there
        self.speed_pending: tuple | None = None        # ("switch", port, rate, asked, verify_ms) / ("revert",): after the answer
        self.speed_log: list[tuple[int, int]] = []     # (port, rate) every switch, reverts included
        self.speed_owner: int | None = None            # the session that raised the port
        self.speed_fallback: str | None = None         # phase 3: why a return waits for a frame boundary (verify/idle/session)
        self.speed_frame_busy = False                  # TEST HOOK: a frame on the raised port is not finished yet
        if self.saved is not None:
            self._apply_saved()                        # disable before idle (core §13.7)
        else:
            if self.saved_unreadable:
                self.storage_state, self.storage_reason = 2, 1   # nothing applied: state 2, reason 1 at the next boot (§3)
            self._initial_values()                     # no save to use: the initial values (core §13.4)
        self._park(self._boot_channels())              # every free pin's idle state before the first answer (core §8)

    def _own(self, fn: int, tag: int, fmt: str, default: int) -> int:
        """fn's describe value of `tag` as one number (`fmt`), else `default`."""
        return next((struct.unpack_from("<" + fmt, v)[0] for t, v in self.decl.get(fn, ()) if t == tag), default)

    def _channel_ok(self, ch: int) -> bool:
        """An item's channel (core §13.8): below fn 0's `channels` and not one the probe uses itself."""
        return ch < self.channels and ch not in self.own_channels

    def _idle_mode(self, ch: int) -> int | None:
        """The mode of channel `ch`'s idle (channel.{}.idle, core §13.8), None without one."""
        v = self._value(0, "channel.{}.idle", ch)
        return v[0] if v and len(v) == 1 else None

    def _output_idle(self, ch: int) -> bool:
        return self._idle_mode(ch) in (IDLE_MODE["low"], IDLE_MODE["high"])

    @property
    def disabled(self) -> set[int]:
        """The channels channel.{}.disable takes away (core §13.8): never used, driven or configured."""
        return {k for (_, k), v in self._values_of("channel.{}.disable", 0).items() if v == b"\x01"}

    def _refuse_disabled(self, channels, extra: bytes = b"") -> None:
        """A request naming a disabled channel: rejected unavailable cause 5 (held by settings) with the channel."""
        for ch in channels:
            if ch != 0xFFFF and ch in self.disabled:
                raise unavailable("held_by_settings", ch, extra=extra)

    def _all_channels(self) -> set[int]:
        """Every channel some fn's describe offers (role_channels, channel_group, the wires' pairs and reset lines)."""
        out = {ch for fn in self.decl if fn != m.CORE_FN for ch in self._declared_channels(fn)}
        out |= {p for pairs in self.pairs.values() for pair in pairs for p in pair}
        out |= {ch for chs in self.reset_channels.values() for ch in chs}
        out.discard(0xFFFF)
        return out

    def _boot_channels(self) -> set[int]:
        """What the probe parks at boot, before its first answer (core §8): every channel below fn 0's `channels` but
        its own (a probe with channels declares them, core §7.5; none declared: no channel)."""
        return set(range(self.channels)) - self.own_channels

    def _park(self, channels) -> None:
        """Free pins go to their idle state (the idle item, else Hi-Z) at boot and whenever released (core §13.8);
        a disabled channel stays as the reset left it (never parked), one a plan or a connection holds is not free (a
        core §13.8)."""
        busy = {a[2] for a in self.plan if not self._listens(a[0])} | {p for c in self.conns.values() for p in c.pair}
        for ch in channels:
            if ch == 0xFFFF or ch in self.disabled or ch in busy:
                continue
            mode = self._idle_mode(ch)
            self.parked[ch] = mode if mode is not None else 0      # 0 = Hi-Z

    @property
    def target_id(self) -> int | None:
        return self.target.target_id

    @target_id.setter
    def target_id(self, value: int | None) -> None:
        self.target.target_id = value

    @staticmethod
    def _capture_from(decl: list[tuple[int, bytes]], inner: dict) -> virtual_bench_capture.VirtualCapture:
        """A capture as its describe declares it (modes and their max_samples, rate_range, the frontends) and as it
        keeps to itself (`inner`: the w it can lay a sample out in, the segment records it keeps, how much a read
        returns at most, whether any rate in the range is kept (`exact`), the analog's skews and diagnostics - the
        probe's own choices, oep-if-capture §1.1, §2, §3.2, §3.3)."""
        d = reg.FIXTURE_ANALOG.tlv["describe"]                     # the logic's tags are the same numbers
        modes, lo, hi, fronts, most_samples = set(), 1, 1_000_000, {}, {}
        max_pre = virtual_bench_capture.MAX_SAMPLES - 1
        opts = records = kinds = types = 0
        formats, lo_d, hi_d = set(), 1, 1
        for tag, v in decl:
            if tag == d["frontend"]:                               # analog: frontend min_mv max_mv
                fe, lo_mv, hi_mv = struct.unpack("<Bii", v)
                fronts[fe] = (lo_mv, hi_mv)
            elif tag == d["mode"]:
                modes.add(v[0])
                most_samples[v[0]] = struct.unpack_from("<I", v, 1)[0]   # mode max_samples max_segments
            elif tag == d["trigger"]:                              # types(u32) max_pretrigger(u32) opts(u8)
                types = struct.unpack_from("<I", v)[0]
                max_pre, opts = struct.unpack_from("<IB", v, 4)
            elif tag == d["trigger_records"]:                      # u16 (§3.10)
                records = struct.unpack("<H", v)[0]
            elif tag == d["formats"]:                              # u8 list (§3.5)
                formats = set(v)
            elif tag == d["carriage"]:                             # kinds(u8) min_d(u32) max_d(u32)
                kinds, lo_d, hi_d = struct.unpack("<BII", v)
            elif tag == d["rate_range"]:                           # min_hz(u32) max_hz(u32)
                lo, hi = struct.unpack("<II", v)
        return virtual_bench_capture.VirtualCapture(modes or {virtual_bench_capture.MODE["one_shot"]}, set(inner.get("widths", (8,))), lo, hi,
                                        inner.get("ring", 8), inner.get("max_read", 4096), fronts,
                                        max_samples=most_samples, max_pretrigger=max_pre, exact=inner.get("exact", False),
                                        skews=inner.get("skews"), diagnostics=inner.get("diagnostics", True),
                                        opts=opts, records_max=records, formats=formats, carriage_kinds=kinds,
                                        carriage_d=(lo_d, hi_d),
                                        linked=bool(types >> reg.FIXTURE_LOGIC.enum["trigger"]["linked"] & 1))

    @staticmethod
    def _group_from(decl: list[tuple[int, bytes]], inner: dict) -> virtual_bench_capture.VirtualGroup:
        d = reg.FIXTURE_CAPTURE_GROUP.tlv["describe"]
        tracks = []
        for tag, v in decl:
            if tag == d["tracks"]:                                 # n(u8) n x fn(u16)
                tracks = list(struct.unpack_from(f"<{v[0]}H", v, 1))
        features = next((struct.unpack("<I", v)[0] for tag, v in decl if tag == catalog.FEATURES), 0)
        return virtual_bench_capture.VirtualGroup(tracks, inner.get("max_tracks", len(tracks)), list(inner.get("budgets", ())),
                                                  sourced=bool(features & reg.FIXTURE_CAPTURE_GROUP.enum["features"]["trigger_source"]))

    def _next_seq(self, fn: int) -> int:
        seq = self.push_seq.get(fn, 0)
        self.push_seq[fn] = (seq + 1) & 0xFFFF
        return seq

    def _events(self, fn: int, events: list[bytes]) -> None:
        """Events (kind(u8) payload) of `fn` to go out while it is subscribed; unsubscribed, they are not sent."""
        for e in events:
            if fn in self.subscribed:
                self.outbox.append(bytes([m.ROLE_EVENT]) + struct.pack("<HH", fn, self._next_seq(fn)) + e)

    def pushes_to(self) -> list[tuple[Hashable, bytes]]:
        """`pushes`, each with the connection it goes to (core §10.4: where its fn's subscribe came - the holder's open
        again does not move it, §6.2; `handle`'s `link`). A serving loop with several connections sends each to its own
        and drops one whose connection is gone (transports §3)."""
        return [(self.push_link.get(struct.unpack_from("<H", f, 1)[0]), f) for f in self.pushes()]

    def pushes(self) -> list[bytes]:
        """The frames the probe sends by itself now (core §10): events (at once: never batched, core §10.3) and a
        streaming capture's data - held back while its subscription's batching says so: until min_bytes bytes wait, or
        max_delay_ms has passed since the oldest of them was there (0 = that condition unused; both 0: at once). fn 0
        sends nothing. A serving loop frames and sends them; a test takes them from here. None once a restart was
        answered (oep-if-restart §2)."""
        if self.restarting:
            return []
        self.tick()
        for fn, cap in self.captures.items():
            if fn not in self.subscribed:
                if cap.unsent():                                   # streaming with no one to send to: dropped, the
                    cap.drop_to(cap.base + len(cap.data))          # position jumps (capture §2.1 rule 1, §3.2) - and
                    cap.drop_until = cap.segment_end_after(cap.sent)   # a segment begun meanwhile goes whole (rule 1)
                continue
            if cap.sent < cap.drop_until:                          # subscribed again inside a segment begun with no
                cap.drop_to(min(cap.drop_until, cap.base + len(cap.data)))   # room: the rest of it goes too
            waiting = cap.unsent()
            if not waiting:
                self.data_since_ms.pop(fn, None)
                continue
            since = self.data_since_ms.setdefault(fn, self.now())
            min_bytes, max_delay_ms = self.subscribed[fn]
            stopped = cap.state not in (virtual_bench_capture.STATE["capturing"], virtual_bench_capture.STATE["paused"])
            if push_due(min_bytes, max_delay_ms, self.now() - since, waiting, stopped):
                self.outbox += cap.pushes(fn, lambda fn=fn: self._next_seq(fn), self.probe.max_frame)
                self.data_since_ms.pop(fn, None)
        if self.speed_fallback is not None:
            # phase 3 (oep-if-link §3): a return waits for the frame boundary - no new notification frame starts on
            # the raised port until it is back at its boot speed; the frames wait, nothing is dropped
            raised = [self.push_link.get(struct.unpack_from("<H", f, 1)[0], self.speed_port) == self.speed_port
                      for f in self.outbox]
            out = [f for f, r in zip(self.outbox, raised) if not r]
            self.outbox = [f for f, r in zip(self.outbox, raised) if r]
            return out
        out, self.outbox = self.outbox, []
        return out

    def _group_tick(self, fn: int) -> None:
        per, own = self.groups[fn].tick(self.captures, self.uptime_ms())
        for track, events in per:
            self._events(track, events)
        self._events(fn, own)

    def _capture_tick(self, fn: int) -> None:
        cap = self.captures[fn]
        if cap.group is None:
            self._events(fn, cap.tick(self.uptime_ms()))
        else:
            self._group_tick(next(g for g, group in self.groups.items() if group is cap.group))

    def _capture(self, fn: int, op: int, t: Take) -> tuple[int, int, bytes]:
        cap, O = self.captures[fn], virtual_bench_capture.OP
        self._capture_tick(fn)                                    # advance a bound group together
        roles = sorted(a[1] for a in self.plan if a[0] == fn)
        budget = self.probe.max_frame - m.RESULT_HEADER
        try:
            if op in (O["configure"], O["query"]):
                analog = self.names[fn] == "oep.fixture.analog"
                frontend_tag = virtual_bench_capture.ANA.tlv["configure"]["frontend"]
                known = set(virtual_bench_capture.IMPLEMENTED) | ({frontend_tag} if analog else set())
                # the frontend comes once per channel (capture §3.3): it repeats
                got = t.tail(known, repeats={frontend_tag, virtual_bench_capture.TLV["carriage"]})
                fronts = []                                        # (role, frontend, the tag as received)
                for tag, v in t.repeated:
                    if tag & 0x7F != frontend_tag:
                        continue
                    if len(v) != 2:
                        raise Reject(m.MALFORMED)                  # an implemented TLV of another length (core §2.3)
                    fronts.append((v[0], v[1], tag))
                if len({r for r, _, _ in fronts}) != len(fronts):
                    raise Reject(m.MALFORMED)                      # two frontends for one role (capture §3.3)
                settled = cap.settle(got, t, len(roles), fronts, query=op == O["query"], planned_roles=roles)
                answer = cap.answer(settled)
                if len(answer) > budget:
                    raise Reject(m.UNSUPPORTED, bytes([m.TAG_FIXED]))   # the answer would not fit (§3.3, core §4.4)
                if op == O["configure"]:
                    cap.apply(settled)
                    cap.slipped = self.capture_slipped
                return self._answer(answer)
            if op in (O["start"], O["stop"], O["force"]):
                t.tail()                                           # the request's form before the state (core §4.3)
                if cap.group is not None:
                    raise unavailable("bound_in_group")            # the group's ops run it (capture §4)
            if op == O["start"]:
                t.tail()
                self._events(fn, cap.start(self.uptime_ms(), subscribed=fn in self.subscribed))
                if fn in self.capture_fail_start:                  # TEST HOOK: the start's initialisation failed
                    cap.state, cap.error = virtual_bench_capture.STATE["error"], virtual_bench_capture.CAP.enum["error"]["peripheral"]
                    # state 6 always says stopped reason 3 with the error (capture §3.2 エラー)
                    self._events(fn, [cap.stopped_event(virtual_bench_capture.STOPPED["error"], cap.error)])
                    return m.COMPLETED, m.FAILED, struct.pack("<I", cap.generation)   # the same fixed form (§3.2)
                return m.COMPLETED, m.SUCCESS, struct.pack("<I", cap.generation)
            if op == O["stop"]:
                t.tail()
                self._events(fn, cap.stop(self.uptime_ms()))
                return m.COMPLETED, m.SUCCESS, b""
            if op == O["force"]:
                t.tail()
                self._events(fn, cap.force(self.uptime_ms()))
                return m.COMPLETED, m.SUCCESS, b""
            if op == O["status"]:
                t.tail()
                return m.COMPLETED, m.SUCCESS, cap.status()
            if op == O["read"]:
                generation, position, most = t.take("IQH")           # generation position max(u16) (§3.2)
                t.tail()
                return m.COMPLETED, m.SUCCESS, cap.read(generation, position, most, budget)
            if op == O["segments"]:
                first = t.take("I")
                t.tail()
                return m.COMPLETED, m.SUCCESS, cap.segment_list(first, budget)
            if op == O["release"]:
                generation, serial = t.take("II")
                t.tail()
                cap.release(generation, serial, self.uptime_ms())
                return m.COMPLETED, m.SUCCESS, b""
            if op == O["triggers"]:
                generation, first = t.take("II")                   # generation from_serial (§3.10)
                t.tail()
                return m.COMPLETED, m.SUCCESS, cap.trigger_list(generation, first, budget)
            if op == virtual_bench_capture.ANA.op["calibration"] and cap.analog:
                t.tail()
                answer = cap.calibration()
                if len(answer) > budget:
                    raise Reject(m.UNSUPPORTED, bytes([m.TAG_FIXED]))   # not cut, not left out (§3.7, core §4.4)
                return m.COMPLETED, m.SUCCESS, answer
        except virtual_bench_capture.Reject as e:
            raise Reject(e.reason, e.payload)
        return m.REJECTED, m.UNKNOWN_OPERATION, b""

    def capture_overflow(self, fn: int, error: int = virtual_bench_capture.CAP.enum["error"]["storage"]) -> None:
        """TEST HOOK: capture `fn` drops data inside the segment it is taking (capture §2.2): what the clock captured up
        to now is kept, that segment is not handed out, the track stops in error (state 6, stopped reason 3, `error`).
        A bound track's group stops its other tracks and says stopped reason 3 too (capture §4.1)."""
        cap = self.captures[fn]
        self._capture_tick(fn)
        self._events(fn, cap.overflow(error))
        grp = cap.group
        if grp is not None:
            gfn = next(g for g, v in self.groups.items() if v is grp)
            for track in grp.tracks:
                if track != fn:
                    self._events(track, self.captures[track].stop(self.uptime_ms()))
            self._events(gfn, [grp.stopped_event(virtual_bench_capture.STOPPED["error"], error)])

    def _group(self, fn: int, op: int, t: Take) -> tuple[int, int, bytes]:
        grp, O = self.groups[fn], virtual_bench_capture.GRP.op
        self._group_tick(fn)
        try:
            if op == O["bind"]:
                n = t.take("B")
                fns = [t.take("H") for _ in range(n)]
                bind_tlvs = virtual_bench_capture.GRP.tlv["bind"]   # the trigger source's (§4.4), if it has one
                t.tail(set(bind_tlvs.values()) if grp.sourced else set(), repeats={bind_tlvs["stage"], bind_tlvs["cond"]})
                roles = {f: sorted(a[1] for a in self.plan if a[0] == f) for f in fns}
                # the start answer's message (18 + 6 x n bytes, its result header in the 18) against the frame (§4.1)
                grp.bind(self.captures, fns, [g for g in self.groups.values() if g is not grp], self.probe.max_frame,
                         t.repeated, roles)
                return self._answer(b"")
            if op == O["start"]:
                t.tail()
                per, own, failed = grp.start(self.captures, self.uptime_ms(), lambda track: track in self.subscribed,
                                             self.capture_fail_start)
                for track, events in per:
                    self._events(track, events)
                self._events(fn, own)
                return m.COMPLETED, m.FAILED if failed else m.SUCCESS, grp.start_answer(self.captures)   # one fixed form
            if op == O["stop"]:
                t.tail()
                per, own = grp.stop(self.captures, self.uptime_ms())
                for track, events in per:
                    self._events(track, events)
                self._events(fn, own)
                return m.COMPLETED, m.SUCCESS, b""
            if op == O["force"]:
                t.tail()
                per, own = grp.force(self.captures, self.uptime_ms())
                for track, events in per:
                    self._events(track, events)
                self._events(fn, own)
                return m.COMPLETED, m.SUCCESS, b""
            if op == O["status"]:
                t.tail()
                return m.COMPLETED, m.SUCCESS, grp.status(self.captures)
        except virtual_bench_capture.Reject as e:
            raise Reject(e.reason, e.payload)
        return m.REJECTED, m.UNKNOWN_OPERATION, b""

    def _role_pairs(self, fn: int) -> list[tuple[int, int]]:
        """A role_channels wire's allowed pairs (oep-if-debug §1: role 1 x role 2, no channel twice): swdio ascending, then
        swclk."""
        roles = self.pin_roles[fn]
        if self.names[fn] == "oep.wire.swio":
            return [(d, 0xFFFF) for d in sorted(roles.get(1, ()))]
        return [(d, c) for d in sorted(roles.get(1, ())) for c in sorted(roles.get(2, ())) if d != c]

    def _allowed_pairs(self, fn: int) -> list[tuple[int, int]]:
        return self._role_pairs(fn) if fn in self.pin_roles else self.pairs.get(fn, [])

    def _allows(self, fn: int, pair: tuple[int, int]) -> bool:
        """Whether the declaration allows (swdio, swclk) (debug §1, core §7.4): with channel_groups, the roles a group
        lists match that group exactly; role_channels binds the roles it lists; a role neither names is not allowed.
        Both declared: one group matched AND the role_channels candidates."""
        swio = self.names[fn] == "oep.wire.swio"
        if (pair[1] != 0xFFFF) if swio else pair[1] == pair[0]:
            return False
        want = {1: pair[0]} if swio else {1: pair[0], 2: pair[1]}
        roles = self.pin_roles.get(fn, {})
        groups = [dict(catalog.unpack_channel_group(v)[1]) for tag, v in self.decl[fn] if tag == catalog.CHANNEL_GROUP]
        return any(all((r in g or r in roles) and (r not in g or g[r] == ch) and (r not in roles or ch in roles[r])
                       for r, ch in want.items()) for g in (groups or [{}]))

    def _held(self, fn: int | None = None, pair: tuple[int, int] | None = None) -> set[int]:
        """Channels something holds (core §8.1): the plan and the live connections' pairs - except
        wire `fn`'s own connection on `pair` (attaching there again, or scanning through it)."""
        held = {a[2] for a in self.plan if not self._listens(a[0])}   # a logic capture only listens
        held |= {p for c in self.conns.values() for p in c.pair if p != 0xFFFF and not (c.fn == fn and c.pair == pair)}
        return held

    def _target(self, fn: int, pair: tuple[int, int]) -> VirtualTarget:
        tg = self.targets.get((fn, pair))
        if tg is None:                                             # a pair nothing is wired to
            tg = self.targets[(fn, pair)] = VirtualTarget(present=False)
        return tg

    def _group_pairs(self, fn: int, name: str, roles: dict[int, set[int]]) -> list[tuple[int, int]]:
        """A wire with channel_groups: the (swdio, swclk) pairs they allow, in declared order (debug §1, core §7.4) - a
        role a group lists is that group's channel (in role_channels' candidates when they list the role), a role it
        does not list comes from role_channels alone. No groups: [] (role_channels alone, `_role_pairs`)."""
        swio = name == "oep.wire.swio"
        out: list[tuple[int, int]] = []
        for tag, v in self.decl[fn]:
            if tag != catalog.CHANNEL_GROUP:
                continue
            group = dict(catalog.unpack_channel_group(v)[1])

            def options(role: int) -> list[int]:
                if role in group:
                    return [group[role]] if role not in roles or group[role] in roles[role] else []
                return sorted(roles.get(role, ()))
            out += [(d, c) for d in options(1) for c in ([0xFFFF] if swio else options(2))
                    if (swio or d != c) and (d, c) not in out]
        return out

    def _raw_ns(self) -> int:
        return int(self._clock_ns()) if self._clock_ns is not None else int(self.now() * 1_000_000)

    def now_ns(self) -> int:
        """The probe's one clock (core §2.6a): ns since boot, at the resolution of `now_ns` when the endpoint has one
        (else ms). It never decreases while the boot_id is the same (C-31), and starts again at a reboot."""
        self._last_ns = max(self._last_ns, self._raw_ns() - self._origin_ns)
        return self._last_ns

    def uptime_ms(self) -> int:
        """The probe's clock in ms (what the captures count their times from)."""
        return self.now_ns() // 1_000_000

    def _clock_ns_of(self, ms: int) -> int:
        """A time of the timers' clock (`now_ms`) on the probe's clock (ns since boot)."""
        return max(0, ms * 1_000_000 - self._origin_ns)

    def _new_resource(self, kind: str) -> int:
        """The next resource number (core §9): one u32 space for connections and streams from 1, never reused within
        a boot_id; past 0xFFFFFFFF a new resource is refused (unavailable cause 2)."""
        n = self._next_resource
        if n > 0xFFFFFFFF:
            raise unavailable("limit")
        self._next_resource = n + 1
        self.resources[n] = kind
        return n

    def _connection(self, cid: int) -> Connection:
        """The connection `cid`, or rejected: no_resource (unknown), unavailable cause 6 (a stream's number)."""
        c = self.conns.get(cid)
        if c is None:
            raise wrong_kind() if cid in self.resources else Reject(m.NO_RESOURCE)
        return c

    def _stream(self, sid: int) -> Stream:
        s = self.streams.get(sid)
        if s is None:
            raise wrong_kind() if sid in self.resources else Reject(m.NO_RESOURCE)
        return s

    # ---- the one entry point: a request message in, a result message out ------------------------
    def handle(self, data: bytes, transport: int = 0, link: Hashable = None) -> bytes | None:
        """A request from transport `transport` (the index in the describe's transport list) -> its result. `link` names
        the connection it came on when one transport has several (a TCP listener's accepted connections, transports §1:
        each is a transport of its own for the revision in use and the notifications' target); None = the transport."""
        if self.probe.core_contract:
            from .virtual_core import Core
            if self.current_core is None:
                self.current_core = Core(self)
            return self.current_core.handle(data, transport)
        if not data or data[0] != m.ROLE_REQUEST or len(data) < REQUEST_HEADER:
            self.discarded += 1                                   # not a request, or shorter than its header (C-36)
            return None
        if self.restarting:
            self.unanswered += 1                                  # the restart's answer is out: nothing more (oep-if-restart §2)
            return None
        req = m.Request.unpack(data)
        if self.revision == 0 and req.session:
            self.dropped += 1                                     # a v0 probe: no sessions in its shapes, no answer
            return None
        self.requests.append(req)
        self._transport = transport
        if req.session and req.session == self.holder:
            self.session_ports.add(transport)                      # its pass-through stops for the session (config §1.1)
            self.resident.pop(transport, None)                     # (its console's too: closed, §1.2)
        self._via = transport if link is None else link
        self._lapse()                                              # an expired lease is cleaned up before the next
        #                                                            request's processing, its resend check too (core §6.1)
        self._speed_due(transport)                                 # a return due now waits for this frame's end (link §3)
        header = self._header(req)                                 # core §4.3: the header, before the resend table
        if header is not None:
            return m.Result(req.corr, m.REJECTED, header).pack()
        if self.policy is not None and (req.fn, req.op) not in PUBLIC_OPS and not self.policy(req):
            # an admission policy (core §4.3, optional): after the header, before the resend table - nothing changes,
            # no remembered answer is given out
            return m.Result(req.corr, m.REJECTED, m.UNAVAILABLE, unavailable("policy_denied").payload).pack()
        remembered = self._resent(req, data)
        if remembered is not None:
            return remembered                                      # replayed: the lease does not restart (§6.1)
        try:
            res, detail, payload = self._dispatch(req)
        except Reject as r:
            res, detail, payload = m.REJECTED, r.reason, r.payload
        finally:
            if self._processing is not None:
                advance, self._processing = self._processing, None
                advance()                                          # the request's processing time (process())
        if res == m.COMPLETED:
            self.executions += 1
        if res == m.REJECTED and detail == m.UNSUPPORTED and not payload:
            payload = bytes([m.TAG_FIXED])                         # core §4.3: unsupported is always tag [TLV]
        if res != m.REJECTED and not self._closed_tail(req.fn, req.op) and self.revision >= 1:
            payload += self.tail
        is_open = req.fn == m.CORE_FN and req.op == m.OP_OPEN
        # the header's refusals were answered above: every request here passed orders 1-2 (an unknown_function here is
        # order 4's, a payload's fn, and counts like any other refusal)
        if req.session and req.session == self.holder:
            # every answer to the holder's request past the session check, rejected ones too (core §6.1)
            self.expires_ms = self.now() + self.lease_ms
        took_lock = is_open and res == m.COMPLETED
        out = m.Result(req.corr, res, detail, payload).pack()
        self.speed_answered(transport)                             # a committed rate's idle counts from here too
        if req.session and req.session == self.last:
            self._remember(req, data, out)                         # rejected answers too, open too (core §5.1)
        if transport not in self.serial_ports:
            self.after_answer()                                    # the answer is not on a port whose speed changes
        return out

    # ---- one route's requests in steps: receive, process, sent (core §4.4) ---------------------------------------
    def receive(self, data: bytes, route: Hashable = 0) -> str:
        """A request arriving on `route`: "queued", or "coalesced" when the same bytes are still waiting or being
        answered there - the copy is dropped unexamined, run never, counted in no window (core §4.4)."""
        queue = self.routes.setdefault(route, [])
        if any(entry[0] == data for entry in queue):
            return "coalesced"
        queue.append([data, None])
        return "queued"

    def process(self, route: Hashable = 0, spend: Callable[[], None] | None = None) -> bytes | None:
        """The oldest request queued on `route` carried out; `spend` passes its processing time (the clock moves while
        it runs: an owner's lease is not counted meanwhile, its answer restarts it, core §6.1). The answer waits for
        `sent`."""
        entry = next(e for e in self.routes.get(route, []) if e[1] is None)
        self._processing = spend
        entry[1] = self.handle(entry[0], route) or b""
        self._processing = None
        return entry[1]

    def sent(self, route: Hashable = 0) -> None:
        """The oldest answer on `route` is out: its credit is free again (window / max_inflight)."""
        queue = self.routes.get(route, [])
        queue.pop(next(i for i, e in enumerate(queue) if e[1] is not None))

    def close_route(self, route: Hashable) -> None:
        """The route closed: what it had not carried out is dropped; the session is not ended (transports §3)."""
        self.routes.pop(route, None)

    def credits(self) -> dict:
        return {r: len(q) for r, q in self.routes.items() if q}

    def _header(self, req: m.Request) -> int | None:
        """core §4.3, the header: unknown_function, unknown_operation (an op the interface does not define,
        or an optional op this probe does not offer: core §1.2, `offers`), session_required - in that order and before
        the resend table, so these are neither remembered nor restart the lease. corr 0 is malformed before them."""
        if req.corr == 0:
            return m.MALFORMED                                     # corr never 0 (core §4.1, §4.3 order 1)
        if req.fn != m.CORE_FN and req.fn not in self.names:
            return m.UNKNOWN_FUNCTION
        if not self.offers(req.fn, req.op):
            return m.UNKNOWN_OPERATION
        if not req.session and not self._lock_free(req.fn, req.op):
            return m.SESSION_REQUIRED                              # an op that needs the lock with session_id 0 (§4.1)
        return None

    def features(self, fn: int) -> int:
        """fn's describe features (common tag 0x06, u32), 0 without one."""
        return self._own(fn, catalog.FEATURES, "I", 0)

    def offers(self, fn: int, op: int) -> bool:
        """core §1.2: an fn offers exactly the ops its describe's ops tag sets (§7.4) - every required op, and an
        optional one when the probe has it; any other op is unknown_operation (§4.3, the header). The profiles say which
        optional ops they have (virtual_bench.VirtualProbe fills in the ops tag of an interface that gives none: all of its ops).
        oep.probe.link's port_speed follows `port_speed_base` (a test turns it off or on)."""
        if fn not in self.ops:
            return False
        if fn == self.link_fn and op in (OP_PORT_SPEED, OP_PORT_STATE):
            return self.port_speed_base is not None
        return op in self.ops[fn]

    def _interface(self, fn: int):
        return reg.INTERFACES.get(self.names.get(fn, ""))

    def _closed_tail(self, fn: int, op: int) -> bool:
        """Answers the test `tail` is not appended to: the answers that are TLV lists themselves - describe and
        fn 0's settings get (their own meta TLVs are 0x3F / 0x7E)."""
        if fn == m.CORE_FN and op == m.OP_DESCRIBE:
            return True
        return fn == m.CORE_FN and op == _SET_OP["get"]

    def _lock_free(self, fn: int, op: int) -> bool:
        i = self._interface(fn)
        if fn == m.CORE_FN:
            return op in reg.CORE.lock_free
        if i is None or self.names.get(fn) not in SIMS:
            return op == TOY_READ or op in self.reads.get(fn, ())
        return op in i.lock_free

    @staticmethod
    def _answer(payload: bytes, detail: int = m.SUCCESS) -> tuple[int, int, bytes]:
        """A completed result."""
        return m.COMPLETED, detail, payload

    def _dispatch(self, req: m.Request) -> tuple[int, int, bytes]:
        """Routes a request (its Take reads the payload as core §2.3 says)."""
        return self._route(req, Take(req.payload))

    def _route(self, req: m.Request, t: Take) -> tuple[int, int, bytes]:
        self._lapse()
        self._console_poll()
        if req.fn == m.CORE_FN and req.op == m.OP_OPEN:
            return self._open(t, req)
        if req.session:                                            # §4.1: any request with an id goes through §6.2
            refused = self._check(req.session)
            if refused:
                return refused
        if req.fn == m.CORE_FN and req.op == m.OP_CONFIRM:
            return self._confirm(t)
        if req.fn == m.CORE_FN:
            return self._core(req, t)
        if req.op in (m.OP_SUBSCRIBE, m.OP_UNSUBSCRIBE) and (req.fn in self.captures or req.fn in self.groups):
            return self._subscription(req.fn, req.op, t)          # the emitting interface's own ops (core §10.3)
        sim = SIMS.get(self.names[req.fn])
        if sim is not None:
            return getattr(self, f"_{sim}")(req.fn, req.op, t)
        return self._toy(req, t)

    # ---- replay (core §5.1) ---------------------------------------------------------------------
    def _resent(self, req: m.Request, data: bytes) -> bytes | None:
        """A request of the last successfully opened session (core §4.3 order 2, §5.1): its remembered answer, malformed
        (the same corr with other bytes) or result_lost (not kept, or not ahead of the last corr); None = a new
        request. session_id 0 is never deduplicated."""
        if not req.session or req.session != self.last:
            return None
        lost = m.Result(req.corr, m.REJECTED, m.RESULT_LOST).pack()
        if req.corr in self.resend:
            request, result = self.resend[req.corr]
            if request is None:
                return lost
            if request != data:
                return m.Result(req.corr, m.REJECTED, m.MALFORMED).pack()
            return result if result is not None else lost
        if self.newest_corr is not None and not 0 < (req.corr - self.newest_corr) % 0x10000 < 0x8000:
            return lost
        return None

    def _remember(self, req: m.Request, data: bytes, result: bytes) -> None:
        """A new request of the last session that passed order 2: the last corr moves to it, and it is recorded (rejected
        answers too) within the record's bounds; records half the space or more behind are dropped (core §5.1)."""
        keep = lambda b: b if len(b) <= self.remember_max else None   # noqa: E731
        self.resend[req.corr] = (keep(data), keep(result))
        self.resend.move_to_end(req.corr)
        self.newest_corr = req.corr
        for corr in [c for c in self.resend if not 0 <= (self.newest_corr - c) % 0x10000 < 0x8000]:
            del self.resend[corr]
        while len(self.resend) > self.remember_count:               # a record bound of the bench's choosing (§5.1)
            self.resend.popitem(last=False)

    # ---- the stand-in operations ----------------------------------------------------------------
    def _toy(self, req: m.Request, t: Take) -> tuple[int, int, bytes]:
        if req.op in self.reads.get(req.fn, ()):
            t.tail()
            return self._answer(b"")                               # a side-effect-free read with no fixed part
        if req.op == TOY_READ:
            t.tail()
            return self._answer(struct.pack("<I", self.values.get(req.fn, 0)))
        if req.op == TOY_WRITE:
            value = t.take("I")
            t.tail()
            self.values[req.fn] = value
            return self._answer(b"")
        return m.REJECTED, m.UNKNOWN_OPERATION, b""

    # ---- the lock -------------------------------------------------------------------------------
    def _lapse(self) -> None:
        if self.holder is not None and self.now() >= self.expires_ms:
            self._release_lock()                                   # the lock goes, the last id stays (the table's)

    def _release_lock(self) -> None:
        """The session's lock ends - end, lease expiry or force, all alike (core §6.4, §9): everything it created is
        released - its plan (the pins to their idle state), its streams and then its connections (console §2, common
        §2), its subscriptions; the owner is forgotten. Nothing passes to the next session."""
        self.holder = None
        self.owner = None
        self.session_ports.clear()                                 # the pass-throughs may run again (config §1.1)
        for fn in list(self.subscribed):                           # subscriptions end with the lock (core §9), and
            self._drop_unsent(fn)                                  # what they had not sent yet goes (§10.3)
            self.sub_epoch[fn] = self.sub_epoch.get(fn, 0) + 1
            self.data_since_ms.pop(fn, None)
        self.subscribed.clear()
        self.push_link.clear()
        for fn in {a[0] for a in self.plan}:
            self._drop_plan(fn)
        for sid in list(self.streams):                             # the streams before their connections (core §9)
            self._close_stream(sid)
        for cid in list(self.conns):
            self._close_conn(cid)
        for grp in self.groups.values():
            grp.release_session(self.captures)
        self._session_over()

    def _remaining(self) -> int:
        return max(0, self.expires_ms - self.now()) if self.holder is not None else 0

    def _locked(self) -> tuple[int, int, bytes]:
        owner = m.tlv(reg.CORE.tlv["locked_payload"]["owner"], self.owner) if self.owner else b""
        return m.REJECTED, m.LOCKED, struct.pack("<I", self._remaining()) + owner

    def _check(self, session: int) -> tuple[int, int, bytes] | None:
        """core §6.2 for a request (not open) with a session_id: no session holds the lock -> no_session; another
        holds it -> locked; the holder's -> None (handled)."""
        if self.holder is None:
            return m.REJECTED, m.NO_SESSION, b""                   # no resume: an ended session never continues
        if session == self.holder:
            return None
        return self._locked()

    def _open(self, t: Take, req: m.Request) -> tuple[int, int, bytes]:
        """open (core §6.4): lease_ms(u32) force(u8) [TLV owner], the id in the header; the decision table of §6.2.
        force is a boolean: any value but 0 is true (core §2.1)."""
        session = req.session
        if session == 0:
            raise Reject(m.MALFORMED)                              # session_id 0 (§4.1)
        # the session check (core §4.3 order 3: no_session / locked) comes before the payload's (order 4: malformed);
        # force is read where it stands, a payload too short to hold it asks for no force
        asks_force = len(req.payload) >= 5 and req.payload[4] != 0
        if self.holder is None and session == self.last:
            return m.REJECTED, m.NO_SESSION, b""                   # the ended id is never opened again (core §6.2)
        if self.holder is not None and self.holder != session and not asks_force:
            return self._locked()
        lease, force = t.take("IB")
        got = t.tail({OWNER})
        owner = got.get(OWNER)
        if any(not 1 <= len(v) <= reg.LIMITS["owner_max_bytes"] for tag, v in t.repeated if tag & 0x7F == OWNER):
            raise Reject(m.MALFORMED)                              # owner: 1-32 bytes, a later copy too (core §6.4, §2.3)
        if self.holder is not None and self.holder != session:
            self._release_lock()                                   # force: the old session is released first (§9)
        if session != self.holder:                                 # a new lock (a resent open keeps everything)
            self.subscribed.clear()
            self.push_link.clear()
            self.owner = owner                                     # from the open that takes the lock, kept while held
            self.resend.clear()                                    # only a new id's open starts a new replay state (§5.1)
            self.newest_corr = None
        # the holder's open again (from any connection) keeps the resources, the resend state and where the
        # subscriptions send (core §6.2): a subscribe again moves them (§10.3)
        self.holder = self.last = session
        self.session_ports.add(self._transport)                    # its pass-through stops for the session (config §1.1)
        self.resident.pop(self._transport, None)                   # (its console's too: closed, §1.2)
        # 0: the probe's default; else rounded into lease_min_ms .. lease_max_ms (core §6.4)
        lo, hi = reg.LIMITS["lease_min_ms"], min(self.lease_max_ms, reg.LIMITS["lease_max_ms"])
        self.lease_ms = self.lease_default_ms if lease == 0 else min(max(lease, lo), hi)
        self.expires_ms = self.now() + self.lease_ms
        return self._answer(struct.pack("<II", self.lease_ms, self.boot_id))

    def reboot(self, boot_id: int | None = None) -> None:
        """The probe restarts: lock, last id, the resend table, connections, streams, captures, subscriptions, the
        revision in use, the port speed, the unsaved config and the plan are gone; the saved config comes back, and the
        clock starts again from 0 (ns since boot, core §2.6a). `boot_id` None
        draws a new one, as from a hardware random source (core §6.5, C-19); passing the current one plays a probe
        whose only source repeated it - its hosts learn only that their session is gone (no_session, core §6.2)."""
        if boot_id is None:
            boot_id = self.boot_id
            while boot_id == self.boot_id:
                boot_id = secrets.randbits(32)
        self.current_core = None
        self.boot_id = boot_id
        self.reboots += 1
        self._origin_ns, self._last_ns = self._raw_ns(), 0
        for fn in self.captures:
            self.captures[fn] = self._capture_from(self.decl[fn], self.inner[fn])
        for fn in self.groups:
            self.groups[fn] = self._group_from(self.decl[fn], self.inner[fn])
        self._boot()

    def lose_connections(self) -> None:
        """A wire drops every connection; their console streams close with them."""
        self.lose()

    def lose(self, cid: int | None = None) -> list[int]:
        """TEST HOOK: the line of connection `cid` (every connection when None) is lost for good (debug §2): the
        connection and its console streams close, and a request naming either is answered no_resource. -> the
        connections closed (an unknown `cid`: none)."""
        gone = [c for c in (list(self.conns) if cid is None else [cid]) if c in self.conns]
        for c in gone:
            self._close_conn(c, lost=True)
        return gone

    # ---- core -----------------------------------------------------------------------------------
    def _confirm(self, t: Take) -> tuple[int, int, bytes]:
        magic, lo, hi = t.bytes(4), *t.take("BB")
        if magic != m.CONFIRM_REQUEST:
            return m.REJECTED, m.MALFORMED, b""
        if self.revision == 0:                                     # v0 shape: max_frame(16) window(16) inflight flags
            return m.COMPLETED, m.SUCCESS, struct.pack("<4sBHHBB", m.CONFIRM_RESULT, 0, self.probe.max_frame,
                                                       min(self.window, 0xFFFF), self.max_inflight, 0)
        t.tail()
        if lo > hi:
            raise Reject(m.MALFORMED)                              # min_rev > max_rev (core §7.1)
        if not lo <= self.revision <= hi:
            # no revision in the range: tag 0x00, then TLV supported (min, max) - the range this probe handles
            raise unsupported_fixed(m.tlv(UNSUPPORTED_SUPPORTED, bytes([self.revision, self.revision])))
        self.revision_in_use[self._via] = self.revision           # until the next confirm on this connection (§7.1)
        return self._answer(struct.pack("<4sBHIBI", m.CONFIRM_RESULT, self.revision, self.probe.max_frame,
                                        self.window, self.max_inflight, self.boot_id)    # boot_id (core §7.1)
                            + m.tlv(CONFIRM_TRANSPORT, bytes([self._transport])))   # the transport it came on

    def _core(self, req: m.Request, t: Take) -> tuple[int, int, bytes]:
        op = req.op
        if op == m.OP_LIST:                                         # first(u16) [TLV] (core §7.2)
            first = t.take("H")
            t.tail()
            return m.COMPLETED, m.SUCCESS, self.probe.call(m.CORE_FN, op, struct.pack("<H", first))
        if op == m.OP_DESCRIBE:
            fn, first = t.take("HH")
            t.tail()
            if fn not in self.names:
                return m.REJECTED, m.UNKNOWN_FUNCTION, b""         # an fn the probe does not offer (core §4.3)
            return m.COMPLETED, m.SUCCESS, self._page(self._declarations(fn), first)
        if op == m.OP_LOCK_STATE:
            t.tail()
            owner = (m.tlv(reg.CORE.tlv["lock_state_answer"]["owner"], self.owner)
                     if self.owner and self.holder is not None else b"")
            return self._answer(struct.pack("<BI", int(self.holder is not None), self._remaining()) + owner)
        if op == m.OP_END:
            t.tail()
            self._release_lock()
            return m.COMPLETED, m.SUCCESS, b""
        if op == m.OP_KEEPALIVE:
            t.tail()
            return self._answer(b"")
        if op in (_SET_OP[k] for k in ("get", "set", "unset", "save", "state", "network")):
            return self._settings_op(op, t)                        # the settings (core §13), offered per the ops tag
        if op == m.OP_CLOCK:
            # core §7.6: no fixed part; lock-free (session_id 0 touches no session, lock or lease, §4.1). The clock is
            # read while handling this request, never a value read earlier
            t.tail()
            return self._answer(struct.pack("<IQ", self.boot_id, self.now_ns()))
        return m.REJECTED, m.UNKNOWN_OPERATION, b""

    # ---- oep.probe.restart (oep-if-restart) ------------------------------------------------------
    def _restart(self, fn: int, op: int, t: Take) -> tuple[int, int, bytes]:
        """restart (oep-if-restart §2): no fixed part; the lock was checked (session_required / no_session / locked).
        The answer goes first; once it is out (`after_answer`) the probe restarts, and until then nothing more is
        processed."""
        if op == OP_RESTART:
            t.tail()
            self.restarting = True
            return self._answer(b"")
        return m.REJECTED, m.UNKNOWN_OPERATION, b""

    # ---- notifications: the emitting interface's own subscribe / unsubscribe (core §10.3) --------------------
    def _subscription(self, fn: int, op: int, t: Take) -> tuple[int, int, bytes]:
        """subscribe: min_bytes(u16) max_delay_ms(u32) [TLV] - replaces the fn's subscription atomically (the batching,
        the transport, seq from 0); unsubscribe: [TLV] - none to end succeeds doing nothing. Either drops what the old
        subscription has not sent yet (core §10.3; the bench's frames are whole once handed out)."""
        if op == m.OP_SUBSCRIBE:
            min_bytes, max_delay_ms = t.take("HI")
            t.tail()
            self._drop_unsent(fn)
            self.sub_epoch[fn] = self.sub_epoch.get(fn, 0) + 1
            self.subscribed[fn] = (min_bytes, max_delay_ms)
            self.push_link[fn] = self._via                         # sent where the subscribe came (core §10.4)
            self.push_seq[fn] = 0                                  # seq from 0 at every subscribe (core §10.2)
            self.data_since_ms.pop(fn, None)
            return m.COMPLETED, m.SUCCESS, b""
        t.tail()
        self._drop_unsent(fn)
        self.sub_epoch[fn] = self.sub_epoch.get(fn, 0) + 1
        self.subscribed.pop(fn, None)
        self.push_link.pop(fn, None)
        self.data_since_ms.pop(fn, None)
        return m.COMPLETED, m.SUCCESS, b""

    def _drop_unsent(self, fn: int) -> None:
        self.outbox = [f for f in self.outbox if struct.unpack_from("<H", f, 1)[0] != fn]

    # ---- oep.probe.plan (oep-if-plan) ------------------------------------------------------------
    def _plan_op(self, plan_fn: int, op: int, t: Take) -> tuple[int, int, bytes]:
        """plan_apply: role_assignment TLVs (oep-if-plan §2.1; the tag the same with or without bit 7, core §2.3);
        plan_release: n(u8) n x fn(u16) (§2.2). Every check runs before anything changes (core §4.3)."""
        if op == OP_PLAN_APPLY:
            t.tail({ROLE_ASSIGNMENT}, repeats={ROLE_ASSIGNMENT}, defer=True)
            got, tags = [], {}
            for tag, value in t.repeated:
                if len(value) != 5:
                    raise Reject(m.MALFORMED)                      # fn(u16) role(u8) channel(u16) (oep-if-plan §2.1)
                a = struct.unpack("<HBH", value)
                got.append(a)
                tags.setdefault(a[0], tag)                         # the tag as received, for unsupported (§2.4): the
                tags.setdefault(a, tag)                            # refused assignment's own (core §4.3), or the fn's first
            if len(set(got)) != len(got) or any(fn == m.CORE_FN for fn, _, _ in got):
                raise Reject(m.MALFORMED)                          # the same (fn, role, channel) twice, or fn 0 (oep-if-plan §2.5)
            named = {fn for fn, _, _ in got}
            if any(fn not in self.names for fn in named):
                raise Reject(m.UNKNOWN_FUNCTION)                   # ... then its fns ...
            t.check()                                              # ... an unknown critical TLV ...
            tag_of = lambda key: tags.get(key, ROLE_ASSIGNMENT)    # noqa: E731  key: an fn or an (fn, role, channel)
            self._check_plan(got, held=False, tag_of=tag_of)       # ... roles and channels not declared ...
            self._check_plan(got, tag_of=tag_of)
            self._refuse_disabled(ch for _, _, ch in got)          # a disabled channel (core §13.8): cause 5
            for fn, _, ch in got:                                  # only gpio and a logic capture take an output idle
                if (self._output_idle(ch) and self.names.get(fn) not in ("oep.fixture.gpio", "oep.fixture.logic")
                        and (fn, _, ch) not in self.plan):
                    raise unavailable("held_by_settings", ch)      # (fixture §1, capture §1.2; plan §2.4: cause 5)
            if self.plan_roles is not None and len([a for a in self.plan if a[0] not in named]) + len(got) > self.plan_roles:
                raise unavailable("limit")                          # plan_roles (oep-if-plan §2.1)
            # only the fns whose set of assignments changes; the same set is left as it is (oep-if-plan §2.1)
            changed = {fn for fn in named if {a for a in self.plan if a[0] == fn} != {a for a in got if a[0] == fn}}
            for fn in sorted(changed):
                refusal = self._plan_change_refusal(fn)
                if refusal is not None:
                    raise refusal
            self._replace_plans(changed, [a for a in got if a[0] in changed])
            for fn in changed:
                self._uart_plan_changed(fn)
                self._capture_unplanned(fn)
            return m.COMPLETED, m.SUCCESS, b""
        if op == OP_PLAN_RELEASE:                                 # n(u8) n x fn(u16); n = 0: every fn
            n = t.take("B")
            fns = {t.take("H") for _ in range(n)}
            t.tail()
            if any(fn not in self.names for fn in fns):
                raise Reject(m.UNKNOWN_FUNCTION)                   # every fn checked before anything is released (§2.2);
            #                                                        fn 0 exists and has no plan: ignored like any such fn
            dropping = {a[0] for a in self.plan if not fns or a[0] in fns}
            if any(fn in self.captures and self.captures[fn].group is not None for fn in dropping):
                raise unavailable("bound_in_group")                # a track bound in a group (oep-if-plan §2.2)
            for fn in dropping:
                self._drop_plan(fn)
            return m.COMPLETED, m.SUCCESS, b""
        return m.REJECTED, m.UNKNOWN_OPERATION, b""

    def _plan_change_refusal(self, fn: int) -> Reject | None:
        """Why fn's set of assignments may not change now (oep-if-plan §2.1): bound in a capture group (cause 4), or its
        function running (cause 6) - a configured fixture UART (fixture §2), an i2c / spi target in state 1 (§3, §4), a
        capture in state 2, 3 or 5 (capture §3.2). None: it may."""
        cap = self.captures.get(fn)
        if cap is not None:
            if cap.group is not None:
                return unavailable("bound_in_group")
            if cap.state in (virtual_bench_capture.STATE["waiting"], virtual_bench_capture.STATE["capturing"],
                             virtual_bench_capture.STATE["paused"]):
                return unavailable("wrong_state")
        if self.names.get(fn) == "oep.fixture.uart" and fn in self.uart_baud and fn in self.uarts:
            return unavailable("wrong_state")
        if (fn in self.i2c and self.i2c[fn].state) or (fn in self.spi and self.spi[fn].state):
            return unavailable("wrong_state")
        return None

    def _capture_unplanned(self, fn: int) -> None:
        """A capture whose plan changed or went: its configuration, data and segments are gone and it is back in state
        0; the generation stays and the next start moves it on (capture §3.2)."""
        cap = self.captures.get(fn)
        if cap is None or cap.state == virtual_bench_capture.STATE["unconfigured"]:
            return
        fresh = self._capture_from(self.decl[fn], self.inner[fn])
        fresh.generation = cap.generation
        self.captures[fn] = fresh

    def _listens(self, fn: int) -> bool:
        """A logic capture only listens: it shares pins with anything (core §8.1). The analog does not: its pads go to
        their analog function, cutting their digital input and output (oep-if-capture §1.2), as on this library's ESP32s."""
        return fn in self.captures and not self.captures[fn].analog

    def _check_target_roles(self, got: list[tuple[int, int, int]], tag_of=None) -> None:
        """An i2c-target / spi-target plan holds each of its roles exactly once, on distinct channels (fixture §3 /
        §4): a missing role, a role twice or two roles on one channel -> unsupported, the role_assignment's tag as
        received. A role the fn does not define is left to the declaration check (unsupported too)."""
        for fn in sorted({f for f, _, _ in got if f in self.i2c or f in self.spi}):
            roles = TARGET_ROLES[self.names[fn]]
            mine = [(role, ch) for f, role, ch in got if f == fn and role in roles]
            if sorted(r for r, _ in mine) != sorted(roles) or len({ch for _, ch in mine}) != len(mine):
                raise Reject(m.UNSUPPORTED, bytes([tag_of(fn) if tag_of else ROLE_ASSIGNMENT]))

    def _check_plan(self, got: list[tuple[int, int, int]], held: bool = True, tag_of=None) -> None:
        """plan_apply's all-or-nothing check, in core §4.3's order over the whole request: the roles and channels each
        fn declares (unsupported) for every assignment, then - `held` - no pin another fn or a
        connection holds (unavailable). `tag_of(fn)`: the tag an unsupported names (the role_assignment or
        a plan as received)."""
        self._check_target_roles(got, tag_of)
        for declared in (True, False) if held else (True,):
            self._check_plan_pass(got, declared, tag_of)

    def _check_plan_pass(self, got: list[tuple[int, int, int]], declared: bool, tag_of) -> None:
        named = {fn for fn, _, _ in got}
        others = {a for a in self.plan if a[0] not in named}
        kept = {a for a in others if not self._listens(a[0])}
        slot_pins = {p for c in self.conns.values() for p in c.pair if p != 0xFFFF}    # a live connection's pins
        analog = {fn for fn, cap in self.captures.items() if cap.analog}
        current = [None]                                           # the fn of the assignment being checked

        def not_declared(ch: int) -> Reject | None:
            # a role or channel the describe does not offer: unsupported, tag 0x90 + the channel (oep-if-plan §2.5); this pass
            # raises only its own kind (None: the other pass's)
            if not declared:
                return None
            tag = tag_of(current[0]) if tag_of else ROLE_ASSIGNMENT
            return Reject(m.UNSUPPORTED, bytes([tag]) + m.tlv(_UNA["channel"], struct.pack("<H", ch)))

        def held(ch: int, holder: int | None) -> Reject | None:
            return None if declared else unavailable("pin_in_use", ch)
        def check(r: Reject | None) -> None:
            if r is not None:
                raise r
        for fn, role, ch in got:
            current[0] = (fn, role, ch)                            # an unsupported names this assignment's tag
            beside = [f for f, _, c in got if c == ch and f != fn] + [a[0] for a in others if a[2] == ch]
            if fn in analog:   # the analog shares its pin with nothing: another fn's plan, a connection
                if role not in self._declared_roles(fn, ch):
                    check(not_declared(ch))
                    continue
                if beside or ch in slot_pins:
                    check(held(ch, beside[0] if beside else None))
                if self._output_idle(ch) and not declared:         # its pad would leave an output idle (capture §1.2)
                    raise unavailable("held_by_settings", ch)
                continue
            if any(f in analog for f in beside):   # nor may anything come onto an analog pin
                check(held(ch, next(f for f in beside if f in analog)))
            if fn in self.captures:
                if role not in self._declared_roles(fn, ch):
                    check(not_declared(ch))
                continue
            roles = TARGET_ROLES.get(self.names.get(fn, ""), set())
            if role not in roles or ch not in self._declared_channels(fn):
                check(not_declared(ch))
                continue
            if (self.names.get(fn) == _UART.name and role == _UART.enum["role"]["tx"]
                    and not self.offers(fn, _UART.op["write"])):
                check(not_declared(ch))                            # no write: no TX role (fixture §2)
                continue
            # role_channels binds the roles it lists, each to its own candidates (core §7.4: "bit i が立っていれば
            # channel base+i をその role に使える"), for every fn - a uart's RX on a channel offered for TX only too
            # (oep-if-plan §2.4: "channel が role_channels の候補に無い" -> unsupported)
            if role in self._listed_roles(fn) and role not in self._declared_roles(fn, ch):
                check(not_declared(ch))
                continue
            if any(k[2] == ch for k in kept) or ch in slot_pins:
                check(held(ch, next((k[0] for k in kept if k[2] == ch), None)))
        for fn in {f for f, _, _ in got if f in self.i2c or f in self.spi}:
            current[0] = fn
            groups = [set(catalog.unpack_channel_group(v)[1]) for tag, v in self.decl[fn]
                      if tag == catalog.CHANNEL_GROUP]
            mine = {(role, ch) for f, role, ch in got if f == fn}
            # A group fixes only the roles it names. Other roles are selected
            # through role_channels, whose candidates were checked above.
            listed = self._listed_roles(fn)
            def matches(group):
                grouped = {role for role, _ in group}
                return ({(role, ch) for role, ch in mine if role in grouped} == group
                        and all(role in grouped or role in listed for role, _ in mine))
            if groups and not any(matches(group) for group in groups):
                check(not_declared(min(ch for _, ch in mine)))

    def _declared_roles(self, fn: int, channel: int) -> set[int]:
        """The roles fn's describe offers on `channel` (role_channels)."""
        return {v[0] for tag, v in self.decl[fn] if tag == catalog.ROLE_CHANNELS and channel in _role_channels(v)}

    def _listed_roles(self, fn: int) -> set[int]:
        """The roles fn's role_channels name (the only ones it binds, core §7.4)."""
        return {v[0] for tag, v in self.decl[fn] if tag == catalog.ROLE_CHANNELS}

    def _declared_channels(self, fn: int) -> set[int]:
        """Every channel fn's describe offers in any role (role_channels and channel_group)."""
        out = set()
        for tag, v in self.decl[fn]:
            if tag == catalog.ROLE_CHANNELS:
                out.update(_role_channels(v))
            elif tag == catalog.CHANNEL_GROUP:
                out.update(c for _, c in catalog.unpack_channel_group(v)[1])
        return out

    def _replace_plans(self, fns: set[int], got) -> None:
        """The plans of `fns` become `got` as one change (oep-if-plan §2.1): a channel leaving goes to its idle state, one in
        both the old and the new plan of a gpio keeps its state, and a new gpio line keeps the state it was
        in - an output idle keeps driving - until the first set (fixture §1); the idle modes 0-4 are the gpio modes of
        the same numbers."""
        old = {a for a in self.plan if a[0] in fns}
        # what stays is the same (fn, role, channel), not the same channel: a channel moving to another fn is released
        # (to the idle state the settings give it now) and then taken (oep-if-plan §2.1, core §8)
        kept = {a[2] for a in old & set(got) if self.names.get(a[0]) == "oep.fixture.gpio"}
        released = {a[2] for a in old - set(got)}
        for ch in {a[2] for a in old} - kept:
            self.gpio_modes.pop(ch, None)
        self.plan = {a for a in self.plan if a[0] not in fns} | (old & set(got))
        self._park(released)
        self.plan |= set(got)
        for fn, _, ch in got:
            if ch not in kept and self.names.get(fn) == "oep.fixture.gpio" and self.parked.get(ch, 0):
                self.gpio_modes[ch] = self.parked[ch]

    def _drop_plan(self, fn: int) -> None:
        released = set()
        for a in [a for a in self.plan if a[0] == fn]:
            self.gpio_modes.pop(a[2], None)
            self.plan.discard(a)
            released.add(a[2])
        self._uart_plan_changed(fn)
        self._capture_unplanned(fn)
        self._park(released)

    # ---- what the probe does to a pin now (core §8, fixture §2-§4, capture §1.2) -----------------------
    IDLE_NAMES = {v: k.replace("_", "-") for k, v in IDLE_MODE.items()}

    def pin_state(self, ch: int) -> str:
        """TEST HOOK: the electrical state the probe gives channel `ch` now, as a word a test can compare:
        "reset" (disabled: never touched), "wire" (a live connection's pin at its rest state between exchanges),
        "wire-free" (the same after an exchange went unanswered, until one succeeds: undriven, no pull - never a pull-up
        on a clock resting low; debug §2), "wire-free pull-up" (swio's one line then: released to its pull-up, never
        driven high; debug §3.2), "idle <mode>" (the idle item's, hi-z
        without one), "gpio <mode>" (after a set, or taken in its idle state), "uart-tx-high" / "input" (a fixture
        UART's TX at the plan, its RX), "open-drain" / "open-drain pull-up" (an i2c-target from configure: pulls low or
        releases, never drives high; pull-up only when it declares internal pull-ups), "miso-driven" / "miso-hi-z" /
        "input" (an spi-target from configure: MISO driven only while CS is active), "analog" (from start). Taking a
        plan changes nothing until the interface starts to use the pin (core §8), and a logic capture only listens."""
        if ch in self.disabled:
            return "reset"
        conn = next((c for c in self.conns.values() if ch in c.pair), None)
        if conn is not None:
            if not conn.free:
                return "wire"
            return "wire-free pull-up" if self.names.get(conn.fn) == "oep.wire.swio" else "wire-free"
        idle = f"idle {self.IDLE_NAMES.get(self.parked.get(ch, 0), 'hi-z')}"
        for fn, role, c in sorted(self.plan):
            if c != ch or self._listens(fn):
                continue
            name = self.names.get(fn)
            if name == "oep.fixture.gpio":
                mode = self.gpio_modes.get(ch)
                return idle if mode is None else f"gpio {mode}"
            if name == "oep.fixture.uart":
                if role == _UART.enum["role"]["tx"]:
                    return "uart-tx-high"
                return "input" if fn in self.uart_baud else idle   # RX: used from configure, else idle (fixture §2)
            if fn in self.i2c:
                if self.i2c[fn].state == 0:
                    return idle                                    # state 0: released, the idle state (fixture §3)
                return "open-drain pull-up" if self.i2c_pullups.get(fn) else "open-drain"
            if fn in self.spi:
                if self.spi[fn].state == 0:
                    return idle                                    # before configure: the idle state (fixture §4)
                if role == _SPI.enum["role"]["miso"]:
                    return "miso-driven" if fn in self.spi_selected else "miso-hi-z"
                return "input"
            if fn in self.captures:                                # the analog: its pad leaves the digital function at start
                acquiring = self.captures[fn].state in (virtual_bench_capture.STATE["waiting"],   # and comes back when
                                                        virtual_bench_capture.STATE["capturing"])  # it stops (capture §1.2)
                return "analog" if acquiring else idle
        return idle

    def spi_select(self, fn: int, active: bool) -> None:
        """TEST HOOK: the bus controller moves spi-target `fn`'s CS (active: selected). MISO is driven only while CS is
        active, from configure until the plan is released (fixture §4)."""
        if active:
            self.spi_selected.add(fn)
        else:
            self.spi_selected.discard(fn)

    # ---- oep.fixture.uart's stream: made by the plan, gone with it (fixture §2) ---------------------
    def _uart_plan_changed(self, fn: int) -> None:
        """fn's plan moved: a fixture UART with RX or TX gets its stream (carrying on from where the last one ended,
        common §1.1), not configured until the host's configure (fixture §2); one without loses it and its configure. A fixture
        I2C / SPI target goes back to not configured (state 0): its configure is made on the plan's pins."""
        if fn in self.i2c:
            self.i2c[fn] = I2cState()
        if fn in self.spi:
            self.spi[fn] = SpiState()
        if self.names.get(fn) != "oep.fixture.uart":
            return
        planned = any(a[0] == fn for a in self.plan)
        s = self.uarts.get(fn)
        if planned and s is None:
            s = self.uarts[fn] = Stream(kinds=UART_MARK)
            s.base, s.serial = self.uart_carry.get(fn, (0, 0))
        elif planned:                                              # a new set of pins, not configured (fixture §2):
            s.drop_oldest(len(s.data))                             # the buffer and the marks go; position and serial
            s.marks.clear()                                        # carry on
        elif not planned and s is not None:
            self.uart_carry[fn] = (s.end, s.serial)
            del self.uarts[fn]
            self.uart_baud.pop(fn, None)

    def _uart_actual(self, baud: int) -> int:
        return self.uart_clock_hz // max(1, self.uart_clock_hz // baud)

    def _uart_check(self, baud: int, fmt: int, fn: int, fmt_tag: int | None, divide: bool = True) -> int:
        """fixture §2's refusals: baud 0 -> malformed; an undefined format value or bit, and a format the UART does not
        declare -> unsupported (the format TLV's tag as received, or 0x00 for the item); a baud off by more than 5 % ->
        unsupported 0x00. -> the actual baud. `divide` False (the settings' item at set time): the range alone
        (1 .. the UART's max_clock_hz), the divider is made when the plan runs the UART (the default when it misses)."""
        if baud == 0:
            raise Reject(m.MALFORMED)
        # undefined values (2 / 3 in bit0-1, 3 in bit2-3, bits 5-7: a later revision may define them, core §2.5) and a
        # format not declared: unsupported alike
        if fmt & ~UART_FORMAT_MASK or fmt & 3 > 1 or (fmt >> 2) & 3 > 2 or fmt not in self.uart_formats.get(fn, {0}):
            raise Reject(m.UNSUPPORTED, bytes([fmt_tag if fmt_tag is not None else m.TAG_FIXED]))
        if not divide:
            if baud > self.uart_max_hz.get(fn, 3_000_000):
                raise Reject(m.UNSUPPORTED)
            return baud
        actual = self._uart_actual(baud)
        if abs(actual - baud) * 20 > baud:
            raise Reject(m.UNSUPPORTED)
        return actual

    def add_transport(self, kind: int, interface: int = 0xFF) -> int:
        """A transport the virtual bench also serves (virtual_bench_serve's TCP listener: kind 6, interface 0xFF): listed in fn 0's
        describe under the next unused index - an index is never reused (core §7.5) - which every confirm accepted on
        it reports (transports §1, core §7.1). -> its index."""
        one_only = {virtual_bench.TRANSPORT["vendor_bulk"], virtual_bench.TRANSPORT["hid"]}
        if kind in one_only and kind in self.transports.values():
            raise ValueError("a probe offers at most one vendor bulk and one HID interface (transports §3)")
        index = max(self.transports, default=-1) + 1
        self.transports[index] = kind
        self.static[0] = tuple(self.static.get(0, ())) + (catalog.tlv(virtual_bench.CORE_TRANSPORT, bytes([index, kind, interface])),)
        self.decl[0] = self.decl.get(0, []) + [(virtual_bench.CORE_TRANSPORT, bytes([index, kind, interface]))]
        if kind in virtual_bench.SERIAL_KINDS:
            self.serial_ports.add(index)
        return index

    # ---- describe: the static declarations plus the live ones -----------------------------------
    def _declarations(self, fn: int) -> list[bytes]:
        """describe: the profile's declarations as they are (core §7.3: nothing that changes - the firmware's labels
        only, the settings' are read by get; the settings' state is fn 0's op state), the ops tag first and as `offers`
        answers (oep.probe.link's port_speed follows `port_speed_base`: a test turns it off or on); oep.probe.restart's
        restart_max_ms is `restart_max_ms` (oep-if-restart §1)."""
        ops = {op for op in range(256) if self.offers(fn, op)}
        if fn in self.broken_ops:
            head = [catalog.tlv(catalog.OPS, self.broken_ops[fn])]
        else:
            head = [catalog.ops_tlv(ops)] if ops else []           # no op at all: no ops tag (a test's probe)
        out = head + [t for t in self.static[fn] if t[0] != catalog.OPS]
        if fn == self.restart_fn:                                  # restart_max_ms as the endpoint has it now
            out = [catalog.u32(virtual_bench.RESTART_MAX_MS_TAG, self.restart_max_ms) if t[0] == virtual_bench.RESTART_MAX_MS_TAG else t
                   for t in out]
        return out

    # ---- oep.probe.link (oep-if-link): the link test and port_speed -------------------------------------
    def _link(self, fn: int, op: int, t: Take) -> tuple[int, int, bytes]:
        """source: length(u32) [TLV] -> len(u16) data [TLV], byte k = k & 0xFF, at most max_frame - 7 (the answer's
        header and len); sink: count(u16) data [TLV] -> empty, a count past the bytes that follow malformed; port_speed
        (§3)."""
        if op == _LINK.op["source"]:
            length = t.take("I")
            t.tail()
            n = min(length, self.probe.max_frame - LINK_SOURCE_OVERHEAD)   # oep-if-link §2
            return self._answer(struct.pack("<H", n) + bytes(k & 0xFF for k in range(n)))
        if op == _LINK.op["sink"]:
            count = t.take("H")
            t.bytes(count)                                         # count past the bytes that follow: malformed
            t.tail()
            return self._answer(b"")
        if op == OP_PORT_SPEED:
            return self._port_speed(t)
        if op == OP_PORT_STATE:                                    # [TLV] -> baud(u32) state(u8) (oep-if-link §3)
            t.tail()
            port = self._transport
            if self.transports.get(port) != virtual_bench.TRANSPORT["uart_bridge"]:
                raise unavailable("wrong_state")                   # only a UART bridge's port
            return self._answer(struct.pack("<IB", self.port_baud(port), self.speed_phase(port)))
        return m.REJECTED, m.UNKNOWN_OPERATION, b""

    # ---- port_speed (oep-if-link §3) --------------------------------------------------------------
    @staticmethod
    def speed_actual(baud: int) -> int:
        """The rate the virtual bench's UART makes nearest `baud`: any rate in SPEED_RATES exactly, else the nearest end."""
        return min(max(baud, SPEED_RATES[0]), SPEED_RATES[1])

    def _port_speed(self, t: "Take") -> tuple[int, int, bytes]:
        """baud(u32) step(u8) verify_ms(u16) [TLV] -> baud(u32): the rate that applies. The port is the one the request
        came on: only a UART bridge (else unavailable cause 6)."""
        baud, step, verify_ms = t.take("IBH")
        t.tail()
        if step == SPEED_STEP["try"] and verify_ms == 0:
            raise Reject(m.MALFORMED)                              # verify_ms 0 in a try (oep-if-link §3)
        if step not in SPEED_STEP.values():
            raise Reject(m.UNSUPPORTED)                            # a step the definition leaves unused (§2.5): 0x00
        port = self._transport
        if self.transports.get(port) != virtual_bench.TRANSPORT["uart_bridge"]:
            raise unavailable("wrong_state")                       # only a UART bridge's port (oep-if-link §3)
        # the port's phase (0 boot, 1 trying, 2 committed, 3 a return waiting for a frame boundary) decides which step
        # fits (oep-if-link §3): any other is cause 6, and so is a try on another port while one is raised
        if self.speed_fallback is not None:
            raise unavailable("wrong_state")                       # phase 3 refuses every step
        if step == SPEED_STEP["try"]:
            if self.speed_state != "base":
                raise unavailable("wrong_state")                   # trying or committed (here or on another port)
            actual = self.speed_actual(baud)
            if abs(actual - baud) * 100 > baud * SPEED_TOLERANCE_PCT or actual <= (self.port_speed_base or 115200):
                raise unsupported_fixed()                          # more than 2 % off, or not above the boot speed
            self.speed_pending = ("switch", port, actual, baud, verify_ms)
            self.speed_owner = self.holder
            return self._answer(struct.pack("<I", actual))
        if step == SPEED_STEP["commit"]:
            if self.speed_state != "try" or port != self.speed_port or baud != self.speed_asked:
                raise unavailable("wrong_state")                   # nothing tried, committed already, or another baud
            self.speed_state = "committed"
            self.speed_good_ms = self.now()
            return self._answer(struct.pack("<I", self.speed_rate))
        if self.speed_state == "base" or port != self.speed_port:
            raise unavailable("wrong_state")                       # a revert at the boot speed: nothing to go back from
        self.speed_pending = ("revert",)
        return self._answer(struct.pack("<I", self.port_speed_base))

    def port_baud(self, port: int) -> int:
        """The rate serial port `port` runs at now (its boot speed, or what port_speed set)."""
        if self.speed_state != "base" and port == self.speed_port:
            return self.speed_rate
        return self.port_speed_base or 115200

    def breaks(self, port: int, size: int, to_host: bool, duplex: bool = False) -> bool:
        """The line model: a frame of `size` bytes on `port` at its rate now breaks (`broken_rates`). duplex: the other
        way carries a frame of the rate's min_size or more at the same time (a BrokenRate with `duplex` breaks only
        then)."""
        b = self.broken_rates.get(self.port_baud(port))
        if b is None:
            return False
        if b.carried < b.after:
            b.carried += size
            return False
        if size < b.min_size or not (b.to_host if to_host else b.to_probe) or (b.duplex and not duplex):
            return False
        return b.hit()

    def duplex_rate(self, port: int) -> BrokenRate | None:
        """The rate `port` runs at now when it breaks only both ways at once (virtual_bench_serial checks the unread answers)."""
        b = self.broken_rates.get(self.port_baud(port))
        return b if b is not None and b.duplex else None

    def after_answer(self) -> None:
        """The answer just handled is out (its transport calls this once the answer has left - at the old speed): a
        port_speed switch or revert it asked for happens now (`speed_after_answer`), and a restart it answered
        (oep-if-restart §2) restarts the probe now (`reboot`: a new boot_id, the saved settings, no session; the pins in their
        free state, nothing driven before). A test calling `handle` itself on a serial port index calls this as
        VirtualSerialPort does."""
        self.speed_after_answer()
        if self.speed_fallback is not None and not self.speed_frame_busy:
            self._speed_revert()                                   # a return that waited for this answer (link §3)
        if self.restarting:
            self.reboot()

    def speed_after_answer(self) -> None:
        """The answer that asked for a switch or a revert is out (at the old speed): now do it."""
        pending, self.speed_pending = self.speed_pending, None
        if pending is None:
            return
        if pending[0] == "revert":
            self._speed_revert()
            return
        _, port, rate, asked, verify_ms = pending
        self.speed_state, self.speed_port, self.speed_rate, self.speed_asked = "try", port, rate, asked
        if rate in self.broken_rates:
            self.broken_rates[rate].carried = 0                    # `after` counts from this switch
        self.speed_until_ms = self.now() + verify_ms
        self.speed_good_ms = self.now()
        self.speed_log.append((port, rate))

    def _speed_revert(self) -> None:
        self.speed_fallback = None
        if self.speed_state == "base":
            return
        self.speed_log.append((self.speed_port, self.port_speed_base))
        self.speed_state, self.speed_port, self.speed_rate, self.speed_asked = "base", None, 0, 0
        self.speed_owner = None

    def speed_phase(self, port: int) -> int:
        """port_state's phase of `port` (oep-if-link §3): 0 boot, 1 trying, 2 committed, 3 a return pending."""
        if self.speed_state == "base" or port != self.speed_port:
            return 0
        if self.speed_fallback is not None:
            return 3
        return 1 if self.speed_state == "try" else 2

    def _speed_fall(self, why: str) -> None:
        """A return condition holds (oep-if-link §3): back at once, or - a frame on the port not finished - phase 3 until
        its boundary. A session's end is never cancelled, so it overrides an idle return."""
        if self.speed_state == "base":
            return
        if not self.speed_frame_busy:
            self._speed_revert()
        elif self.speed_fallback is None or why == "session":
            self.speed_fallback = why

    def speed_frame_done(self) -> None:
        """TEST HOOK: the frame on the raised port is finished: a pending return happens now."""
        self.speed_frame_busy = False
        if self.speed_fallback is not None:
            self._speed_revert()

    def _speed_due(self, transport: int) -> None:
        """A request frame arrives: a verify_ms or port_speed_idle_ms that has run out by now is a return condition even
        when no tick saw it yet (oep-if-link §3). On the raised port this frame is on the line, so the return waits for
        its answer (phase 3: a late commit is refused cause 6, answered at the raised rate, then back); elsewhere it
        happens at once."""
        if self.speed_state == "base" or self.speed_fallback is not None or self.speed_pending is not None:
            return
        now = self.now()
        why = ("verify" if self.speed_state == "try" and now >= self.speed_until_ms else
               "idle" if self.speed_state == "committed" and now - self.speed_good_ms >= SPEED_IDLE_MS else None)
        if why is None:
            return
        if transport == self.speed_port:
            self.speed_fallback = why
        else:
            self._speed_fall(why)

    def speed_frame(self, port: int, good: bool) -> None:
        """A candidate closed on serial port `port`: a frame (good) or not. A good frame restarts the committed rate's
        port_speed_idle_ms (oep-if-link §3, return condition 2); a broken one changes nothing."""
        if self.speed_state == "base" or port != self.speed_port or not good:
            return
        self.speed_good_ms = self.now()

    def speed_answered(self, port: int) -> None:
        """An answer went out on `port`: the committed rate's port_speed_idle_ms counts from it too (oep-if-link §3)."""
        if self.speed_state != "base" and port == self.speed_port:
            self.speed_good_ms = self.now()

    def _speed_tick(self) -> None:
        # Return condition 2 (committed, port_speed_idle_ms with no good frame) is not counted while a request executes
        # (oep-if-link §3, as the lease, §6.1). The virtual bench handles every request within one call and its clock does not
        # move meanwhile, so there is nothing to pause here: the answer restarts it (`speed_answered`).
        if self.speed_pending and self.speed_pending[0] == "revert":
            self.speed_after_answer()
        now = self.now()
        if self.speed_fallback is not None:
            return
        if self.speed_state == "try" and now >= self.speed_until_ms:
            self._speed_fall("verify")                             # no commit within verify_ms (return condition 1)
        elif self.speed_state == "committed" and now - self.speed_good_ms >= SPEED_IDLE_MS:
            self._speed_fall("idle")                               # return condition 2

    def _page(self, tlvs: list[bytes], first: int) -> bytes:
        budget = self.probe.max_frame - m.RESULT_HEADER - 1
        out, sent = b"", 0
        for t in tlvs[first:]:
            if out and len(out) + len(t) > budget:
                break
            out += t
            sent += 1
        return bytes([1 if first + sent < len(tlvs) else 0]) + out

    # ---- oep.wire.rvswd / swio ------------------------------------------------------------------
    def _wire(self, fn: int, op: int, t: Take) -> tuple[int, int, bytes]:
        rvswd = self.names[fn] == "oep.wire.rvswd"
        O = reg.WIRE_RVSWD.op
        if op == O["scan"]:                                        # scan: count(u8) pairs -> tried count found...
            count = t.take("B")
            pairs = [t.take("HH") for _ in range(count)]
            known = {_T_SCAN["max_speed"]} | ({_T_SCAN["idle_clock"]} if rvswd else set())
            got = t.tail(known, defer=True)
            t.fixed(got, _T_SCAN["max_speed"], 4)
            speeds = [(raw, struct.unpack("<I", v)[0]) for raw, v in t.repeated if raw & 0x7F == _T_SCAN["max_speed"]]
            if not count or len(set(pairs)) != len(pairs) or any(not hz for _, hz in speeds):
                raise Reject(m.MALFORMED)                          # no implicit list, no pair twice, no speed 0 (§1) -
            self._idle_clock(t, got)                               # in any copy of max_speed (core §2.3)
            t.check()                                              # an unknown critical TLV (core §2.3)
            for raw, hz in speeds:                                 # below min_clock_hz, a later copy too (core §2.3):
                if hz < self._min_clock(fn):                       # the tag as that copy came
                    raise Reject(m.UNSUPPORTED, bytes([raw]))
            for i, p in enumerate(pairs):                          # not a combination the declaration allows:
                if not self._allows(fn, p):                        # unsupported, 0x00 + its index (debug §1)
                    raise unsupported_fixed(m.tlv(_WIRE_INDEX, bytes([i])))
            for p in pairs:                                        # a pin disabled, with an output idle, or held
                self._refuse_named(fn, p)                          # (a live connection's included): every pair first
            found, tried = [], 0
            for p in pairs:
                if m.RESULT_HEADER + 2 + 9 * (len(found) + 1) > self.probe.max_frame:
                    break                                          # another hit might not fit: the rest next time (§1)
                tried += 1
                tg = self._target(fn, p)
                if tg.answers:                                     # the set-up pair and dmactive, left set (§1)
                    tg.bring_up(scratch=False)
                if tg.answers and tg.found:
                    found.append(struct.pack("<BHHI", 1, *p, tg.dmstatus()))   # 9 bytes, no length (§2.3)
            return m.COMPLETED, m.SUCCESS, struct.pack("<BB", tried, len(found)) + b"".join(found)
        if op == O["attach"]:                                      # attach: method swdio swclk max_speed [TLV] (§1, §3)
            method, swdio, swclk, max_speed = t.take("BHHI")
            known = {_T_ATTACH["reset"]} | ({_T_ATTACH["idle_clock"]} if rvswd else set())
            got = t.tail(known, defer=True)
            if not max_speed:
                raise Reject(m.MALFORMED)                          # max_speed 0 (§1)
            idle_clock = self._idle_clock(t, got)
            reset = t.fixed(got, _T_ATTACH["reset"], 4)
            t.check()
            if method not in self._attach_methods(fn):
                raise Reject(m.UNSUPPORTED)                        # not offered, or undefined: 0x00 (§3)
            if max_speed < self._min_clock(fn):
                raise Reject(m.UNSUPPORTED)                        # below min_clock_hz: 0x00 (§1)
            pair = (swdio, swclk)
            if not self._allows(fn, pair):
                raise Reject(m.UNSUPPORTED)                        # a pair the declaration does not allow (§1)
            if reset is not None:
                channel, hold_ms = struct.unpack("<HH", reset)
                for raw, v in t.repeated:                          # every copy, the first used (core §2.3)
                    ch, hold = struct.unpack("<HH", v) if raw & 0x7F == _T_ATTACH["reset"] else (channel, 0)
                    if ch not in self.reset_channels.get(fn, set()) or hold > self.max_op_ms:
                        # not a reset line here (§3), or longer than one request may take: the tag as received
                        raise Reject(m.UNSUPPORTED, bytes([raw]))
            self._refuse_named(fn, pair)                           # busy pins never join or evict (§1)
            if reset is not None:
                self._refuse_disabled([channel])                   # a disabled reset line: cause 5 (core §13.8)
                if self._output_idle(channel):                     # its idle drives it: nothing executed (debug §1)
                    raise unavailable("held_by_settings", channel)
                if any(a[2] == channel for a in self.plan) or channel in self._held() or channel in pair:
                    raise unavailable("pin_in_use", channel)               # this attach's own pins too (§3)
            if len([c for c in self.conns.values() if c.fn == fn]) >= self.max_connections.get(fn, 1):
                raise unavailable("limit")                         # no connection is closed to make room (§1)
            if self._next_resource > 0xFFFFFFFF:
                raise unavailable("limit")                         # the u32 space is spent: before the target is touched
            tg = self._target(fn, pair)                            # (core §9, §4.3 order 4)
            if reset is not None and tg.resets_through(channel):
                tg.silent_until_reset = False                      # held in reset and let go: it answers again
                tg.havereset = True                                # the reset leaves havereset pending: acked below (§3, §4.6)
                if not self._settled(tg, hold_ms):                 # silent past max_op_ms, the hold counted (debug §2)
                    return m.COMPLETED, m.FAILED, bytes([LINE])
            if not tg.answers:
                return m.COMPLETED, m.FAILED, bytes([LINE])        # failed: status [TLV] (common §3); nothing made
            if not tg.bring_up(scratch=True):                      # no speed reads PROGBUF0 back: failed (§1)
                return m.COMPLETED, m.FAILED, bytes([LINE])
            if method == 1 and tg.halt_stuck:                      # hart 0 never halts: no success (§3)
                tg.haltreq = False
                return m.COMPLETED, m.FAILED, bytes([TIMEOUT])
            if method == 1 and 0x10 in tg.dmi:                     # method 1 selects hart 0 (hartsel 0, haltreq down)
                tg.dmi[0x10] &= ~HARTSEL
            flags = 0
            fixed_hz = self._min_clock(fn) if self.names[fn] == "oep.wire.swio" else 0   # swio: no speed chosen (§3.2)
            cid = self._seat(fn, pair, tg, fixed_hz or min(4_000_000, max_speed))
            if tg.havereset:
                tg.havereset, flags = False, flags | ATTACH_FLAGS["havereset_acked"]
            c = self.conns[cid]
            if rvswd:                                              # the host's TLV, the setting, else from target_id (§3.1)
                setting = (self._value(fn, "rest.{}", pair[1]) or bytes([REST_MODE["auto"]]))[0]
                c.idle_clock = (idle_clock if idle_clock is not None
                                else _IDLE_CLOCK["high"] if setting == REST_MODE["high"]
                                else _IDLE_CLOCK["low"] if setting == REST_MODE["low"] else rvswd_rest(c.tid))
            if reset is not None and tg.reset_line not in (None, channel):
                if method == 1:                                    # a line that resets nothing: halted where it ran
                    tg.halted, tg.dpc = True, tg.reset_vector + 0x2f8
            elif reset is not None:                                # the reset line held, then let go: a host reset
                tg.halted = method == 1
                tg.dpc = tg.reset_vector if method == 1 else tg.reset_vector + 0x200
            elif method == 1:
                tg.halted = True
            tail = b"" if c.tid is None else m.tlv(_T_ATTACH_ANSWER["target_id"],
                                                   bytes([TARGET_ID_SCHEME["dmi_7f"]]) + struct.pack("<I", c.tid))
            if tg.halted and method == 1:                          # bit3: the probe saw hart 0 halted (§3)
                flags |= ATTACH_FLAGS["halted"]
                tail += m.tlv(_T_ATTACH_ANSWER["dpc"], struct.pack("<I", tg.dpc))
            if rvswd:                                              # the rest in use, always (§3.1)
                tail += m.tlv(_T_ATTACH_ANSWER["idle_clock"], bytes([c.idle_clock]))
            return self._answer(struct.pack("<IIBI", cid, tg.dmstatus(), flags, c.speed) + tail)
        if op == O["detach"]:
            cid = t.take("I")
            t.tail()
            c = self._connection(cid)
            if c.fn != fn:
                raise wrong_kind()                                 # another wire's connection
            self._close_conn(cid)
            return m.COMPLETED, m.SUCCESS, b""
        if op == O["holds"]:                                       # lock-free, paged by first (debug §2.2)
            first = t.take("B")
            t.tail()
            rows = [struct.pack("<HHB", *pair, rest) for pair, rest in self.held_ports(fn)]
            return m.COMPLETED, m.SUCCESS, self._paged(rows, first)
        if op == O["connections"]:                                 # lock-free, paged by first (debug §2.1)
            first = t.take("B")
            t.tail()
            mine = sorted((c.order, cid) for cid, c in self.conns.items() if c.fn == fn)
            rows, rests = [], []
            for _, cid in mine:
                c = self.conns[cid]
                tid = b"" if c.tid is None else struct.pack("<I", c.tid)
                scheme = TARGET_ID_SCHEME["dmi_7f"] if tid else 0
                rows.append(struct.pack("<IHHIBB", cid, *c.pair, c.speed, scheme, len(tid)) + tid)
                rests.append(c.idle_clock)
            # rvswd: TLV idle_clock (entry_index, rest) for every entry of the page (§2.1, §3.1)
            return m.COMPLETED, m.SUCCESS, self._paged(rows, first, rests if rvswd else None)
        return m.REJECTED, m.UNKNOWN_OPERATION, b""

    def _paged(self, rows: list[bytes], first: int, rests: list[int] | None = None) -> bytes:
        """more(u8) count(u8) the rows from `first` that fit the frame (connections, streams: core §2.3's lists);
        with `rests` (rvswd's connections), each row's TLV idle_clock (entry_index, rest) after them, counted in."""
        budget = self.probe.max_frame - m.RESULT_HEADER - 2
        out: list[bytes] = []
        tail: list[bytes] = []
        for i, row in enumerate(rows[first:]):
            extra = b"" if rests is None else m.tlv(_T_CONNECTIONS_ANSWER["idle_clock"], bytes([len(out), rests[first + i]]))
            if out and sum(map(len, out + tail)) + len(row) + len(extra) > budget:
                break
            out.append(row)
            if extra:
                tail.append(extra)
        return bytes([int(first + len(out) < len(rows)), len(out)]) + b"".join(out) + b"".join(tail)

    def _attach_methods(self, fn: int) -> set[int]:
        """method 0, and 1 when the wire declares features bit0 attach_halt (debug §3)."""
        halt = self._own(fn, catalog.FEATURES, "I", 0) & reg.WIRE_RVSWD.enum["features"]["attach_halt"]
        return {ATTACH_METHOD["run"]} | ({ATTACH_METHOD["halt"]} if halt else set())

    def _min_clock(self, fn: int) -> int:
        return self._own(fn, catalog.MIN_CLOCK_HZ, "I", 0)

    def _refuse_named(self, fn: int, pair: tuple[int, int], any_idle: bool = False) -> None:
        """A pair a request names (scan's list, attach's pins): a disabled channel or an output idle (any idle with
        `any_idle`) -> unavailable cause 5; held by a plan or another connection -> cause 1 (debug §1, core
        §8.1); each with the channel."""
        self._refuse_disabled(pair)
        for ch in pair:
            if ch != 0xFFFF and (self._output_idle(ch) or (any_idle and self._idle_mode(ch) is not None)):
                raise unavailable("held_by_settings", ch)
        held = set(pair) & self._held()                         # a live connection's own pins too (§1)
        if held:
            raise unavailable("pin_in_use", min(held))

    def _idle_clock(self, t: Take, got: dict[int, bytes]) -> int | None:
        """rvswd's idle_clock TLV (u8 enum: 0 high, 1 low): another value is one the definition leaves unused ->
        unsupported with the tag as received, critical or not (core §2.3, §2.5)."""
        tag = _T_ATTACH["idle_clock"]
        value = t.fixed(got, tag, 1)
        if value is None:
            return None
        for raw, v in t.repeated:                                  # a later copy is checked too (core §2.3)
            if raw & 0x7F == tag and v[0] not in reg.WIRE_RVSWD.enum["idle_clock"].values():
                raise Reject(m.UNSUPPORTED, bytes([raw]))          # the tag as that copy came
        return value[0]

    def _conn_at(self, fn: int, pair: tuple[int, int]) -> int | None:
        return next((cid for cid, c in self.conns.items() if c.fn == fn and c.pair == pair), None)

    def _seat(self, fn: int, pair: tuple[int, int], tg: VirtualTarget, speed: int) -> int:
        """A new connection; a full wire is refused unavailable cause 2 - none is closed to make room (debug §1)."""
        if sum(1 for c in self.conns.values() if c.fn == fn) >= self.max_connections.get(fn, 1):
            raise unavailable("limit")
        cid = self._new_resource("connection")                    # one u32 space with the streams (core §9)
        self._order += 1
        tid = None if tg.target_id in (0, 0xFFFFFFFF) else tg.target_id   # scheme 1's "none" (debug §3)
        self.conns[cid] = Connection(fn, pair, self._order, speed, tid)
        return cid

    def _close_conn(self, cid: int, lost: bool = False) -> None:
        """The connection goes, its console streams first (common §2). Its lines stay held at its rest unless the line
        was lost, the port's wire-hold is off or a pin has an idle / disable item (debug §2); else to their idle state,
        and a target that needs the drive loses its halt."""
        for sid in [k for k, st in self.streams.items() if st.conn == cid]:
            self._close_stream(sid)
        gone = self.conns.pop(cid, None)
        self.resources.pop(cid, None)
        if gone is None:
            return
        if not lost and self._may_hold(gone.pair, gone.fn):
            self.holds[(gone.fn, gone.pair)] = gone.idle_clock if self.names[gone.fn] == "oep.wire.rvswd" else 0
            return
        self._park(gone.pair)
        tg = self.targets.get((gone.fn, gone.pair))
        if tg is not None and tg.needs_drive:
            tg.halted = False                                      # its debug module dropped the link: the hart runs

    def _may_hold(self, pair: tuple[int, int], fn: int | None = None) -> bool:
        """debug §2: hold.{} not false (no value: hold) for the port's SWDIO, and no idle / disable on its pins."""
        holds = [self._value(f, "hold.{}", pair[0]) for f in ([fn] if fn is not None else self.pairs)]
        pins = [p for p in pair if p != 0xFFFF]
        return (b"\x00" not in holds and not any(self._idle_mode(p) is not None for p in pins)
                and not any(p in self.disabled for p in pins))

    def held_ports(self, fn: int) -> list[tuple[tuple[int, int], int]]:
        """The ports wire `fn` holds now (debug §2.2): a hold ends once its pins are taken (a plan, a connection), its
        wire-hold goes off or an idle / disable item lands on them; an ended hold's pins go to their free state."""
        busy = {a[2] for a in self.plan if not self._listens(a[0])} | {p for c in self.conns.values() for p in c.pair}
        for key in list(self.holds):
            hfn, pair = key
            if any(p in busy for p in pair if p != 0xFFFF) or not self._may_hold(pair, hfn):
                del self.holds[key]
                if not any(p in busy for p in pair if p != 0xFFFF):
                    self._park(pair)
                    tg = self.targets.get(key)
                    if tg is not None and tg.needs_drive:
                        tg.halted = False
        return [(pair, rest) for (hfn, pair), rest in self.holds.items() if hfn == fn]

    def _cleanup_failed(self, cid: int, done: int, payload: bytes) -> tuple[int, int, bytes]:
        """A block op whose restore of DATA0 / DATA1 failed: partial when done > 0 (all words included), else failed,
        status the failure; then the connection and its streams close (debug §4)."""
        self._close_after_answer(cid)
        return m.COMPLETED, m.PARTIAL if done else m.FAILED, payload

    def _close_after_answer(self, cid: int) -> None:
        """A high-level op whose cleanup could not finish answers, then the connection and its streams close (debug §4,
        common §2). The bench answers within the call, so closing here is what the host sees after the answer."""
        self._close_conn(cid)

    def _target_of(self, cid: int) -> VirtualTarget:
        c = self.conns[cid]
        return self._target(c.fn, c.pair)

    # ---- oep.target.riscv-dm --------------------------------------------------------------------
    @staticmethod
    def _outcome(status: int, done: int) -> int:
        return m.SUCCESS if status == OK else (m.PARTIAL if done else m.FAILED)

    def _dm(self, fn: int, op: int, t: Take) -> tuple[int, int, bytes]:
        """oep.target.riscv-dm (debug §4); `_dm_op` does the op. An op whose connection's target does not answer
        on the wire (`VirtualTarget.answers` False) fails with status line and nothing done: its exchanges went unanswered,
        and from then until an exchange succeeds the probe rests the connection's lines in the free state (debug §2,
        `Connection.free`, `pin_state` "wire-free")."""
        try:
            return self._dm_op(fn, op, t)
        except _Unanswered as e:
            e.conn.free = True
            return m.COMPLETED, m.FAILED, DM_LINE_FAILED[op]

    @staticmethod
    def _hart0(tg: VirtualTarget) -> VirtualTarget:
        """A high-level op works on hart 0: it sets DMCONTROL.hartsel to 0 and leaves it 0 (debug §4) - a hartsel the
        host chose with dmi lasts only for that dmi request."""
        if 0x10 in tg.dmi:
            tg.dmi[0x10] &= ~HARTSEL
        return tg

    def _exchange(self, cid: int) -> VirtualTarget:
        """A target op's exchanges on connection `cid` (after the request's checks): the target, or _Unanswered.
        A success after failures restores the connection's rest state (debug §2: rvswd §3.1, swio §3.2)."""
        c = self._connection(cid)
        tg = self._target_of(cid)
        if not tg.answers:
            raise _Unanswered(c)
        c.free = False
        return tg

    def _dm_op(self, fn: int, op: int, t: Take) -> tuple[int, int, bytes]:
        """Each op reads and checks its whole request first - malformed, then unsupported (core §4.3 orders 5 / 6) -
        and only then looks up the connection (no_resource; every check before any change, core §4.3)."""
        conn = t.take("I")
        if op == _RV.op["dmi"]:                                    # raw dmi adds nothing, keeps the host's hartsel
            target = lambda: self._exchange(conn)
        elif op == _RV.op["reset"]:                                # reset acks its own; hart 0, hartsel left 0 (§4)
            target = lambda: self._hart0(self._exchange(conn))
        else:                                                      # a high-level op acks a pending havereset (§4.6)
            target = lambda: self._hart0(self._ack_havereset(conn, self._exchange(conn)))
        if op == _RV.op["dmi"]:
            n = t.take("H")
            steps = []
            for _ in range(n):
                kind = t.take("B")
                if kind not in STEP_ARGS:
                    raise Reject(m.UNSUPPORTED)                    # its length is unknown: stop there, run nothing (§4.1)
                steps.append((kind, t.take(STEP_ARGS[kind])))
            t.tail()
            # the time-based waits only: 0x04's wait_us and 0x05's max_us; 0x03 is bounded by its count (debug §4.1)
            waits_us = sum(a if k == STEP["wait_us"] else a[3] if k == STEP["poll_us"] else 0 for k, a in steps)
            if waits_us > self.max_op_ms * 1000:
                raise Reject(m.UNSUPPORTED)                        # longer than one request may take (debug §4.1)
            reads = sum(k in (STEP["read"], STEP["poll_reads"], STEP["poll_us"]) for k, _ in steps)
            if 10 + 4 * reads > self.probe.max_frame:
                raise Reject(m.UNSUPPORTED)                        # the success answer would not fit (§4.1, core §4.4)
            tg = target()
            tg.begin_dmi()
            done, status, values = 0, OK, []
            for kind, args in steps:
                if kind == STEP["write"]:
                    address, value = args
                    if address in tg.fail_write:
                        status = LINE
                        break
                    tg.write_dmi(address, value)
                elif kind == STEP["read"]:
                    v = tg.read_dmi(args)
                    if v is None:
                        status = LINE
                        break
                    values.append(v)
                elif kind in (STEP["poll_reads"], STEP["poll_us"]):
                    address, mask, want, limit = args
                    tries = limit if kind == STEP["poll_reads"] else max(1, limit // 100)
                    for _ in range(max(1, tries)):
                        v = tg.read_dmi(address)
                        if v is None or v & mask == want:
                            break
                    if v is None:                                  # cut off by the line: no value (debug §4.1)
                        status = LINE
                        break
                    values.append(v)
                    if v & mask != want:
                        status = TIMEOUT
                        break
                done += 1
            return (m.COMPLETED, self._outcome(status, done),
                    struct.pack(f"<HBH{len(values)}I", done, status, len(values), *values))   # done status nvals values
        if op == _RV.op["halt"]:
            t.tail()
            tg = target()
            if tg.halted:
                return m.COMPLETED, m.SUCCESS, bytes([OK])         # already halted: nothing, ok (debug §4.2)
            if tg.halt_stuck:                                      # allhalted never seen: haltreq cleared (debug §4.2)
                tg.haltreq = False
                return m.COMPLETED, m.FAILED, bytes([TIMEOUT])
            tg.halted = True
            return m.COMPLETED, m.SUCCESS, bytes([OK])
        if op == _RV.op["resume"]:
            t.tail()
            tg = target()
            if tg.resume_misses:                                  # a CH32V006 now and then: the request does not take
                tg.resume_misses -= 1
                return m.COMPLETED, m.FAILED, bytes([STATE])
            if tg.halted:
                tg.halted, tg.dpc = False, tg.dpc + 0x40
            return m.COMPLETED, m.SUCCESS, bytes([OK])
        if op == _RV.op["reset"]:
            mode = t.take("B")
            t.tail()                                               # revision 1 defines no request TLV (0x01 reserved)
            if mode not in RESET_MODE.values():
                raise Reject(m.UNSUPPORTED)                        # a mode the definition leaves unused: 0x00 (§2.5)
            tg = target()
            tg.havereset = True
            if not self._settled(tg):                              # the DM still silent at max_op_ms (§4.3)
                return m.COMPLETED, m.FAILED, struct.pack("<BBI", LINE, 0, 0)   # the connection kept
            tg.halted = mode == RESET_MODE["halt_at_reset"]
            tg.dpc = tg.reset_vector if mode == RESET_MODE["halt_at_reset"] else tg.reset_vector + 0x200
            tg.havereset = tg.haltreq = False                      # havereset acknowledged, haltreq cleared (§4.3)
            self._host_reset(conn, MARK_RESET["ndmreset"])         # a successful reset op: one mark reset (§4.6)
            # debug §4.3: status flags(bit0 reached the mode's state) pc (mode 2: dpc)
            flags, pc = (0b01, 0) if mode == 0 else (0b01, tg.dpc)
            return self._answer(struct.pack("<BBI", OK, flags, pc))
        if op == _RV.op["step"]:
            t.tail()
            tg = target()
            not_ok = struct.pack("<BBII", STATE, 0, 0, 0)            # moved and both dpc 0 unless status ok (§4.2)
            if not tg.halted:
                return m.COMPLETED, m.FAILED, not_ok
            before = tg.dpc
            if tg.step_stuck is not None:
                # not back in debug mode: haltreq, and a wait for the halt (debug §4.2)
                if tg.step_stuck == "halts":                       # halted: dcsr.step cleared, DATA1 / DATA0 restored
                    tg.dpc, tg.haltreq, tg.dcsr_step = tg.dpc + 0x20, False, False
                    return m.COMPLETED, m.FAILED, not_ok               # the host reads dpc with dmi
                tg.halted, tg.haltreq, tg.dcsr_step = False, False, True   # runs on, dcsr.step may still be set
                self._close_after_answer(conn)                     # the cleanup could not finish (debug §4.2)
                return m.COMPLETED, m.FAILED, not_ok + m.tlv(STEP_LEFT, b"")
            tg.dpc += 4
            return m.COMPLETED, m.SUCCESS, struct.pack("<BBII", OK, 1, before, tg.dpc)
        if op == _RV.op["read_block"]:
            address, count = t.take("IH")
            t.tail()
            if address % 4:
                raise Reject(m.MALFORMED)                          # not a word address (debug §4.5)
            if 4 * count > self.block_max.get(fn, 1 << 16):
                raise unsupported_fixed()                          # past the declared max_length (bytes; debug §4.5)
            if m.RESULT_HEADER + 3 + 4 * count > self.probe.max_frame:
                raise unsupported_fixed()                          # the answer would not fit the route (§4.5, core §4.4)
            if not count:
                self._connection(conn)
                return m.COMPLETED, m.SUCCESS, struct.pack("<HB", 0, OK)   # count 0: success, done 0 (debug §4.5)
            tg = target()
            if not tg.halted:
                return m.COMPLETED, m.FAILED, struct.pack("<HB", 0, STATE)   # a running hart (debug §4.5)
            words, status = [], OK
            for i in range(count):
                if address + 4 * i in tg.fault_at:
                    status = FAULT
                    break
                words.append(tg.mem.get(address + 4 * i, 0))
            if tg.restore_fails:                                   # the cleanup lost the line (debug §4)
                return self._cleanup_failed(conn, len(words), struct.pack(f"<HB{len(words)}I", len(words), LINE, *words))
            return (m.COMPLETED, self._outcome(status, len(words)),
                    struct.pack(f"<HB{len(words)}I", len(words), status, *words))
        if op == _RV.op["write_block"]:
            address, count = t.take("IH")
            words = struct.unpack(f"<{count}I", t.bytes(4 * count))
            t.tail()
            if address % 4:
                raise Reject(m.MALFORMED)
            if 4 * count > self.block_max.get(fn, 1 << 16):
                raise unsupported_fixed()                          # past the declared max_length (debug §4.5)
            if not count:
                self._connection(conn)
                return m.COMPLETED, m.SUCCESS, struct.pack("<HB", 0, OK)   # count 0: success, done 0 (debug §4.5)
            tg = target()
            if not tg.halted:
                return m.COMPLETED, m.FAILED, struct.pack("<HB", 0, STATE)
            done, status = 0, OK
            for i, w in enumerate(words):
                if address + 4 * i in tg.fault_at:
                    status = FAULT
                    break
                tg.mem[address + 4 * i] = w
                done += 1
            if tg.restore_fails:                                   # the cleanup lost the line (debug §4)
                return self._cleanup_failed(conn, done, struct.pack("<HB", done, LINE))
            return m.COMPLETED, self._outcome(status, done), struct.pack("<HB", done, status)
        if op == _RV.op["run"]:
            pc, timeout_ms, n = t.take("IIB")
            regs = dict(t.take("HI") for _ in range(n))
            n_out = t.take("B")
            outs = [t.take("H") for _ in range(n_out)]
            t.tail()
            if timeout_ms > self.max_op_ms:
                raise Reject(m.UNSUPPORTED)                        # longer than one request may take (debug §4.4)
            if m.RESULT_HEADER + 11 + 4 * n_out > self.probe.max_frame:
                raise unsupported_fixed()                          # the answer would not fit the route (core §4.4)
            tg = target()
            not_run = struct.pack("<BIIB", RUN_STOPPED["not_run"], 0, 0, 0)   # stopped 3, dpc invalid, nvals 0 (Q1)
            if not tg.halted:                                      # nothing prepared on a running hart: not run
                return m.COMPLETED, m.FAILED, bytes([STATE]) + not_run
            if set(regs) & tg.fail_regs:                           # a register write before the run failed on the line
                return m.COMPLETED, m.FAILED, bytes([LINE]) + not_run   # not run, still halted (debug §4.4)
            tg.regs.update(regs)
            if timeout_ms == 0:                                    # the limit passes at once: halted where it began
                tg.dpc = pc
                values = [tg.regs.get(r, 0) for r in outs]
                return (m.COMPLETED, m.FAILED, struct.pack(f"<BBIIB{n_out}I", TIMEOUT, RUN_STOPPED["timeout_halted"],
                                                           pc, 0, n_out, *values))
            stopped, dpc, us = tg.run_hook(pc, tg.regs) if tg.run_hook else (True, pc + 0x10, 50)
            if not stopped and tg.unstoppable:                     # the limit passed and the hart would not halt
                return m.COMPLETED, m.FAILED, struct.pack("<BBIIB", TIMEOUT, RUN_STOPPED["not_halted"], 0, us, 0)
            tg.dpc = dpc
            status = OK if stopped else TIMEOUT
            values = [tg.regs.get(r, 0) for r in outs]
            code = RUN_STOPPED["stopped"] if stopped else RUN_STOPPED["timeout_halted"]
            return (m.COMPLETED, m.SUCCESS if stopped else m.FAILED,
                    struct.pack(f"<BBIIB{n_out}I", status, code, dpc, us, n_out, *values))   # ... nvals values
        return m.REJECTED, m.UNKNOWN_OPERATION, b""

    def _settled(self, tg: VirtualTarget, spent_ms: int = 0) -> bool:
        """The wait after a reset's release for a target that restarts by itself (debug §2, §3, §4.3): the DM answers
        after tg.restart_ms; the whole request - `spent_ms` already gone (an attach's hold_ms), the wait and the cleanup
        - ends within max_op_ms. -> whether it answered in time (the wait goes in `settle_log`)."""
        left = max(0, self.max_op_ms - spent_ms)
        self.settle_log.append(min(tg.restart_ms, left))
        return tg.restart_ms <= left

    def _ack_havereset(self, cid: int, tg: VirtualTarget) -> VirtualTarget:
        """A high-level op's automatic havereset acknowledgement (debug §4.6): a pending one (the target reset by
        itself) is acknowledged and the connection's console streams get a restart mark (detail havereset)."""
        if tg.havereset:
            tg.havereset = False
            now = self.now_ns()
            for st in self.streams.values():
                if st.conn == cid:
                    st.add_mark(CON_MARK["restart"], now, MARK_RESTART["havereset"])
        return tg

    def _host_reset(self, cid: int, detail: int) -> None:
        """The reset op's ndmreset on connection `cid` (detail 1): its stream gets one mark reset (console §2, debug
        §4.6) and no restart for the same reset."""
        now = self.now_ns()
        for st in self.streams.values():
            if st.conn == cid:
                st.add_mark(CON_MARK["reset"], now, detail)

    # ---- position streams (console, fixture.uart) -----------------------------------------------
    def _stream_args(self, op: int, t: Take):
        """A position stream op's request (common §1), read and checked before anything is looked up (core §4.3):
        read's from 4 or more -> unsupported (a from 3 arg above 0xFF names no mark kind: from now); a write of count 0
        takes nothing and succeeds (accepted = count)."""
        if op == _CON.op["read"]:
            frm, arg, mx = t.take("BQH")
            t.tail()
            if frm > 3:
                raise Reject(m.UNSUPPORTED)                        # a later revision may define it: 0x00 (§2.5)
            return frm, arg, mx
        if op == _CON.op["marks"]:
            frm = t.take("I")
            t.tail()
            return frm
        if op == _CON.op["mark"]:
            value = t.take("B")
            t.tail()
            return value
        if op == _CON.op["write"]:
            count = t.take("H")
            data = t.bytes(count)
            t.tail()
            return data
        if op in (_CON.op["clear"], _CON.op.get("close", -1)):
            t.tail()
        return None

    def _stream_op(self, s: Stream, op: int, args, t: Take, accept: int | None) -> tuple[int, int, bytes]:
        """read / marks / clear / mark / write of a position stream (common §1). `accept`: what a write takes (a
        fixture UART's queue); None: a console, its send queue (`send_queue`)."""
        if op == _CON.op["read"]:
            frm, arg, mx = args
            if frm == READ_FROM["position"]:
                pos = arg
            elif frm == READ_FROM["oldest"]:
                pos = s.base
            elif frm == READ_FROM["now"]:
                pos = s.end
            else:
                hits = [mk for mk in s.marks if arg == 0 or mk[2] == arg]
                pos = hits[-1][1] if hits else s.end                 # no such mark: from now (common §1.2)
            flags = 0
            if pos < s.base:
                pos, flags = s.base, 2
            if pos >= s.end:                                       # at or past the write position: nothing to read,
                return m.COMPLETED, m.SUCCESS, struct.pack("<QBH", s.end, flags & 2, 0)   # gap kept (common §1.2)
            budget = self.probe.max_frame - m.RESULT_HEADER - 11    # within max_frame (common §1.2)
            data = bytes(s.data[pos - s.base:pos - s.base + min(mx, budget)])
            if pos + len(data) < s.end:
                flags |= 1
            return m.COMPLETED, m.SUCCESS, struct.pack("<QBH", pos, flags, len(data)) + data   # start flags len data
        if op == _CON.op["marks"]:
            # common §1.3: from from_serial inclusive; from next (s.serial) nothing and more 0; a serial not kept (pushed
            # out, not given yet, an earlier boot's) from the oldest kept
            hits = s.marks[m.serial_page_start([mk[0] for mk in s.marks], s.serial, args):]
            fit = (self.probe.max_frame - m.RESULT_HEADER - 2) // 22             # more(u8) count(u8) 22 bytes a mark
            page = hits[:max(1, min(self.MARKS_PER_ANSWER, fit))]
            body = struct.pack("<BB", int(len(hits) > len(page)), len(page))
            body += b"".join(struct.pack("<IQBQB", *mk) for mk in page)   # 22 bytes each, time_ns u64 (common §1.3)
            return self._answer(body)
        kinds = s.kinds or MARK
        if op == _CON.op["clear"]:
            s.drop_oldest(len(s.data))
            s.add_mark(kinds["clear"], self.now_ns())
            return m.COMPLETED, m.SUCCESS, b""
        if op == _CON.op["mark"]:
            s.add_mark(kinds["host"], self.now_ns(), args)
            return m.COMPLETED, m.SUCCESS, b""
        if op == _CON.op["write"]:
            data, count = args, len(args)
            if accept is None:                                     # a console: its send queue (console §2)
                if s.mechanism not in self.console_feed:
                    raise Reject(m.UNSUPPORTED)                    # SDI has no host -> target way (§1, §3.1): 0x00
                took = min(count, self.send_queue - len(s.queue))
                if took:
                    if not s.queue:
                        s.fed_ms = self.now()                      # the first poll that can take them: a ms on
                    s.queue += data[:took]
            else:
                took = min(count, accept)                          # what fit the UART's transmit queue
                s.written += data[:took]
            return self._answer(struct.pack("<H", took),          # all = success, fewer = partial, none = failed
                                m.SUCCESS if took == count else m.PARTIAL if took else m.FAILED)   # (common §1.4)
        return m.REJECTED, m.UNKNOWN_OPERATION, b""

    # ---- oep.target.console ---------------------------------------------------------------------
    def _console(self, fn: int, op: int, t: Take) -> tuple[int, int, bytes]:
        if op == _CON.op["open"]:
            conn, mech = t.take("IB")
            t.tail()
            if mech not in self.mechanisms:
                raise Reject(m.UNSUPPORTED)                        # not a mechanism this probe opens: 0x00 (§1)
            self._connection(conn)                                 # no_resource, or unavailable 6 for a stream's number
            if any(st.conn == conn for st in self.streams.values()):
                raise unavailable("wrong_state")                   # one live stream a connection, any mechanism (§2)
            sid = self._new_resource("stream")                     # one u32 space with the connections (core §9)
            self._order += 1
            self.stream_order[sid] = self._order                   # streams lists them in the order they were made
            self.streams[sid] = Stream(conn=conn, mechanism=mech)
            return self._answer(struct.pack("<I", sid))
        if op == _CON.op["streams"]:                               # lock-free, paged by first(u16): the live streams
            first = t.take("H")
            t.tail()
            rows = [struct.pack("<IIB", sid, self.streams[sid].conn, self.streams[sid].mechanism)
                    for sid in sorted(self.streams, key=lambda k: self.stream_order.get(k, 0))]
            return m.COMPLETED, m.SUCCESS, self._paged(rows, first)
        if op not in _CON.op.values():
            return m.REJECTED, m.UNKNOWN_OPERATION, b""
        sid = t.take("I")
        args = self._stream_args(op, t)                            # the request first (core §4.3 orders 5 / 6) ...
        if op == _CON.op["read"] and args[0] == READ_FROM["last_mark"] and not self.offers(fn, _CON.op["marks"]):
            raise Reject(m.UNSUPPORTED)                            # from 3 needs marks (console §1, common §1)
        s = self._stream(sid)                                      # ... then no_resource (unavailable 6: a connection's)
        if op == _CON.op["close"]:
            self._close_stream(sid)
            return m.COMPLETED, m.SUCCESS, b""
        return self._stream_op(s, op, args, t, None)

    def _close_stream(self, sid: int) -> None:
        """The stream, its buffer and its send queue go (console §2); its number is never used again (core §9)."""
        self.streams.pop(sid, None)
        self.resources.pop(sid, None)
        self.stream_order.pop(sid, None)

    def console_take(self, sid: int | None = None) -> None:
        """TEST HOOK: the target takes everything console stream `sid`'s send queue holds (every stream when None), as
        after enough polls (console §2, §3)."""
        for k, st in self.streams.items():
            if (sid is None or k == sid) and st.queue:
                st.written += st.queue
                st.queue.clear()

    def _console_poll(self) -> None:
        """The probe's polling of the console streams (console §2, §3): one poll per ms on the timers' clock while the
        hart runs and answers, each handing the queue's head to the target - dmseq 2 bytes, DMDATA 3. A poll is an
        exchange on its connection's lines (debug §2, "要求の中でもコンソールの読みの中でも"): unanswered, the lines rest
        released (`Connection.free`) until an exchange succeeds; answered, the connection's rest state is back."""
        now = self.now()
        for st in self.streams.values():
            if st.conn not in self.conns:
                continue
            self.conns[st.conn].free = not self._target_of(st.conn).answers
            tg = self._target_of(st.conn)
            if tg.answers and not tg.halted:
                # the console's read acknowledges a pending havereset too, with a restart mark (debug §4.6)
                self._ack_havereset(st.conn, tg)
            if not st.queue or now <= st.fed_ms:
                continue
            tg = self._target_of(st.conn)
            if tg.answers and not tg.halted:
                n = min(len(st.queue), (now - st.fed_ms) * self.console_feed.get(st.mechanism, 0))
                st.written += st.queue[:n]
                del st.queue[:n]
            st.fed_ms = now

    def emit(self, sid: int, data: bytes) -> None:
        """The target writes to its console stream `sid`."""
        self.streams[sid].data += data

    def target_says(self, data: bytes, target: VirtualTarget | None = None) -> None:
        """The target (the first one by default) writes to its console: every open stream on a connection to it, and a
        serial port whose console pass-through is attached to it (console §2.1)."""
        target = target or self.target
        for st in self.streams.values():
            if st.conn in self.conns and self._target_of(st.conn) is target:
                st.data += data
        for port, (wfn, pair) in self.console_passthroughs().items():
            if self.targets.get((wfn, pair)) is target and target.answers and not target.halted:
                self.passthrough_out.setdefault(port, bytearray()).extend(data)

    # ---- oep.fixture.gpio -----------------------------------------------------------------------
    def _gpio(self, fn: int, op: int, t: Take) -> tuple[int, int, bytes]:
        mine = {ch for f, role, ch in self.plan if f == fn and role == 1}
        if op == _GPIO.op["set"]:
            n = t.take("B")
            pairs = [t.take("HB") for _ in range(n)]
            t.tail()                                               # no set TLV in revision 1 (fixture §1)
            index_tag = _GPIO.tlv["unavailable_payload"]["index"]
            for i, (ch, mode) in enumerate(pairs):
                # a mode the definition leaves unused (7 or more) or one it does not drive (fixture §1)
                if mode > GPIO_MODE_MAX or not self.gpio_allowed.get(fn, 0xFF) >> mode & 1:
                    raise unsupported_fixed(m.tlv(_UNA["channel"], struct.pack("<H", ch)), m.tlv(index_tag, bytes([i])))
            for i, (ch, mode) in enumerate(pairs):
                self._refuse_disabled([ch], m.tlv(index_tag, bytes([i])))   # disabled: cause 5 (core §13.8)
                if ch not in mine:                                 # the position as the gpio's TLV (fixture §1)
                    raise unavailable(channel=ch, extra=m.tlv(index_tag, bytes([i])))
            for ch, mode in pairs:
                self.gpio_modes[ch] = mode
                self.gpio_log.append((ch, mode))
            return self._answer(b"")
        if op == _GPIO.op["read"]:
            n = t.take("B")
            chans = [t.take("H") for _ in range(n)]
            t.tail()
            for i, ch in enumerate(chans):
                self._refuse_disabled([ch], m.tlv(_GPIO.tlv["unavailable_payload"]["index"], bytes([i])))
                if ch not in mine:
                    raise unavailable(channel=ch, extra=m.tlv(_GPIO.tlv["unavailable_payload"]["index"], bytes([i])))
            levels = []
            for ch in chans:
                mode = self.gpio_modes.get(ch, 0)
                outside = self.gpio_world(ch, mode) if self.gpio_world and mode in (0, 1, 2, 6) else None
                levels.append(outside if outside is not None else
                              {1: 1, 3: 0, 4: 1, 5: 0, 6: 1}.get(mode, self.gpio_inputs.get(ch, 0)))
            return self._answer(bytes([len(levels)]) + bytes(levels))   # n(u8) n x level [TLV] (fixture §1)
        return m.REJECTED, m.UNKNOWN_OPERATION, b""

    def _uart(self, fn: int, op: int, t: Take) -> tuple[int, int, bytes]:
        fmt_tag = _UART.tlv["configure"]["format"]
        if op == _UART.op["configure"]:
            baud = t.take("I")
            got = t.tail({fmt_tag})
            fmt = t.fixed(got, fmt_tag, 1)                         # another length: malformed (core §2.3)
            fmt = b"\0" if fmt is None else fmt
            for tag, later in t.repeated[1:]:                      # a later copy is checked for its value too (core §2.3)
                self._uart_check(baud, later[0], fn, tag)
            actual = self._uart_check(baud, fmt[0], fn, t.received(fmt_tag))   # unhandled: unsupported, bit 7 or not
            if fn not in self.uarts:
                raise unavailable("wrong_state")                   # no pins: the plan has neither RX nor TX (fixture §2)
            self.uarts[fn].written.clear()                         # unsent TX never carries over (fixture §2)
            if fn in self.uart_fail_configure:                     # taken, but the UART does not come up: not configured,
                self.uart_fail_configure.discard(fn)               # the stream's bytes and marks stay (fixture §2)
                self.uart_baud.pop(fn, None)
                return self._answer(struct.pack("<I", 0), m.FAILED)
            self.uart_baud[fn] = (actual, fmt[0])
            return self._answer(struct.pack("<I", actual))
        if op == _UART.op["status"]:                               # lock-free: baud(u32) format(u8) (fixture §2)
            t.tail()
            if fn not in self.uarts:
                raise unavailable("wrong_state")                   # no RX and no TX in a plan (fixture §2)
            baud, fmt = self.uart_baud.get(fn, (0, 0))             # 0 0: not configured, no implicit baud
            return self._answer(struct.pack("<IB", baud, fmt))
        names = {v: k for k, v in _UART.op.items()}
        if names.get(op) not in ("read", "marks", "mark", "write", "clear"):
            return m.REJECTED, m.UNKNOWN_OPERATION, b""
        op = _CON.op[names[op]]                                    # the same form as the console's (common §1)
        args = self._stream_args(op, t)                            # the request first (core §4.3 orders 5 / 6)
        if op == _CON.op["read"] and args[0] == READ_FROM["last_mark"] and not self.offers(fn, _UART.op["marks"]):
            raise Reject(m.UNSUPPORTED)                            # from 3 needs marks (fixture §2): 0x00
        s = self.uarts.get(fn)
        if s is None:
            raise unavailable("wrong_state")                       # the plan makes the stream (fixture §2)
        if op == _CON.op["write"] and (fn not in self.uart_baud or not any(
                a[0] == fn and a[1] == _UART.enum["role"]["tx"] for a in self.plan)):
            raise unavailable("wrong_state")                       # no TX in the plan, or not configured (fixture §2)
        return self._stream_op(s, op, args, t, self.uart_accept)

    def uart_rx(self, fn: int, data: bytes, channel: int | None = None) -> None:
        """Bytes arrive on fixture UART `fn`'s RX line: kept only while it is configured with RX in its plan (fixture
        §2: nothing is received before a configure, after a failed one, or on a TX-only plan). `channel`: the line they
        came on - a serial port's pass-through on it takes them while it runs (fixture §2.1)."""
        if fn in self.uart_baud and any(a[0] == fn and a[1] == _UART.enum["role"]["rx"] for a in self.plan):
            self.uarts[fn].data += data
            return
        for port, (pfn, rx) in self.passthroughs().items():
            if pfn == fn and (channel is None or channel == rx):
                self.passthrough_out.setdefault(port, bytearray()).extend(data)

    def passthroughs(self) -> dict[int, tuple[int, int]]:
        """The serial ports whose pass-through runs now (fixture §2.1) -> port: (fn, rx). One runs while no session
        request came on its port, its fn has no plan, and no plan or connection takes its rx."""
        busy = {a[2] for a in self.plan} | {p for c in self.conns.values() for p in c.pair}
        planned = {a[0] for a in self.plan}
        out = {}
        for (fn, port), v in self._values_of("passthrough.{}.rx").items():
            if self.names.get(fn) != _UART.name or port in self.session_ports:
                continue
            rx = struct.unpack("<H", v)[0]
            if fn not in planned and rx not in busy:
                out[port] = (fn, rx)
        return out

    def console_passthroughs(self) -> dict[int, tuple[int, tuple[int, int]]]:
        """The serial ports whose console pass-through runs and is attached now (console §2.1) -> port: (wire fn,
        pair). The condition: no session request on the port, no session connection or plan on its pins. When it
        starts, the probe attaches (method 0); unanswered, swio tries again at every look, rvswd only when the
        condition starts again (its wake may reset the target). Off: its connection closes, the target untouched."""
        busy = {a[2] for a in self.plan} | {p for c in self.conns.values() for p in c.pair}
        out = {}
        wires = self._values_of("passthrough.{}.wire")
        for port in set(self.resident) - {p for (_, p) in wires}:   # unset: the condition starts again with a set
            self.resident.pop(port)
        for (cfn, port), v in wires.items():
            get = lambda n: self._value(cfn, n, port)
            io, ck, mech = get("passthrough.{}.swdio"), get("passthrough.{}.swclk"), get("passthrough.{}.mechanism")
            wfn = struct.unpack("<H", v)[0]
            if io is None or mech is None or (ck is None and self.names.get(wfn) == "oep.wire.rvswd"):
                continue                                           # not every value there: no pass-through (console §2.1)
            pair = (struct.unpack("<H", io)[0], struct.unpack("<H", ck)[0] if ck else 0xFFFF)
            if port in self.session_ports or busy & {p for p in pair if p != 0xFFFF}:
                self.resident.pop(port, None)                      # closed, the target untouched (debug §2)
                continue
            tg = self.targets.get((wfn, pair))
            if port not in self.resident or (not self.resident[port] and self.names[wfn] == "oep.wire.swio"):
                self.resident[port] = tg is not None and tg.present and tg.answers
            if self.resident[port]:
                out[port] = (wfn, pair)
        return out

    def console_line_lost(self, port: int) -> None:
        """The console pass-through of `port` lost its line (debug §2, config §1.2): its connection closes; swio tries
        again at the next look, rvswd only when the condition starts again."""
        if self.resident.get(port):
            self.resident[port] = False

    def passthrough_take(self, port: int) -> bytes:
        """The pass-through bytes serial port `port` sends now, between frames (transports §4); none while it does
        not run (what was waiting is dropped then: not kept, config §1.1)."""
        waiting = self.passthrough_out.pop(port, bytearray())
        return bytes(waiting) if port in self.passthroughs() or port in self.console_passthroughs() else b""

    # ---- oep.fixture.i2c-target (fixture §3) ------------------------------------------------------
    def _i2c_op(self, fn: int, op: int, t: Take) -> tuple[int, int, bytes]:
        st, (max_length, features, depth, max_stretch) = self.i2c[fn], self.target_decl[fn]
        ops = _I2C.op
        planned = any(a[0] == fn for a in self.plan)
        if op == ops["configure"]:                                 # address(u8) [TLV]; every check before any change
            address = t.take("B")
            t.tail()
            if address > 0x7F:
                raise Reject(m.MALFORMED)                          # a 7-bit address (excluded for every revision)
            if address <= 0x07 or address >= 0x78:
                raise unsupported_fixed()                          # reserved by I2C: general call, 10-bit, ... (△5)
            if not planned:
                raise unavailable("wrong_state")                   # before the plan (cause 6)
            if fn in self.target_fail_configure:                   # taken, not brought up: state 0, the queue, slots
                self.target_fail_configure.discard(fn)             # and counters gone, stretch kept (fixture §3)
                self.i2c[fn] = I2cState(stretch_us=st.stretch_us)
                return self._answer(b"", m.FAILED)
            self.i2c[fn] = I2cState(state=1, address=address, stretch_us=st.stretch_us)   # made anew, stretch kept
            return self._answer(b"")
        if op == ops["read_rx"]:
            t.tail()
            if st.state == 0:
                raise unavailable("wrong_state")                   # cause 6
            if not st.queue:
                return self._answer(struct.pack("<BH", 0, 0))
            frame, ns = st.queue.pop(0)
            body = struct.pack("<BH", min(len(st.queue), 255), len(frame)) + frame
            tail = m.tlv(_I2C.tlv["read_rx_answer"]["ns"], struct.pack("<Q", ns))
            fits = m.RESULT_HEADER + len(body) + len(tail) <= self.probe.max_frame   # the optional TLV only if it fits
            return self._answer(body + (tail if fits else b""))
        if op == ops["preload_tx"]:                                # count(u16) data -> nothing (fixture §3)
            count = t.take("H")
            data = t.bytes(count)
            t.tail()
            if count == 0:
                raise Reject(m.MALFORMED)
            if count > max_length:
                raise unsupported_fixed()
            if st.state == 0:
                raise unavailable("wrong_state")                   # cause 6
            if len(st.tx) >= depth:
                raise unavailable("limit")                         # every slot holds an unread preload
            st.tx.append(data)
            return self._answer(b"")
        if op == ops["status"]:                                    # lock-free: state queued rx_frames tx_slots errors
            t.tail()
            return self._answer(struct.pack("<BBIBI", st.state, min(len(st.queue), 255), st.rx_frames,
                                            min(len(st.tx), 255), st.errors))
        if op == ops["stretch"]:                                   # offered by the ops tag (`offers`, the header)
            us = t.take("I")
            t.tail()
            if us > max_stretch:
                raise unsupported_fixed()                          # past describe's max_stretch_us
            st.stretch_us = us                                     # any state; configure keeps it
            return self._answer(b"")
        return m.REJECTED, m.UNKNOWN_OPERATION, b""

    def i2c_write(self, fn: int, data: bytes, address: int | None = None) -> bool:
        """TEST HOOK: a bus controller writes `data` to i2c-target `fn` in one transaction (START, address + W, data,
        STOP). -> whether the target ACKed its address (configured, and `address` None or its own). A write with data
        is one frame (fixture §3): more than max_length bytes are cut to max_length; a full queue drops the frame. errors
        grows by 1 at most per write (cut and dropped alike: 1). An address-only write (empty data) counts nothing."""
        st, (max_length, _, depth, _) = self.i2c[fn], self.target_decl[fn]
        if st.state == 0 or (address is not None and address != st.address):
            return False
        if not data:
            return True                                            # address only: nothing counts
        over = len(data) > max_length
        data = data[:max_length]
        if len(st.queue) >= depth:
            st.errors = min(st.errors + 1, 0xFFFFFFFF)            # the queue overflows: the new frame goes (once)
            return True
        st.errors = min(st.errors + over, 0xFFFFFFFF)               # saturates (fixture §3)
        st.queue.append((bytes(data), self.now_ns()))
        st.rx_frames = min(st.rx_frames + 1, 0xFFFFFFFF)
        return True

    def i2c_read(self, fn: int, n: int, address: int | None = None) -> bytes | None:
        """TEST HOOK: a bus controller reads n bytes from i2c-target `fn` in one transaction. -> the bytes, or None
        when the address is not ACKed. The oldest preloaded slot answers (cut or padded with 0xFF to n: the next read
        starts at the next slot); no slot: 0xFF (fixture §3)."""
        st = self.i2c[fn]
        if st.state == 0 or (address is not None and address != st.address):
            return None
        if st.tx:
            slot = st.tx.pop(0)
            return (slot + b"\xff" * n)[:n]
        return b"\xff" * n

    # ---- oep.fixture.spi-target (fixture §4) ------------------------------------------------------
    def _spi_op(self, fn: int, op: int, t: Take) -> tuple[int, int, bytes]:
        st, (max_length, features, _, _) = self.spi[fn], self.target_decl[fn]
        ops = _SPI.op
        if op == ops["configure"]:
            mode, order = t.take("BB")
            t.tail()
            if mode > 3 or order > 1:
                raise Reject(m.MALFORMED)
            if order == 1 and not features & SPI_FEATURES["lsb_first"]:
                raise unsupported_fixed()
            if not self.spi_modes.get(fn, 0x0F) >> mode & 1:
                raise unsupported_fixed()                          # a mode not in describe's modes (fixture §4)
            if not any(a[0] == fn for a in self.plan):
                raise unavailable("wrong_state")                   # before the plan (cause 6)
            if fn in self.target_fail_configure:                   # taken, not brought up: state 0, everything 0, the
                self.target_fail_configure.discard(fn)             # queue and the arm's data gone (fixture §4)
                self.spi[fn] = SpiState()
                return self._answer(b"", m.FAILED)
            self.spi[fn] = SpiState(state=1, mode=mode, bit_order=order)
            return self._answer(b"")
        if op == ops["arm"]:
            length, count = t.take("HH")
            tx = t.bytes(count)
            t.tail()
            if length == 0 or count > length:
                raise Reject(m.MALFORMED)
            if length > max_length:
                raise unsupported_fixed()
            if st.state == 0 or st.armed is not None:
                raise unavailable("wrong_state")                   # not configured, or one is armed already
            if fn in self.spi_fail_arm:                            # cannot get MISO ready: nothing armed, nothing
                self.spi_fail_arm.discard(fn)                      # changes (fixture §4: completed failed, no fixed part)
                return self._answer(b"", m.FAILED)
            st.armed = (length, tx)
            return self._answer(b"")
        if op == ops["read_rx"]:
            t.tail()
            if st.state == 0:
                raise unavailable("wrong_state")                   # cause 6
            if not st.queue:
                return self._answer(struct.pack("<BIH", 0, 0, 0))
            bits, data, ns = st.queue.pop(0)
            tail = m.tlv(_SPI.tlv["read_rx_answer"]["ns"], struct.pack("<Q", ns)) if self.spi_ns else b""   # optional
            body = struct.pack("<BIH", min(len(st.queue), 255), bits, len(data)) + data
            if m.RESULT_HEADER + len(body) + len(tail) > self.probe.max_frame:
                tail = b""                                         # the optional TLV only if it fits (fixture §4)
            return self._answer(body + tail)
        if op == ops["status"]:                                    # lock-free
            t.tail()
            return self._answer(struct.pack("<BBBBBII", st.state, st.mode, st.bit_order, int(st.armed is not None),
                                            min(len(st.queue), 255), st.transactions, st.errors))
        return m.REJECTED, m.UNKNOWN_OPERATION, b""

    def spi_transfer(self, fn: int, mosi: bytes, bits: int | None = None) -> bytes:
        """TEST HOOK: a bus controller runs one transaction on spi-target `fn` (CS low, `bits` clocks - 8 a MOSI byte
        by default - CS high). -> the MISO bytes: the armed tx, 0 past it and when not armed. Armed: the MOSI bytes
        (bits / 8 rounded up) up to the armed length (more is an error) and the bits are queued, the wait ends; a full
        queue drops them (an error). Not armed: MOSI dropped, an error. errors grows by 1 at most per transaction
        (fixture §4). `mosi`: the wire bits packed in the configured bit order (fixture.pack_wire_bits); the bits of a
        partial last byte past `bits` are cleared. Every transaction of a configured target counts; a target not
        configured sees nothing. 0 bits (CS edges without SCK) is no transaction: nothing counts,
        an arm keeps waiting."""
        st, (_, _, depth, _) = self.spi[fn], self.target_decl[fn]
        n = len(mosi)
        bits = 8 * n if bits is None else bits
        if bits % 8 and bits <= 8 * n:                             # the last byte's bits that did not come are 0 (§4)
            keep = (1 << (bits % 8)) - 1 if st.bit_order else (0xFF << (8 - bits % 8)) & 0xFF
            last = (bits - 1) // 8
            mosi = mosi[:last] + bytes([mosi[last] & keep]) + mosi[last + 1:]
        self.spi_select(fn, True)                                  # CS active: MISO driven for this transaction
        try:
            return self._spi_transaction(fn, st, depth, mosi, n, bits)
        finally:
            self.spi_select(fn, False)

    def _spi_transaction(self, fn: int, st: "SpiState", depth: int, mosi: bytes, n: int, bits: int) -> bytes:
        if st.state == 0 or bits == 0:
            return bytes(n)
        st.transactions = min(st.transactions + 1, 0xFFFFFFFF)
        if st.armed is None:
            st.errors = min(st.errors + 1, 0xFFFFFFFF)
            return bytes(n)
        length, tx = st.armed
        st.armed = None
        got = (bits + 7) // 8
        if got > length or len(st.queue) >= depth:
            st.errors = min(st.errors + 1, 0xFFFFFFFF)            # past the armed length, or the queue full: once
        if len(st.queue) < depth:                                  # bits saturates; ns: when CS went inactive (§4)
            st.queue.append((min(bits, 0xFFFFFFFF), bytes(mosi[:min(got, length)]), self.now_ns()))
        return (tx + bytes(n))[:n]

    # ---- the settings (core §13) ----------------------------------------------------------------------
    # The items each fn's describe declares (setting, setting_choice); the values as (fn, item, key) -> data (no origin,
    # §13.4: the initial values go in on a boot without a save); fn 0's ops get / set / unset / save / state / network.
    def _items(self, fn: int) -> list[SettingItem]:
        return self.setting_items.get(fn, [])

    def _item_number(self, fn: int, name: str) -> int:
        """An item's number in fn's describe (from 1; 0: none)."""
        return next((k for k, it in enumerate(self._items(fn), 1) if it.name == name), 0)

    def _value(self, fn: int, name: str, key: int) -> bytes | None:
        """The value now of an item."""
        return self.config.get((fn, name, key))

    def _values_of(self, name: str, fn: int | None = None, config: dict | None = None) -> dict[tuple[int, int], bytes]:
        """Every value of item `name` (of fn `fn`, else of any fn): (fn, key) -> data."""
        return {(f, k): v for (f, n, k), v in (self.config if config is None else config).items() if n == name and fn in (None, f)}

    @staticmethod
    def _fold(d: bytes) -> bytes:
        return d.translate(bytes.maketrans(b"ABCDEFGHIJKLMNOPQRSTUVWXYZ", b"abcdefghijklmnopqrstuvwxyz"))

    def _label_clash(self, config: dict) -> int | None:
        """The later channel of two whose labels are equal but for ASCII case (core §13.8), else None."""
        seen: dict[bytes, int] = {}
        for (_, ch), d in sorted(self._values_of("channel.{}.label", 0, config).items()):
            if self._fold(d) in seen:
                return ch
            seen[self._fold(d)] = ch
        return None

    def _initial_values(self) -> None:
        """A boot without a save (core §13.4): the initial values, checked as a set's; ones that do not pass (the
        firmware's mistake) stay out."""
        values = {}
        for (fn, name, key), d in self.initial.items():
            number = self._item_number(fn, name)
            if not number:
                continue
            it = self._items(fn)[number - 1]
            try:
                self._value_form(it, key, fn, d)
            except Reject:
                continue
            if not self._value_refused(fn, it, key, d):
                values[(fn, name, key)] = d
        if self._label_clash(values) is None:
            self.config = values

    def _key_ok(self, fn: int, it: SettingItem, key: int) -> bool:
        kind = it.key_kind
        if kind == KEY_KIND["none"]:
            return key == 0
        if kind == KEY_KIND["index"]:
            return key < it.keys
        if kind == KEY_KIND["channel"]:
            return self._channel_ok(key)
        if kind == KEY_KIND["transport"]:
            return self.transports.get(key) in virtual_bench.SERIAL_KINDS     # the pass-throughs: serial ports
        if kind == KEY_KIND["netif"]:
            return 1 <= key <= len(self.netif_names)
        return False

    def _value_form(self, it: SettingItem, key: int, fn: int, d: bytes) -> None:
        """A value's form (core §13.3) and what its definition leaves out: malformed."""
        f = it.form
        if not d and it.flags & SETTING_SECRET:
            if (fn, it.name, key) not in self.config:
                raise Reject(m.MALFORMED)                          # 0 bytes keep a value: there must be one (§13.5)
            return
        ok = {FORM["bytes"]: len(d) <= it.max_len, FORM["text"]: 1 <= len(d) <= it.max_len,   # text: its length only
              FORM["u32"]: len(d) == 4, FORM["ipv4"]: len(d) == 4, FORM["bool"]: len(d) == 1 and d[0] in (0, 1),
              FORM["enum"]: len(d) == 1, FORM["transport"]: len(d) == 1, FORM["channel"]: len(d) == 2,
              FORM["fn"]: len(d) == 2}.get(f, False)
        if ok and it.name == "wifi.{}.passphrase":                 # transports §7.1
            ok = (8 <= len(d) <= 63 and all(0x20 <= b <= 0x7E for b in d)) or (
                len(d) == 64 and all(chr(b) in "0123456789abcdefABCDEF" for b in d))
        elif ok and it.name == "ipv4.{}.address":                  # transports §7.2: an address a host may have
            a = int.from_bytes(d, "big")
            ok = a not in (0, 0xFFFFFFFF) and d[0] != 127 and not 224 <= d[0] <= 239
        elif ok and it.name == "ipv4.{}.prefix":
            ok = 1 <= struct.unpack("<I", d)[0] <= 32
        elif ok and it.name in ("passthrough.{}.baud", "passthrough.{}.max-speed"):
            ok = struct.unpack("<I", d)[0] != 0
        if not ok:
            raise Reject(m.MALFORMED)

    def _value_refused(self, fn: int, it: SettingItem, key: int, d: bytes) -> bool:
        """Whether this probe cannot take the value (unsupported, core §13.3): a key it has no value for, a choice it does
        not declare, a channel or fn it does not have, and what the item's owner refuses."""
        if not self._key_ok(fn, it, key):
            return True
        if not d:
            return False                                           # a secret kept
        if it.form == FORM["enum"] and d[0] not in it.choices:
            return True
        if it.form == FORM["channel"] and not self._channel_ok(struct.unpack("<H", d)[0]):
            return True
        if it.form == FORM["fn"] and self.names.get(struct.unpack("<H", d)[0]) not in WIRES:
            return True
        if it.name == "channel.{}.idle":
            mode = d[0]
            if mode in (IDLE_MODE["low"], IDLE_MODE["high"]) and key in self.input_only:
                return True
            if mode in (IDLE_MODE["pull_up"], IDLE_MODE["pull_down"]) and mode in self.no_pull.get(key, ()):
                return True
        if it.name == "passthrough.{}.rx":
            rx = struct.unpack("<H", d)[0]
            return rx not in {c for tag, v in self.decl[fn] if tag == catalog.ROLE_CHANNELS
                              and v[0] == _UART.enum["role"]["rx"] for c in _role_channels(v)}
        if it.name == "passthrough.{}.baud":
            try:
                self._uart_check(struct.unpack("<I", d)[0], 0, fn, None, divide=False)
            except Reject:
                return True
        if it.name == "passthrough.{}.mechanism":
            return d.decode(errors="replace") not in self.mech_names
        return False

    def _read_values(self, t: Take, count: int, with_data: bool) -> list[tuple[int, int, int, bytes]]:
        out = []
        for _ in range(count):
            fn, number, key = t.take("HBH")
            d = t.bytes(t.take("H")) if with_data else b""
            if number == 0:
                raise Reject(m.MALFORMED)
            out.append((fn, number, key, d))
        if len({(f, n, k) for f, n, k, _ in out}) != len(out):
            raise Reject(m.MALFORMED)                              # the same value twice in one request (§13.5)
        return out

    @staticmethod
    def _unsupported_item(index: int) -> Reject:
        return Reject(m.UNSUPPORTED, bytes([m.TAG_FIXED]) + m.tlv(ITEM_TLV, bytes([index])))

    def _resolve(self, values) -> list[tuple[int, SettingItem | None, int, bytes]]:
        out = []
        for fn, number, key, d in values:
            if fn != m.CORE_FN and fn not in self.names:
                raise Reject(m.UNKNOWN_FUNCTION)                   # a fn the probe does not have (core §4.3)
            items = self._items(fn)
            out.append((fn, items[number - 1] if number <= len(items) else None, key, d))
        return out

    def _settings_op(self, op: int, t: Take) -> tuple[int, int, bytes]:
        if op == _SET_OP["get"]:                                   # first(u16): more generation count values [TLV]
            first = t.take("H")
            t.tail()
            rows = []
            for fn in sorted(self.setting_items):
                for number, it in enumerate(self._items(fn), 1):
                    for k in sorted(k for (f, n, k) in self.config if f == fn and n == it.name):
                        d = b"" if it.flags & SETTING_SECRET else self.config[(fn, it.name, k)]   # a secret is never read
                        rows.append(struct.pack("<HBHH", fn, number, k, len(d)) + d)          # fn number key len data
            budget = self.probe.max_frame - m.RESULT_HEADER - 6
            out, sent = b"", 0
            for row in rows[first:]:
                if len(out) + len(row) > budget:
                    break                                          # a value is never split
                out += row
                sent += 1
            more = 1 if first + sent < len(rows) else 0
            return m.COMPLETED, m.SUCCESS, struct.pack("<BIB", more, self.generation, sent) + out + self.config_get_tail
        if op == _SET_OP["set"]:                                   # count(u8) values [TLV]
            values = self._resolve(self._read_values(t, t.take("B"), True))
            t.tail()                                               # the request's own tail (core §2.3), not values
            refused = None
            for i, (fn, it, key, d) in enumerate(values):
                if it is None:
                    refused = i if refused is None else refused    # after every value's form (core §4.3)
                    continue
                self._value_form(it, key, fn, d)
            if refused is None:
                refused = next((i for i, (fn, it, key, d) in enumerate(values) if self._value_refused(fn, it, key, d)), None)
            if refused is not None:
                raise self._unsupported_item(refused)
            new = dict(self.config)
            for fn, it, key, d in values:
                if d:                                              # 0 bytes keep a secret (§13.5)
                    new[(fn, it.name, key)] = d
            before = {n: self._in_use(n) for n in self.netif_numbers}
            self._change_config(new)
            self._reconnect(before)
            return m.COMPLETED, m.SUCCESS, struct.pack("<I", self.generation)
        if op == _SET_OP["unset"]:                                 # count(u8) count x (fn number key) [TLV]
            values = self._resolve(self._read_values(t, t.take("B"), False))
            t.tail()
            for i, (fn, it, key, _) in enumerate(values):
                if it is None:
                    raise self._unsupported_item(i)
            new = dict(self.config)
            for fn, it, key, _ in values:
                new.pop((fn, it.name, key), None)                  # nothing there: nothing done
            before = {n: self._in_use(n) for n in self.netif_numbers}
            self._change_config(new)
            self._reconnect(before)
            return m.COMPLETED, m.SUCCESS, struct.pack("<I", self.generation)
        if op == _SET_OP["network"]:                               # lock-free: tcp_port count x 19 bytes (transports §7.3)
            t.tail()
            out = struct.pack("<HB", self.tcp_port, len(self.netif_names))
            for n in self.netif_numbers:
                st, entry, reason, rssi, (addr, pfx, gw, dns) = self.network(n)
                out += bytes([n, st]) + struct.pack("<HBb", entry, reason, rssi) + addr + bytes([pfx]) + gw + dns
            return m.COMPLETED, m.SUCCESS, out
        if op == _SET_OP["state"]:                                 # lock-free (§13.7)
            t.tail()
            return m.COMPLETED, m.SUCCESS, struct.pack("<BIB", self.storage_state, self.saved_generation,
                                                        self.storage_reason)
        if op == _SET_OP["save"]:
            t.tail()
            if sum(8 + len(v) for v in self.config.values()) > self.storage_max:
                raise unavailable("storage_full")                  # more than the save holds, before anything (§13.7)
            if self._storage_failed():
                return m.COMPLETED, m.FAILED, struct.pack("<I", self.generation)   # the same fixed part (§13.7)
            self.saved, self.saved_unreadable = self._by_name(self.config), False
            self.storage_state, self.storage_reason, self.saved_generation = 1, 0, self.generation
            return m.COMPLETED, m.SUCCESS, struct.pack("<I", self.generation)
        return m.REJECTED, m.UNKNOWN_OPERATION, b""

    def _identity(self, fn: int):
        return "core" if fn == m.CORE_FN else self.identity[fn][:2]          # (name, instance): a category (§13.1)

    def _fn_of(self, identity) -> int | None:
        if identity == "core":
            return m.CORE_FN
        return next((fn for fn, ident in self.identity.items() if ident[:2] == identity), None)

    def _by_name(self, config: dict) -> dict:
        """The save (§13.7): by category and item name, an fn a value names by its interface's name and instance."""
        out = {}
        for (fn, name, key), d in config.items():
            it = self._items(fn)[self._item_number(fn, name) - 1]
            out[(self._identity(fn), name, key)] = (self._identity(struct.unpack("<H", d)[0])
                                                    if it.form == FORM["fn"] else d)
        return out

    # ---- the network (transports §7) ------------------------------------------------------------------
    def network(self, netif: int) -> tuple[int, int, int, int, tuple[bytes, int, bytes, bytes]]:
        """Netif `netif` (its number, from 1) now: state, entry, reason, rssi, (address, prefix, gateway, dns). wifi
        tries the entries with an ssid in key order against the access points around (`aps`); none: off. ethernet: on
        while the cable is in (`ether_link`). Without ipv4.{}.mode static: what DHCP gives (transports §7)."""
        zero = (bytes(4), 0, bytes(4), bytes(4))
        name = self.netif_names[netif - 1]
        entry, rssi = 0xFFFF, 0
        if name == "wifi":
            entries = sorted((k, v) for (f, k), v in self._values_of("wifi.{}.ssid", 0).items())
            if not entries:
                return NET_STATE["off"], 0xFFFF, 0, 0, zero
            reason = NET_REASON["not_found"]
            for k, ssid in entries:
                pw = self._value(0, "wifi.{}.passphrase", k) or b""
                if ssid not in self.aps:
                    continue
                if self.aps[ssid] != pw:
                    reason = NET_REASON["auth"]
                    continue
                entry, rssi = k, -52
                break
            else:
                return NET_STATE["waiting"], 0xFFFF, reason, 0, zero
        elif not self.ether_link:
            return NET_STATE["off"], 0xFFFF, 0, 0, zero
        mode = self._value(0, "ipv4.{}.mode", netif)
        if mode == bytes([IPV4_MODE["static"]]):
            addr, pfx = self._value(0, "ipv4.{}.address", netif), self._value(0, "ipv4.{}.prefix", netif)
            if addr is None or pfx is None:
                return NET_STATE["waiting"], 0xFFFF, NET_REASON["no_address"], 0, zero   # static without its values
            got = (addr, struct.unpack("<I", pfx)[0], self._value(0, "ipv4.{}.gateway", netif) or bytes(4),
                   self._value(0, "ipv4.{}.dns", netif) or bytes(4))
        else:
            got = self.dhcp[name]
        return NET_STATE["up"], entry, 0, rssi, got

    def _in_use(self, netif: int):
        """The values a netif connects with now (transports §7: the entry in use, its ipv4)."""
        st, entry, *_ = self.network(netif)
        wifi = ((self._value(0, "wifi.{}.ssid", entry), self._value(0, "wifi.{}.passphrase", entry))
                if entry != 0xFFFF else None)
        ip = tuple(self._value(0, n, netif) for n in IPV4_ITEMS)
        return st, wifi, ip

    def _reconnect(self, before: dict) -> None:
        """After the answer (transports §7): a netif whose in-use values changed drops and connects again with the new."""
        for n, was in before.items():
            if was[0] == NET_STATE["up"] and self._in_use(n) != was:
                self.reconnects[n] += 1

    def _storage_failed(self) -> bool:
        """TEST HOOK `storage_fault`: the next save does not complete (core §13.7) and the storage is then seen as "old"
        (the save before kept: state unchanged), "none" (no save) or "unreadable" (state 2, reason 1). The settings,
        generation and pins stay either way."""
        fault, self.storage_fault = self.storage_fault, None
        if fault is None:
            return False
        if fault == "none":
            self.saved, self.saved_unreadable = None, False
            self.storage_state, self.storage_reason, self.saved_generation = 0, 0, 0
        elif fault == "unreadable":
            self.saved, self.saved_unreadable = None, True
            self.storage_state, self.storage_reason, self.saved_generation = 2, 1, 0
        return True

    def _change_config(self, new: dict) -> None:
        """Make `new` the user values after checking them whole against what runs (core §13.5, §13.8, fixture §2.1,
        console §2.1); a refusal changes nothing. The same contents: nothing changes, the generation stays."""
        if new == self.config:
            return
        if self.generation == 0xFFFFFFFF:
            raise unavailable("limit")                             # no generation left before a restart (§13.5)
        clash = self._label_clash(new)
        if clash is not None:
            raise unavailable("held_by_settings", clash)           # two labels equal but for case (§13.8)
        disabled = {k for (f, k), v in self._values_of("channel.{}.disable", 0, new).items() if v == b"\x01"}
        idles = self._values_of("channel.{}.idle", 0, new)
        outputs = (IDLE_MODE["low"], IDLE_MODE["high"])
        held = lambda ch: ch in disabled or (idles.get((0, ch)) is not None and idles[(0, ch)][0] in outputs)
        users: dict[int, int] = {}
        for (fn, port), rx in self._values_of("passthrough.{}.rx", config=new).items():
            if self.names.get(fn) != _UART.name:
                continue
            users[port] = users.get(port, 0) + 1
            if held(struct.unpack("<H", rx)[0]):
                raise unavailable("held_by_settings", struct.unpack("<H", rx)[0])   # fixture §2.1
        for (fn, port), wire in self._values_of("passthrough.{}.wire", config=new).items():
            users[port] = users.get(port, 0) + 1
            get = lambda n: self._values_of(n, fn, new).get((fn, port))
            io, ck, hz = get("passthrough.{}.swdio"), get("passthrough.{}.swclk"), get("passthrough.{}.max-speed")
            pair = (struct.unpack("<H", io)[0] if io else 0xFFFF, struct.unpack("<H", ck)[0] if ck else 0xFFFF)
            for pin in pair:
                if pin != 0xFFFF and held(pin):
                    raise unavailable("held_by_settings", pin)     # console §2.1
            wfn = struct.unpack("<H", wire)[0]
            if io and (ck or self.names.get(wfn) != "oep.wire.rvswd") and not self._allows(wfn, pair):
                raise unsupported_fixed()                          # a pair the wire does not allow
            if hz and struct.unpack("<I", hz)[0] < self._min_clock(wfn):
                raise unsupported_fixed()
        if any(n > 1 for n in users.values()):
            raise unavailable("held_by_settings")                  # one pass-through a port (fixture §2.1)
        busy = {a[2] for a in self.plan} | {p for c in self.conns.values() for p in c.pair}
        for ch in sorted(disabled - self.disabled):
            if ch in busy:
                raise unavailable("pin_in_use", ch)                # a channel in use cannot be disabled (§13.8)
        both = disabled & {k for (_, k) in idles}
        if both:
            raise unavailable("held_by_settings", min(both))       # idle and disable on one channel (§13.8)
        old_idle = {k: v for (_, k), v in self._values_of("channel.{}.idle", 0).items()}
        new_idle = {k: v for (_, k), v in idles.items()}
        for ch in sorted({a[2] for a in self.plan if a[0] in self.captures and self.captures[a[0]].analog}):
            v = new_idle.get(ch)
            if v != old_idle.get(ch) and v is not None and v[0] in outputs:
                raise unavailable("pin_in_use", ch)                # an output idle onto an analog plan's pad: refused
                                                                   # whichever comes second (capture §1.2)
        repark = (self.disabled - disabled) | {ch for ch in set(old_idle) | set(new_idle)
                                               if old_idle.get(ch) != new_idle.get(ch)}
        repark -= {a[2] for a in self.plan}                        # a channel a plan holds is not free, a logic's that only
        for (_, port) in set(self._values_of("passthrough.{}.wire")) - set(self._values_of("passthrough.{}.wire", config=new)):
            self.resident.pop(port, None)                          # gone: its condition starts again (console §2.1)
        self.config = new                                          # listens too (core §8): its new idle waits for its release
        self.generation += 1
        self._park(repark)                                         # a free channel's new idle, an enabled one (§13.8)

    def _apply_saved(self) -> None:
        """At boot (core §13.7): every saved value, by name, checked by itself against this firmware - disable first,
        then idle (a channel with both keeps the disable), then the rest; of two labels equal but for case the lower
        channel's; one it cannot take (a category or item it does
        not have, a form or value it refuses) is left out and the rest applied (storage_state 3 partial, reason 2)."""
        def rank(kv):
            (ident, name, key), _ = kv
            return ({"channel.{}.disable": 0, "channel.{}.idle": 1}.get(name, 2), str(ident), name, key)
        applied, bad = {}, False
        for (ident, name, key), d in sorted(self.saved.items(), key=rank):
            fn = self._fn_of(ident)
            number = self._item_number(fn, name) if fn is not None else 0
            ok = number > 0
            if ok and isinstance(d, tuple):                        # an fn by its interface's name and instance
                wfn = self._fn_of(d)
                ok, d = wfn is not None, struct.pack("<H", wfn or 0)
            if ok:
                it = self._items(fn)[number - 1]
                try:
                    self._value_form(it, key, fn, d)
                    ok = not self._value_refused(fn, it, key, d)
                except Reject:
                    ok = False
            if ok and name == "channel.{}.idle" and applied.get((0, "channel.{}.disable", key)) == b"\x01":
                ok = False                                         # disable wins over idle on one channel
            if ok and name == "channel.{}.label" and self._label_clash({**applied, (fn, name, key): d}) is not None:
                ok = False                                         # labels in key order: the lower channel's stays
            if ok:
                applied[(fn, name, key)] = d
            bad |= not ok
        self.config = applied
        self.storage_state, self.storage_reason = (3, 2) if bad else (1, 0)
        self.saved_generation = 0 if bad else 1

    def load_settings(self, values, saved: bool = True) -> None:
        """TEST HOOK: `values` as (fn, item, key, data) - with `saved`, as a probe that booted with them stored (applied as
        at boot); without, as the settings now (generation one on, the free pins reparked)."""
        new = {(fn, name, key): bytes(d) for fn, name, key, d in values}
        for fn, name, _ in new:
            if not self._item_number(fn, name):
                raise ValueError(f"the probe declares no item {name!r} on fn {fn} (a profile without the settings?)")
        if saved:
            self.saved, self.saved_unreadable = self._by_name(new), False
            self._apply_saved()
        else:
            self.config = new
            self.generation += 1
        self._park(self._boot_channels())

    def mechanism_number(self, name: str) -> int:
        """TEST HELPER: a console mechanism's number (its place in describe's mechanism list, from 1; core §7.7)."""
        return self.mech_names.index(name) + 1

    def setting_number(self, name: str, fn: int = 0) -> int:
        """TEST HELPER: an item's number on the wire (its place in fn's describe setting list, from 1)."""
        return self._item_number(fn, name)

    def tick(self) -> None:
        """Time passes: the lease, the console polls, the captures, port_speed's timers."""
        if self.current_core is not None:
            self.current_core.tick()
        self._lapse()
        self._speed_tick()
        self._console_poll()
        for fn in self.groups:
            self._group_tick(fn)
        for fn, cap in self.captures.items():
            if cap.group is None:
                self._events(fn, cap.tick(self.uptime_ms()))

    def _session_over(self) -> None:
        """The session ended (end, lapse, force): a port off its boot speed goes back after the answer (oep-if-link §3)."""
        if self.speed_state != "base":
            if self.speed_frame_busy:
                self.speed_fallback = "session"                    # never cancelled (oep-if-link §3)
            else:
                self.speed_pending = ("revert",)


SIMS = {"oep.probe.link": "link", "oep.probe.plan": "plan_op", "oep.probe.restart": "restart", "oep.wire.rvswd": "wire", "oep.wire.swio": "wire", "oep.target.riscv-dm": "dm",
        "oep.target.console": "console", "oep.fixture.gpio": "gpio", "oep.fixture.uart": "uart",
        "oep.fixture.logic": "capture", "oep.fixture.analog": "capture",
        "oep.fixture.capture-group": "group", "oep.fixture.i2c-target": "i2c_op", "oep.fixture.spi-target": "spi_op"}

# A riscv-dm op whose exchanges got no answer from the wire: completed failed, status line, nothing done (debug §4)
DM_LINE_FAILED = {_RV.op["dmi"]: struct.pack("<HBH", 0, LINE, 0), _RV.op["halt"]: bytes([LINE]),
                  _RV.op["resume"]: bytes([LINE]), _RV.op["reset"]: struct.pack("<BBI", LINE, 0, 0),
                  _RV.op["step"]: struct.pack("<BBII", LINE, 0, 0, 0), _RV.op["read_block"]: struct.pack("<HB", 0, LINE),
                  _RV.op["write_block"]: struct.pack("<HB", 0, LINE),
                  _RV.op["run"]: struct.pack("<BBIIB", LINE, RUN_STOPPED["not_run"], 0, 0, 0)}
