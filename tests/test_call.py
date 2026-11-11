"""oep_client.call and the describe's op_name / op_field (oep-core §7.4): decoding the tags, packing a request and
reading an answer by the fields, call() by name and by number (lock_free, a refusal, a failed answer with code and
message, the NotOffered errors). The host's requests are a stand-in here; tests/hw drives a probe."""

import struct

import pytest

from oep_client import call as c, catalog, core, host as h, message as m, registry as reg

T = reg.CORE.enum["op_field_type"]
P = reg.CORE.enum["op_field_part"]
F = reg.CORE.enum["op_name_flag"]
NAME, FIELD = reg.DESCRIBE_COMMON["op_name"], reg.DESCRIBE_COMMON["op_field"]
OFFERED = (0x10, 0x20, 0x30)   # the made-up interface's ops: add, read, raw (its own numbers, not the registry's)


def op_name(op, flags, name):
    return catalog.tlv(NAME, bytes([op, flags]) + name.encode())


def op_field(op, part, type_, name):
    return catalog.tlv(FIELD, bytes([op, part, type_]) + name.encode())


DESCRIBE = (catalog.ops_tlv([0x10, 0x20, 0x30])
            + op_name(0x10, F["form"], "add")
            + op_field(0x10, P["request"], T["i32"], "a") + op_field(0x10, P["request"], T["i32"], "b")
            + op_field(0x10, P["success"], T["i32"], "sum")
            + op_field(0x10, P["failed"], T["i32"], "code") + op_field(0x10, P["failed"], T["text"], "message")
            + op_name(0x20, F["form"] | F["lock_free"], "read")
            + op_field(0x20, P["success"], T["i32"] | T["list"], "temps")
            + op_name(0x30, 0, "raw")                       # a name, no form
            + op_field(0x30, P["request"], T["u8"], "ignored"))   # fields of an op without form: not taken


def test_the_describe_gives_names_and_fields():
    d = catalog.decode_description(DESCRIBE)
    assert sorted(d.op_forms) == [0x10, 0x20, 0x30]
    add, read, raw = d.op_forms[0x10], d.op_forms[0x20], d.op_forms[0x30]
    assert add.form and not add.lock_free and [f.name for f in add.request] == ["a", "b"]
    assert add.signature() == "add(a: i32, b: i32) -> sum: i32"
    assert [f.name for f in add.failed] == ["code", "message"]
    assert read.lock_free and read.signature() == "read() -> temps: i32[]"
    assert not raw.form and raw.request == [] and raw.signature() == "raw"


def test_fields_round_trip():
    fields = [catalog.OpField(n, T[t]) for n, t in
              [("a", "u8"), ("b", "u16"), ("c", "u32"), ("d", "u64"), ("e", "i8"), ("f", "i16"), ("g", "i32"),
               ("h", "i64"), ("i", "bool"), ("j", "bytes"), ("k", "text")]] + [catalog.OpField("l", T["u32"] | T["list"])]
    values = {"a": 0xAB, "b": 0x1234, "c": 0x89ABCDEF, "d": 2**63, "e": -1, "f": -2, "g": -3, "h": -(2**63), "i": True,
              "j": b"\x01\x02", "k": "hi", "l": [10, 20]}
    packed = c.encode(fields, values)
    assert packed[:3] == b"\xab\x34\x12"
    got, rest = c.decode(fields, packed + b"\x40\x00\x00")
    assert got == values and rest == b"\x40\x00\x00"
    with pytest.raises(TypeError):
        c.encode(fields, {"a": 1})                          # the other fields missing
    with pytest.raises(TypeError):
        c.encode(fields, dict(values, z=1))                 # not a field


class FakeHost:
    """Answers request() from a table; records what it was sent."""

    def __init__(self, answers):
        self.answers, self.sent = answers, []

    def request(self, fn, op, payload=b"", *, locked=True, expect_ms=0):
        self.sent.append((fn, op, payload, locked))
        r = self.answers[op](payload)
        if r.resolution == m.REJECTED:
            raise h.Rejected(r)
        return r


