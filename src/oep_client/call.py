"""Call any interface's op by its number, or by the name and fields its describe gives (oep-core §7.4 op_name /
op_field) - an interface this host has no module for, such as a sketch's oep::SimpleInterface.

    from oep_client import link, core
    from oep_client.call import call
    hst = link.open_host("/dev/ttyACM0")
    core.take(hst, 10000, owner="my-test")
    r = call(hst, "io.github.yourname.mine", "add", a=2, b=3)
    r["sum"], r.sum, r.revision                     # the success fields by name; the interface's revision
    call(hst, "io.github.yourname.mine", 0x10, b"\\x02\\0\\0\\0\\x03\\0\\0\\0").payload   # by number, the bytes as they are

Errors: InterfaceMissing / RevisionMismatch / OpMissing (all NotOffered: a test can turn them into a skip),
CommandRejected (the probe refused it: the reason), CommandFailed (completed failed: code and message when the op
gives them).

A job (oep::SimpleInterface::job: it starts work on the probe's worker and answers at once) and its status op:

    st = call_wait(hst, "io.github.yourname.mine", "collect", count=3, timeout=10)
    st.start_ns, st.end_ns                          # on the probe's clock (a capture's markers' one)
    # wait=: a stream's wait (acquire.Request(during=...)) keeps the capture collecting while this waits

JobFailed (the job's Worker::fail: code, message), JobCancelled, JobTimeout (cancel sent first), ProbeRestarted (its
boot_id changed while the job ran)."""

from __future__ import annotations

import struct
import time
from typing import Any, Callable

from . import catalog, core, host as h

T = catalog.OP_FIELD_TYPES
LIST = catalog.OP_FIELD_LIST
_INT = {T["u8"]: "<B", T["u16"]: "<H", T["u32"]: "<I", T["u64"]: "<Q",
        T["i8"]: "<b", T["i16"]: "<h", T["i32"]: "<i", T["i64"]: "<q", T["bool"]: "<B"}


class NotOffered(LookupError):
    """The probe does not offer what was asked: no such interface, another revision, no such op."""


class InterfaceMissing(NotOffered):
    pass


class RevisionMismatch(NotOffered):
    def __init__(self, interface: str, revision: int, wanted: int):
        super().__init__(f"{interface} is revision {revision} on this probe, {wanted} asked")
        self.revision, self.wanted = revision, wanted


class OpMissing(NotOffered):
    pass


class CommandRejected(h.Rejected):
    """The probe refused the op (resolution rejected): `result.detail` is the reason (core §4.3)."""

    def __init__(self, op: str, result):
        super().__init__(result)
        self.op = op
        self.args = (f"{op}: {result.describe()}",)


class CommandFailed(h.Failed):
    """The op ran and did not work (completed failed / partial): `fields` the failed part decoded (code, message for
    an oep::SimpleInterface op), `payload` the bytes."""

    def __init__(self, op: str, result, fields: dict | None):
        why = f"{op}: {result.describe()}"
        if fields and "code" in fields:
            why += f" (code {fields['code']}" + (f": {fields['message']}" if fields.get("message") else "") + ")"
        super().__init__(result, why)
        self.op, self.fields, self.payload = op, fields or {}, result.payload
        self.code, self.message = self.fields.get("code"), self.fields.get("message")


class JobFailed(h.OepError):
    """The job ended failed: `code` and `message` from its Worker::fail(), `status` the status op's fields."""

    def __init__(self, op: str, status: dict):
        super().__init__(f"{op}: failed (code {status.get('code')}"
                         + (f": {status['message']}" if status.get("message") else "") + ")")
        self.op, self.status, self.code, self.message = op, status, status.get("code"), status.get("message")


class JobCancelled(h.OepError):
    """The job stopped on a cancel (`status` the status op's fields)."""

    def __init__(self, op: str, status: dict):
        super().__init__(f"{op}: cancelled")
        self.op, self.status = op, status


class JobTimeout(h.OepError, TimeoutError):
    """The job still ran at the timeout; a cancel was sent (`status` the last status op's fields)."""

    def __init__(self, op: str, timeout: float, status: dict):
        super().__init__(f"{op}: still running after {timeout} s (cancel sent)")
        self.op, self.status = op, status


class ProbeRestarted(h.OepError):
    """The probe's boot_id changed while the job ran (core §6.5): it restarted, the job with it."""


# The status op's state (oep::Worker::State): the firmware's convention, not the registry's
JOB_STATES = {"idle": 0, "running": 1, "done": 2, "failed": 3, "cancelled": 4}


class CallResult(dict):
    """The success fields by name (a dict, and attributes), the raw payload, the interface's revision and the op."""

    def __init__(self, fields: dict, payload: bytes, revision: int, op: catalog.OpForm | int):
        super().__init__(fields)
        self.payload, self.revision, self.op = payload, revision, op

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name) from None


def encode(fields: list[catalog.OpField], values: dict) -> bytes:
    """A request's fixed part from values by field name (ints, bools, bytes, str, lists of ints / bools)."""
    missing = [f.name for f in fields if f.name not in values]
    extra = sorted(set(values) - {f.name for f in fields})
    if missing or extra:
        raise TypeError(f"fields {', '.join(f'{f.name}: {f.type_name}' for f in fields) or 'none'}"
                        + (f"; missing {missing}" if missing else "") + (f"; not fields {extra}" if extra else ""))
    out = b""
    for f in fields:
        v = values[f.name]
        if f.type & LIST:
            items = list(v)
            out += struct.pack("<H", len(items)) + b"".join(struct.pack(_INT[f.type & ~LIST], int(x)) for x in items)
        elif f.type == T["bytes"]:
            b = bytes(v)
            out += struct.pack("<H", len(b)) + b
        elif f.type == T["text"]:
            b = str(v).encode()
            out += struct.pack("<H", len(b)) + b
        elif f.type in _INT:
            out += struct.pack(_INT[f.type], int(v))
        else:
            raise TypeError(f"{f.name}: type {f.type} unknown to this host")
    return out


def decode(fields: list[catalog.OpField], payload: bytes) -> tuple[dict, bytes]:
    """A fixed part's fields by name, and what follows them (the TLVs)."""
    out, at = {}, 0
    for f in fields:
        if f.type & LIST:
            (n,) = struct.unpack_from("<H", payload, at)
            fmt = _INT[f.type & ~LIST]
            w = struct.calcsize(fmt)
            vals = [struct.unpack_from(fmt, payload, at + 2 + i * w)[0] for i in range(n)]
            out[f.name] = [bool(x) for x in vals] if (f.type & ~LIST) == T["bool"] else vals
            at += 2 + n * w
        elif f.type in (T["bytes"], T["text"]):
            (n,) = struct.unpack_from("<H", payload, at)
            if at + 2 + n > len(payload):
                raise h.ShortPayload(f"{f.name}: {n} bytes, the answer ends before")
            raw = payload[at + 2:at + 2 + n]
            out[f.name] = raw.decode("utf-8", "replace") if f.type == T["text"] else raw
            at += 2 + n
        else:
            fmt = _INT[f.type]
            out[f.name] = struct.unpack_from(fmt, payload, at)[0]
            if f.type == T["bool"]:
                out[f.name] = bool(out[f.name])
            at += struct.calcsize(fmt)
    return out, payload[at:]


def ops(hst: h.Host, interface: str | int) -> dict[int, catalog.OpForm]:
    """The op_name / op_field an interface's describe gives (by op number; empty when it gives none)."""
    fn = interface if isinstance(interface, int) else _fn(hst, interface)
    return catalog.decode_description(b"".join(catalog.tlv(t, v) for t, v in core.describe(hst, fn))).op_forms


def _fn(hst: h.Host, interface: str) -> int:
    try:
        return core.find(hst, interface)
    except LookupError as e:
        raise InterfaceMissing(str(e)) from None