@pytest.fixture
def probe(monkeypatch):
    monkeypatch.setattr(core, "find", lambda hst, name, revision=None: 5 if name == "io.github.you.mine" else
                        (_ for _ in ()).throw(LookupError(f"probe does not offer {name}")))
    monkeypatch.setattr(core, "revision", lambda hst, name, fn: 1)
    monkeypatch.setattr(core, "describe", lambda hst, fn: catalog.split_tlv(DESCRIBE))
    monkeypatch.setattr(core, "offers", lambda hst, fn, op: op in OFFERED)

    def add(p):
        a, b = struct.unpack("<ii", p[:8])
        if a < 0:
            return m.Result(1, m.COMPLETED, m.FAILED, struct.pack("<iH", 7, 5) + b"no 44")
        return m.Result(1, m.COMPLETED, m.SUCCESS, struct.pack("<i", a + b))

    return FakeHost({0x10: add,
                     0x20: lambda p: m.Result(1, m.COMPLETED, m.SUCCESS, struct.pack("<Hii", 2, 21, 22)),
                     0x30: lambda p: m.Result(1, m.REJECTED, reg.REJECT_REASONS["unsupported"], b"\x00")})


def test_call_by_name_and_number(probe):
    r = c.call(probe, "io.github.you.mine", "add", a=2, b=3)
    assert r["sum"] == 5 and r.sum == 5 and r.revision == 1
    assert probe.sent[-1] == (5, 0x10, struct.pack("<ii", 2, 3), True)
    r = c.call(probe, "io.github.you.mine", 0x10, struct.pack("<ii", 4, 5))
    assert r["sum"] == 9 and r.payload == struct.pack("<i", 9)
    r = c.call(probe, "io.github.you.mine", "read")
    assert r["temps"] == [21, 22] and probe.sent[-1][3] is False   # lock_free: sent outside the session


def test_failed_and_rejected(probe):
    with pytest.raises(c.CommandFailed) as e:
        c.call(probe, "io.github.you.mine", "add", a=-1, b=0)
    assert e.value.code == 7 and e.value.message == "no 44" and e.value.op == "add"
    with pytest.raises(c.CommandRejected) as r:
        c.call(probe, "io.github.you.mine", "raw", b"\x01")
    assert r.value.result.detail == reg.REJECT_REASONS["unsupported"]
    with pytest.raises(TypeError):
        c.call(probe, "io.github.you.mine", "raw", x=1)     # no form: bytes only


def test_not_offered(probe):
    with pytest.raises(c.InterfaceMissing):
        c.call(probe, "io.github.you.other", "add", a=1, b=2)
    with pytest.raises(c.RevisionMismatch):
        c.call(probe, "io.github.you.mine", "add", a=1, b=2, revision=2)
    with pytest.raises(c.OpMissing):
        c.call(probe, "io.github.you.mine", "nope")
    with pytest.raises(c.OpMissing):
        c.call(probe, "io.github.you.mine", 0x40)
    assert issubclass(c.InterfaceMissing, c.NotOffered) and issubclass(c.OpMissing, c.NotOffered)


def test_call_through_the_virtual_endpoint():
    """The virtual bench's stand-in fn: write(value: u32) in the session, read() -> value lock-free, by name."""
    import random
    from oep_client import endpoint, virtual_bench as vb

    class Clock:
        t = 0

        def __call__(self):
            return self.t

    ep = endpoint.Endpoint(vb.with_stand_in(vb.esp32_v003()), Clock())
    hst = h.Host(ep.handle, rng=random.Random(1))
    core.take(hst, 3000, owner="test_call")
    forms = c.ops(hst, vb.STAND_IN)
    assert sorted(f.signature() for f in forms.values()) == ["read() -> value: u32", "write(value: u32)"]
    c.call(hst, vb.STAND_IN, "write", value=1234)
    assert c.call(hst, vb.STAND_IN, "read")["value"] == 1234


# ---- call_wait: a job and its status op (oep::SimpleInterface::job / addWorker) ----------------------------------------

S = c.JOB_STATES
JOB, STATUS, CANCEL = 0x10, 0x40, 0x41
JOB_DESCRIBE = (catalog.ops_tlv([JOB, STATUS, CANCEL])
                + op_name(JOB, F["form"], "collect") + op_field(JOB, P["request"], T["u8"], "count")
                + op_name(STATUS, F["form"] | F["lock_free"], "status")
                + b"".join(op_field(STATUS, P["success"], T[t], n) for n, t in
                           [("state", "u8"), ("op", "u8"), ("code", "i32"), ("message", "text"), ("start_ns", "u64"),
                            ("end_ns", "u64")])
                + op_name(CANCEL, F["form"], "cancel"))


class JobProbe(FakeHost):
    """A worker: the job runs for `polls` status reads, then ends as `ends` says; clock() gives a boot_id."""

    def __init__(self, polls=2, ends="done", restart_after=None):
        self.polls, self.ends, self.restart_after, self.boot, self.reads = polls, ends, restart_after, 0x1234, 0
        self.state, self.op_ = S["idle"], 0
        super().__init__({JOB: self.start, STATUS: self.status, CANCEL: self.cancel})

    def clock(self):
        return h.ClockReading(0, 0, 0, self.boot, 0)

    def start(self, p):
        self.state, self.op_, self.reads = S["running"], JOB, 0
        return m.Result(1, m.COMPLETED, m.SUCCESS, b"")

    def cancel(self, p):
        if self.state == S["running"] and self.ends != "never":
            self.state = S["cancelled"]
        return m.Result(1, m.COMPLETED, m.SUCCESS, b"")

    def status(self, p):
        self.reads += 1
        if self.restart_after is not None and self.reads > self.restart_after:
            self.boot, self.state, self.op_ = 0x9999, S["idle"], 0
        elif self.state == S["running"] and self.ends != "never" and self.reads > self.polls:
            self.state = S[self.ends]
        code, msg = (5, b"bus stuck") if self.state == S["failed"] else (0, b"")
        return m.Result(1, m.COMPLETED, m.SUCCESS,
                        struct.pack("<BBiH", self.state, self.op_, code, len(msg)) + msg + struct.pack("<QQ", 100, 200))


@pytest.fixture
def jobs(monkeypatch):
    def make(**kw):
        p = JobProbe(**kw)
        monkeypatch.setattr(core, "find", lambda hst, name, revision=None: 6)
        monkeypatch.setattr(core, "revision", lambda hst, name, fn: 1)
        monkeypatch.setattr(core, "describe", lambda hst, fn: catalog.split_tlv(JOB_DESCRIBE))
        monkeypatch.setattr(core, "offers", lambda hst, fn, op: op in (JOB, STATUS, CANCEL))
        return p
    return make


def test_call_wait_done(jobs):
    p = jobs(polls=2)
    waited = []
    st = c.call_wait(p, "io.github.you.jobs", "collect", count=3, timeout=5, wait=waited.append)
    assert st["state"] == S["done"] and st.start_ns == 100 and st.end_ns == 200
    assert p.sent[0] == (6, JOB, b"\x03", True)
    assert all(s[1] == STATUS and s[3] is False for s in p.sent[1:])   # the status: lock-free, outside the session
    assert len(waited) == 2


def test_call_wait_failed_cancelled_timeout(jobs):
    with pytest.raises(c.JobFailed) as e:
        c.call_wait(jobs(ends="failed"), "io.github.you.jobs", "collect", count=1, timeout=5, wait=lambda s: None)
    assert e.value.code == 5 and e.value.message == "bus stuck" and e.value.op == "collect"
    p = jobs(ends="never")
    with pytest.raises(c.JobTimeout):
        c.call_wait(p, "io.github.you.jobs", "collect", count=1, timeout=0, wait=lambda s: None)
    assert p.sent[-1][1] == CANCEL
    p = jobs(polls=99)
    with pytest.raises(c.JobCancelled):
        c.call_wait(p, "io.github.you.jobs", "collect", count=1, timeout=5,
                    wait=lambda s: p.cancel(b""))                  # someone cancels it while this waits
    assert issubclass(c.JobTimeout, TimeoutError)


def test_call_wait_restarted(jobs):
    with pytest.raises(c.ProbeRestarted):
        c.call_wait(jobs(polls=99, restart_after=1), "io.github.you.jobs", "collect", count=1, timeout=5,
                    wait=lambda s: None)