def call(hst: h.Host, interface: str, op: int | str, payload: bytes | None = None, *, locked: bool | None = None,
         revision: int | None = None, expect_ms: int = 0, **fields) -> CallResult:
    """Run `op` (a number, or a name from the describe's op_name) of `interface` on the session `hst` holds.
    payload: the request as bytes (then no fields); otherwise the fields by name, packed as op_field says.
    locked: send it in the session (default: unless op_name says lock_free). revision: the one expected."""
    fn = _fn(hst, interface)
    rev = core.revision(hst, interface, fn)
    if revision is not None and rev != revision:
        raise RevisionMismatch(interface, rev, revision)
    forms = ops(hst, fn)
    if isinstance(op, str):
        form = next((f for f in forms.values() if f.name == op), None)
        if form is None:
            raise OpMissing(f"{interface} has no op named {op!r}" + (f" (it names {', '.join(f.name for f in forms.values())})"
                                                                     if forms else " (its describe names none)"))
        code = form.op
    else:
        code, form = op, forms.get(op)
    label = form.name if form else f"0x{code:02x}"
    if not core.offers(hst, fn, code):
        raise OpMissing(f"{interface} does not offer op {label}")
    if payload is None:
        if fields:
            if form is None or not form.form:
                raise TypeError(f"{interface} {label}: its describe gives no fields; pass payload= as bytes")
            payload = encode(form.request, fields)
        elif form is not None and form.form and form.request:
            payload = encode(form.request, fields)        # raises: the fields it needs
        else:
            payload = b""
    elif fields:
        raise TypeError("payload= or fields, not both")
    if locked is None:
        locked = not (form is not None and form.lock_free)
    try:
        r = hst.request(fn, code, payload, locked=locked, expect_ms=expect_ms)
    except h.Rejected as e:
        raise CommandRejected(label, e.result) from None
    if r.succeeded:
        got = decode(form.success, r.payload)[0] if form is not None and form.form else {}
        return CallResult(got, r.payload, rev, form or code)
    failed = None
    if r.ran and form is not None and form.form and form.failed:
        try:
            failed = decode(form.failed, r.payload)[0]
        except (struct.error, h.ShortPayload):
            failed = None                                  # a failed answer without its fields (a firmware bug)
    raise CommandFailed(label, r, failed)


def call_wait(hst: h.Host, interface: str, op: int | str, payload: bytes | None = None, *, timeout: float,
              wait: Callable[[float], None] | None = None, interval: float = 0.05, status: str = "status",
              cancel: str = "cancel", revision: int | None = None, **fields) -> CallResult:
    """Start the job `op` (as call()) and wait for it by its `status` op: the status's fields when it ended done.
    wait(seconds) sleeps between the status reads (time.sleep by default; a stream's wait keeps a capture going)."""
    boot = hst.clock().boot_id
    started = call(hst, interface, op, payload, revision=revision, **fields)
    code = started.op.op if isinstance(started.op, catalog.OpForm) else started.op
    label = started.op.name if isinstance(started.op, catalog.OpForm) else f"0x{code:02x}"
    pause = wait or time.sleep
    deadline = time.monotonic() + timeout
    while True:
        try:
            st = call(hst, interface, status)
        except (h.OepError, OSError):
            _restarted(hst, boot, label)
            raise
        if st["op"] != code or st["state"] == JOB_STATES["idle"]:
            _restarted(hst, boot, label)
            raise ProbeRestarted(f"{label}: the status names op {st['op']} in state {st['state']}, not this job")
        if st["state"] != JOB_STATES["running"]:
            break
        if time.monotonic() >= deadline:
            try:
                call(hst, interface, cancel)
            except (h.OepError, OSError):
                pass                                       # the timeout is the error to report
            raise JobTimeout(label, timeout, dict(st))
        pause(interval)
    if st["state"] == JOB_STATES["failed"]:
        raise JobFailed(label, dict(st))
    if st["state"] == JOB_STATES["cancelled"]:
        raise JobCancelled(label, dict(st))
    return st


def _restarted(hst: h.Host, boot: int, label: str) -> None:
    try:
        now = hst.clock().boot_id
    except (h.OepError, OSError):
        return
    if now != boot:
        raise ProbeRestarted(f"{label}: the probe restarted while the job ran (boot_id {boot:#x} -> {now:#x})")
