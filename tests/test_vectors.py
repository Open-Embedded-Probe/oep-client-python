"""oep-spec's test vectors (tests/vectors/*.json, copied here by tools/sync_registry.sh, never edited) against this
client's own code: COBS and serial frames (cobs), headers and TLVs (message), confirm (host and the fake), discovery
(list, describe and the header refusals of the smallest probe: the fake's answers and the host's reading), the CRCs,
probe.config's canonical form and hash (config and the fake), the refusals, the session scenarios (sessions.json:
every step to the fake in order) and the per-op vectors (ops.json: the fake in the state each case names, and the
client's request and reading of the answer where it has the op) - each request sent to the fake probe with the
vector's fn numbers, its answer compared byte for byte. Where a vector and this code disagree, the spec's text decides
(core §0 rule 4) and the vector is the one the spec corrects."""

import json
import struct
import zlib
from pathlib import Path

import pytest

from oep_client import catalog, cobs, config, core, endpoint, fake, fake_capture, host as h, message as m, registry as reg

PLAN_APPLY, PLAN_RELEASE = core.OP_PLAN_APPLY, core.OP_PLAN_RELEASE

HERE = Path(__file__).resolve().parent / "vectors"
SPEC = Path(__file__).resolve().parents[2] / "oep-spec" / "tests" / "vectors"


def load(name: str) -> dict:
    return json.loads((HERE / name).read_text(encoding="utf-8"))


def hx(s: str) -> bytes:
    return bytes.fromhex(s)


class Clock:
    def __call__(self):
        return 0


def test_the_copy_is_the_specs():
    """The synced copy matches the sibling oep-spec checkout when there is one (tools/sync_registry.sh)."""
    if not SPEC.is_dir():
        pytest.skip("no sibling oep-spec checkout")
    mine = {p.name: p.read_bytes() for p in HERE.glob("*.json")}
    theirs = {p.name: p.read_bytes() for p in SPEC.glob("*.json")}
    assert mine == theirs, "run tools/sync_registry.sh"


# ---- COBS and serial frames (transports §1) --------------------------------------------------------------------------

@pytest.mark.parametrize("case", load("cobs.json")["encode"], ids=lambda c: c["name"])
def test_cobs_encode_and_decode(case):
    assert cobs.encode(hx(case["data_hex"])).hex() == case["encoded_hex"]
    assert cobs.decode(hx(case["encoded_hex"])).hex() == case["data_hex"]


@pytest.mark.parametrize("case", load("cobs.json")["decode_also_accepts"], ids=lambda c: c["name"])
def test_cobs_decode_also_accepts(case):
    assert cobs.decode(hx(case["encoded_hex"])).hex() == case["data_hex"]


@pytest.mark.parametrize("case", load("cobs.json")["frames"], ids=lambda c: c["name"])
def test_serial_frames(case):
    msg = hx(case["message_hex"])
    assert cobs.crc16(msg) == case["crc16"]
    assert cobs.frame(msg).hex() == case["frame_hex"]
    assert cobs.unframe(hx(case["frame_hex"])[1:-1]) == msg


# ---- headers and TLVs (core §2.2, §4.1, §4.2) ---------------------------------------------------------------------

@pytest.mark.parametrize("case", load("headers.json")["requests"], ids=lambda c: c["name"])
def test_request_headers(case):
    req = m.Request(case["corr"], case["fn"], case["op"], hx(case["payload_hex"]), case["session_id"])
    assert req.pack().hex() == case["message_hex"]
    back = m.Request.unpack(hx(case["message_hex"]))
    assert back == req and hx(case["message_hex"])[0] == case["role"]


@pytest.mark.parametrize("case", load("headers.json")["answers"], ids=lambda c: c["name"])
def test_answer_headers(case):
    res = m.Result(case["corr"], case["resolution"], case["detail"], hx(case["payload_hex"]))
    assert res.pack().hex() == case["message_hex"] and m.Result.unpack(hx(case["message_hex"])) == res
    assert hx(case["message_hex"])[0] == case["role"]


@pytest.mark.parametrize("case", load("headers.json")["tlvs"], ids=lambda c: c["name"])
def test_tlvs(case):
    value = hx(case["value_hex"]) if "value_hex" in case else bytes([case["value_byte"]]) * case["value_len"]
    if case["tag"] & 0x7F == m.TAG_IGNORED:                            # the probe's own list: a host never sends it
        assert endpoint.ignored_tlv(list(value)).hex() == case["tlv_hex"]
        assert m.Tail.parse(hx(case["tlv_hex"])).ignored == list(value)
    else:
        assert m.tlv(case["tag"] & 0x7F, value, critical=bool(case["tag"] & 0x80)).hex() == case["tlv_hex"]
    assert m.split_tlvs(hx(case["tlv_hex"])) == [(case["tag"], value)]


# ---- CRCs ---------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("case", [c for c in load("checks.json")["cases"] if c["algorithm"] != "crc8-dmseq"],
                         ids=lambda c: c["name"])
def test_crc_check_values(case):
    data = hx(case["input_hex"])
    got = cobs.crc16(data) if case["algorithm"] == "crc16-ccitt-false" else zlib.crc32(data)   # core §5.2: IEEE
    assert got == case["crc"]


def test_the_dmseq_crc8_vectors_have_no_counterpart_here():
    """The dmseq CRC-8 and DATA0 words (target-console-dmseq) are the probe's and the target's: this client reads a
    console's bytes, it never decodes dmseq words. Listed so a new algorithm in checks.json is not missed."""
    algos = {c["algorithm"] for c in load("checks.json")["cases"]}
    assert algos == {"crc16-ccitt-false", "crc32-ieee", "crc8-dmseq"}


# ---- confirm (core §7.1) ------------------------------------------------------------------------------------------

def vector_probe(fns: dict, max_frame: int = 1024, ops: dict | None = None) -> fake.FakeProbe:
    """A fake probe whose fn numbers are a vector's: fn 0 with one UART bridge (index 0), then each named interface
    with channels 0-15 for its roles (the wire on pins 1 / 2; gpio without drive_levels). `ops`: fn -> the ops its
    ops tag sets instead of every op of its table (core §1.2, §7.4)."""
    core = fake._core("1.0.0", "vectors", "0123456789ab", 16, [], "", {},
                      fake._transports([(fake.TRANSPORT["uart_bridge"], 0xFF)]))
    chans = list(range(16))
    offered = [core]
    for fn, name in sorted(((int(k), v) for k, v in fns.items())):
        if name == "oep.fixture.gpio":
            o = fake._gpio(fn, chans, drive=False)
        elif name == "oep.fixture.i2c-target":
            o = fake._i2c_target(fn, chans, max_length=16, max_hz=100_000, features=0, queue_depth=4)
        elif name == "oep.wire.rvswd":
            o = fake.Offered(fn, 0, name, (catalog.channel_group(1, [(1, 1), (2, 2)]),
                                           catalog.u32(catalog.MAX_CLOCK_HZ, 4_000_000)))
        elif name == "oep.fixture.uart":
            o = fake._uart(fn, 0, chans, 3_000_000)
        elif name == "oep.target.riscv-dm":                            # every op offered: the default ops tag
            o = fake.Offered(fn, 0, name, (catalog.u16(catalog.MAX_LENGTH, 256),))
        elif name == "oep.target.console":
            o = fake._console(fn)
        elif name == "oep.probe.link":
            o = fake._link(fn)                                         # source and sink (no UART bridge speed)
        elif name == "oep.probe.plan":
            o = fake._plan(fn)
        elif name == "oep.probe.restart":
            o = fake._restart(fn)
        elif name == "oep.probe.config":
            o = fake._config(fn, 0, slots_max=2, storage=0)            # no storage: save / erase not in its ops
        elif name == "oep.fixture.logic":
            o = fake.Offered(fn, 0, name, fake._roles({k: chans for k in range(4)})
                             + fake._capture_decl(["one_shot"], 8, [8], 4096, 1, 480))
        else:
            raise AssertionError(f"a vector names {name}: add it here")
        if ops and fn in ops:
            o = fake.Offered(o.fn, o.instance, o.name, (catalog.ops_tlv(ops[fn]),) + tuple(
                t for t in o.tlvs if t[0] != catalog.OPS))
        offered.append(o)
    return fake.FakeProbe("vectors", max_frame, offered)


CONFIRM = load("confirm.json")["exchanges"]


@pytest.mark.parametrize("case", CONFIRM, ids=lambda c: c["name"])
def test_confirm_request_bytes(case):
    q = case["request"]
    req = m.Request(q["corr"], 0, m.OP_CONFIRM, m.CONFIRM_REQUEST + bytes([q["min_rev"], q["max_rev"]]))
    assert req.pack().hex() == case["request_hex"]
    if "request_serial_frame_hex" in case:
        assert cobs.frame(req.pack()).hex() == case["request_serial_frame_hex"]
    if "request_length_frame_hex" in case:
        assert (struct.pack("<H", len(req.pack())) + req.pack()).hex() == case["request_length_frame_hex"]


@pytest.mark.parametrize("case", CONFIRM, ids=lambda c: c["name"])
def test_confirm_answer_from_the_fake_and_read_by_the_host(case):
    a = case["answer"]
    ep = endpoint.Endpoint(vector_probe({}, a.get("max_frame", 1024)), Clock(), boot_id=a.get("boot_id", 0),
                           window=a.get("window", 4096), max_inflight=a.get("max_inflight", 4))
    out = ep.handle(hx(case["request_hex"]), a.get("transport", 0))
    assert out.hex() == case["answer_hex"]
    if "answer_serial_frame_hex" in case:
        assert cobs.frame(out).hex() == case["answer_serial_frame_hex"]
    hst = h.Host(lambda b: hx(case["answer_hex"]))
    hst._corr = case["request"]["corr"] - 1                            # the host's next corr is the vector's
    q = case["request"]
    if "reason" in a:
        with pytest.raises(h.Unsupported) as e:
            hst.confirm(q["min_rev"], q["max_rev"])
        assert e.value.tag is None and list(e.value.supported) == a["supported"]
    else:
        limits = hst.confirm(q["min_rev"], q["max_rev"])
        assert {k: limits[k] for k in ("revision", "flags", "max_frame", "window", "max_inflight", "boot_id",
                                       "transport")} == {k: a[k] for k in ("revision", "flags", "max_frame", "window",
                                                                         "max_inflight", "boot_id", "transport")}


# ---- discovery: list, describe and the header refusals of the smallest probe (core §7.2, §7.3, §4.3 order 1) -------

DISCOVERY = load("discovery.json")


def smallest_probe() -> endpoint.Endpoint:
    """The vectors' smallest probe (discovery.json about): no interface, fn 0 whose ops are the core's eight (all
    mandatory, core §1.2), one UART
    bridge (index 0, interface 0xFF), unit_id "a1b2c3d4", discoverable 0, max_op_ms 1000 - its describe in that order
    (ops first), nothing else."""
    t = reg.CORE.tlv["describe"]
    core = fake.Offered(0, 0, fake.CORE_NAME, (catalog.ops_tlv(reg.CORE.op[k] for k in fake.CORE_REQUIRED),
                                           catalog.text(t["unit_id"], "a1b2c3d4"),
                                           catalog.tlv(t["transport"], bytes([0, fake.TRANSPORT["uart_bridge"], 0xFF])),
                                           catalog.u8(t["discoverable"], 0),
                                           catalog.u32(t["max_op_ms"], 1000)))
    return endpoint.Endpoint(fake.FakeProbe("smallest", 64, [core]), Clock())


def test_discovery_from_the_fake_byte_for_byte():
    """Every exchange and refusal in order on one smallest probe (no session: list and describe are lock-free, and the
    refusals come at order 1, before the session check)."""
    ep = smallest_probe()
    for case in DISCOVERY["exchanges"] + DISCOVERY["refusals"]:
        out = ep.handle(hx(case["request_hex"]), 0)
        assert out.hex() == case["answer_hex"], case["name"]
        if "answer_serial_frame_hex" in case:
            assert cobs.frame(out).hex() == case["answer_serial_frame_hex"], case["name"]


@pytest.mark.parametrize("case", DISCOVERY["exchanges"], ids=lambda c: c["name"])
def test_discovery_requests_as_the_host_sends_them(case):
    q = case["request"]
    if "prefix" in q:
        payload = catalog.pack_list_request(q["prefix"], bool(q["flags"] & catalog.LIST_EXACT), q["first"])
        assert q["flags"] == (catalog.LIST_EXACT if q["flags"] & catalog.LIST_EXACT else 0)
        op = m.OP_LIST
    else:
        payload, op = catalog.pack_describe_request(q["fn"], q["first"]), m.OP_DESCRIBE
    req = m.Request(q["corr"], 0, op, payload)
    assert req.pack().hex() == case["request_hex"]
    if "request_serial_frame_hex" in case:
        assert cobs.frame(req.pack()).hex() == case["request_serial_frame_hex"]


@pytest.mark.parametrize("case", DISCOVERY["exchanges"], ids=lambda c: c["name"])
def test_discovery_answers_as_the_host_reads_them(case):
    a = case["answer"]
    res = m.Result.unpack(hx(case["answer_hex"]))
    assert (res.corr, res.succeeded) == (a["corr"], True)
    if "entries" in a:
        total, entries = catalog.unpack_list_result(res.payload)
        assert total == a["total"]
        assert [(e.fn, e.instance, e.revision, e.flags, e.name) for e in entries] == [
            (e["fn"], e["instance"], e["revision"], e["flags"], e["name"]) for e in a["entries"]]
        return
    assert res.payload[0] == a["more"]
    tlvs = m.split_tlvs(res.payload[1:])
    if "unit_id" not in a:
        assert tlvs == []                                              # past the end: more 0 and no TLVs (core §7.3)
        return
    hst = h.Host(lambda b: hx(case["answer_hex"]))
    hst._corr = a["corr"] - 1
    t = reg.CORE.tlv["describe"]
    with_core = {k: v for k, v in tlvs}
    assert with_core[t["unit_id"]].decode() == a["unit_id"]
    assert sorted(catalog.unpack_ops(with_core[catalog.OPS])) == a["ops"]
    hst._corr = a["corr"] - 1
    assert core.ops(hst, 0) == set(a["ops"])
    hst._describes.clear()
    hst._corr = a["corr"] - 1
    assert core.transports(hst) == [(x["index"], x["kind"], x["interface"]) for x in a["transports"]]
    hst._corr = a["corr"] - 1
    hst._describes.clear()
    assert core.max_op_ms(hst) == a["max_op_ms"]


@pytest.mark.parametrize("case", DISCOVERY["refusals"], ids=lambda c: c["name"])
def test_discovery_refusals_as_the_host_reads_them(case):
    res = m.Result.unpack(hx(case["answer_hex"]))
    assert res.resolution == m.REJECTED and res.payload == b""
    assert res.detail == reg.REJECT_REASONS[case["answer"]]


# ---- probe.config's canonical form and hash (probe-config §2) -------------------------------------------------------

@pytest.mark.parametrize("case", load("probe_config_hash.json")["cases"], ids=lambda c: c["name"])
def test_probe_config_canonical_form_and_hash(case):
    items = [(it["tag"], hx(it["value_hex"])) for it in case["items_sent"]]
    assert config.canonical(items).hex() == case["canonical_hex"]
    assert config.hash_of(items) == case["hash"]
    held: dict = {}                                                    # the fake's own: keys as a set keeps them
    item = reg.PROBE_CONFIG.tlv["item"]
    for tag, value in items:
        tag &= 0x7F
        if tag == item["plan"]:
            held.setdefault((tag, struct.unpack_from("<H", value)[0]), []).append(value)
        else:
            held[(tag, endpoint.Endpoint._key_len(tag) == 1 and value[0] or struct.unpack_from("<H", value)[0])] = value
    assert b"".join(endpoint.Endpoint._canonical(held)).hex() == case["canonical_hex"]
    assert [(t, v.hex()) for t, v in m.split_tlvs(hx(case["canonical_hex"]))] == [
        (it["tag"], it["value_hex"]) for it in case["canonical_order"]]


# ---- refusals and the ignored list (core §4.3, §2.3) ----------------------------------------------------------------

REFUSALS = load("refusals.json")
SESSION = 0x11223344                                                    # the vectors' lock holder (refusals.json about)


def refusal_endpoint() -> endpoint.Endpoint:
    fns = {}
    for case in REFUSALS["cases"]:
        fns.update(case["fns"])
    ep = endpoint.Endpoint(vector_probe(fns), Clock())
    hst = h.Host(lambda b: ep.handle(b, 0))
    hst.rng = type("Fixed", (), {"randrange": staticmethod(lambda a, b: SESSION)})()   # the vectors' session id
    hst.open(3000)
    gpio = next((int(k) for k, v in fns.items() if v == "oep.fixture.gpio"), None)
    if gpio is not None:                                                # "channel 3 is in fn 2's plan"
        hst._corr = 0
        ep.handle(m.Request(1, ep.fns[fake.PLAN], PLAN_APPLY, m.tlv(0x10, struct.pack("<HBH", gpio, 1, 3), critical=True),
                            SESSION).pack(), 0)
    return ep


def test_refusals_from_the_fake_byte_for_byte():
    """Every case in order on one fake probe (their corrs rise, as one session's do: core §5.2)."""
    ep = refusal_endpoint()
    for case in REFUSALS["cases"]:
        out = ep.handle(hx(case["request_hex"]), 0)
        assert out.hex() == case["answer_hex"], case["name"]


@pytest.mark.parametrize("case", REFUSALS["cases"], ids=lambda c: c["name"])
def test_refusals_as_the_host_reads_them(case):
    res = m.Result.unpack(hx(case["answer_hex"]))
    want = case["answer"]
    if want == "completed success":
        assert res.succeeded
        tail = m.Tail.parse(res.payload)                                # gpio set answers no fixed part (fixture §1)
        assert tail.ignored and not tail.more_ignored
        return
    assert want in ("malformed", "unsupported"), f"a new kind of answer in refusals.json: {want}"
    assert res.resolution == m.REJECTED and m.REJECT_NAMES[res.detail].split()[0] == want
    err = h.rejection(res)
    if want == "unsupported":
        assert isinstance(err, h.Unsupported) and (err.tag is None) == (res.payload[:1] == b"\x00")


# ---- session scenarios (sessions.json: core §5.2, §6, §9) -----------------------------------------------------------

SESSIONS = load("sessions.json")
EXAMPLE_BOOT_ID = 0x12345678                                            # confirm.json's example probe (sessions.json about)


@pytest.mark.parametrize("scenario", SESSIONS["scenarios"], ids=lambda c: c["name"])
def test_session_scenarios_on_the_fake_step_by_step(scenario):
    """Each scenario from a fresh probe, every step on one transport, the answer byte for byte (no time passes on the
    timers' clock; the probe's clock, which clock answers, reads 2 s and then 1 ms more at every read - the example
    values of sessions.json)."""
    reads = iter(range(2_000_000_000, 3_000_000_000, 1_000_000))
    ep = endpoint.Endpoint(vector_probe({}), Clock(), boot_id=EXAMPLE_BOOT_ID, now_ns=lambda: next(reads))
    for step in scenario["steps"]:
        out = ep.handle(hx(step["request_hex"]), 0)
        assert out is not None and out.hex() == step["answer_hex"], step["note"]


@pytest.mark.parametrize("scenario", SESSIONS["scenarios"], ids=lambda c: c["name"])
def test_session_scenarios_as_the_host_reads_them(scenario):
    """The host's reading of each answer: open -> Opened(lease_ms, boot_id), locked -> Locked (remaining, owner),
    no_session -> NoSession, session_required / malformed -> Rejected; and the requests it sends for open (the id in
    the header, core §4.1), keepalive, end and lock_state."""
    for step in scenario["steps"]:
        req = m.Request.unpack(hx(step["request_hex"]))
        res = m.Result.unpack(hx(step["answer_hex"]))
        assert res.corr == req.corr
        sent = []
        hst = h.Host(lambda b: sent.append(b) or hx(step["answer_hex"]))
        hst._corr, hst.revision = req.corr - 1, 1
        hst.rng = type("Fixed", (), {"randrange": staticmethod(lambda a, b, sid=req.session: sid)})()
        if req.op == m.OP_OPEN:
            lease, force = struct.unpack_from("<IB", req.payload)
            owner = m.Tail.parse(req.payload[5:]).get(h.OWNER)
            call = lambda: hst.open(lease, force=bool(force), owner=owner.decode() if owner else None)  # noqa: E731
        elif req.op == m.OP_LOCK_STATE:
            call = hst.lock_owner
        elif req.op == m.OP_CLOCK:
            if req.session:
                continue                                               # the host's clock always goes with session_id 0
            reading = hst.clock()
            assert (reading.boot_id, reading.uptime_ns) == struct.unpack_from("<IQ", res.payload)
            assert reading.round_trip_ns == reading.after_ns - reading.before_ns >= 0
            assert sent[0] == hx(step["request_hex"]), step["note"]
            continue
        else:
            hst.session = req.session or None
            call = {m.OP_KEEPALIVE: hst.keepalive, m.OP_END: hst.end}[req.op]
        if res.resolution == m.REJECTED:
            with pytest.raises(h.Rejected) as e:
                call()
            assert e.value.result == res and isinstance(e.value, h._REJECTS.get(res.detail, h.Rejected))
            if res.detail == m.LOCKED:
                locked = m.Reader(res.payload).u32()
                assert e.value.remaining_ms == locked
            if req.session == 0 or (req.op == m.OP_OPEN and req.session == 0):
                continue                                               # a request the host never makes (session_id 0)
        else:
            got = call()
            if req.op == m.OP_OPEN:
                assert (got.lease_ms, got.boot_id) == struct.unpack("<II", res.payload[:8])
            if req.op == m.OP_LOCK_STATE:
                assert got[:2] == (bool(res.payload[0]), struct.unpack_from("<I", res.payload, 1)[0])
        if req.session or req.op != m.OP_OPEN:
            assert sent and sent[0] == hx(step["request_hex"]), step["note"]


# ---- per-op vectors (ops.json): the fake in each case's state, and the client's side --------------------------------

OPS = load("ops.json")["cases"]
S = SESSION                                                             # ops.json about: the lock holder's id


def req(ep, corr, fn, op, payload=b"", session=S):
    """A setup request (corrs below every vector's, so none of them is taken for an old one, core §5.2)."""
    out = m.Result.unpack(ep.handle(m.Request(corr, fn, op, payload, session).pack(), 0))
    assert out.succeeded, out.describe()
    return out


def held(ep):
    req(ep, 1, 0, m.OP_OPEN, struct.pack("<IB", 3000, 0))


def stream_two(ep, data=b"", marks=()):
    """Console stream 2 on connection 1 as the cases' state has it (one number space, core §9: an attach made
    connection 1, the open stream 2), its bytes and marks put in place."""
    wire = next(fn for fn, name in ep.names.items() if name == "oep.wire.rvswd")
    console_fn = next(fn for fn, name in ep.names.items() if name == "oep.target.console")
    held(ep)
    assert attach(ep, wire) == 1
    sid = struct.unpack_from("<H", req(ep, 3, console_fn, 0x01, struct.pack("<HB", 1, 2)).payload)[0]
    assert sid == 2
    s = ep.streams[sid]
    s.data, s.marks, s.serial = bytearray(data), list(marks), len(marks)
    return s


def attach(ep, wire, corr=2, method=0, speed=4_000_000):
    payload = bytes([method]) + m.tlv(0x01, struct.pack("<I", speed), critical=True) + \
        m.tlv(0x03, struct.pack("<HH", 1, 2), critical=True)
    return struct.unpack_from("<H", req(ep, corr, wire, 0x02, payload).payload)[0]


def setup_case(case):
    """The fake in the state the case names (`state`), with every fn the case lists and a wire where a connection
    is wanted. -> the endpoint."""
    fns = dict(case["fns"])
    names = set(fns.values())
    if names & {"oep.target.riscv-dm", "oep.target.console", "oep.probe.config"} and "oep.wire.rvswd" not in names:
        # a wire under the case's fns: the connection there is number 1 (core §9)
        fns["4" if "4" not in fns else "14"] = "oep.wire.rvswd"
    if "oep.probe.config" in names:
        fns.setdefault("7" if "7" not in fns else "17", "oep.target.console")
    ops = {}
    if "dmi, halt, resume" in case["state"]:
        ops[int(next(k for k, v in case["fns"].items() if v == "oep.target.riscv-dm"))] = {1, 2, 3}
    clock = type("Settable", (), {"t": 0, "__call__": lambda self: self.t})()
    ep = endpoint.Endpoint(vector_probe(fns, ops=ops), clock, boot_id=EXAMPLE_BOOT_ID)
    wire = next((int(k) for k, v in fns.items() if v == "oep.wire.rvswd"), None)
    name, state = case["name"], case["state"]
    plan = ep.fns.get(fake.PLAN)
    if name.startswith("restart"):
        if "holds the lock" in state:
            held(ep)
    elif name.startswith("plan_"):
        if "without a session" not in name:
            held(ep)
        if "has the plan above" in state:
            req(ep, 2, plan, PLAN_APPLY, m.tlv(0x10, struct.pack("<HBH", 2, 1, 3), critical=True))
    elif name.startswith("gpio"):
        held(ep)
        req(ep, 2, plan, PLAN_APPLY, m.tlv(0x10, struct.pack("<HBH", 2, 1, 3), critical=True))   # channel 3 only
        if "after the set" in state:
            req(ep, 3, 2, reg.FIXTURE_GPIO.op["set"], bytes([1]) + struct.pack("<HB", 3, 4))
    elif name.startswith("rvswd connections"):
        held(ep)
        ep._target(wire, (1, 2)).target_id = 0x00203500
        attach(ep, wire, speed=1_000_000)
    elif name.startswith("rvswd scan"):
        held(ep)
        ep._target(wire, (1, 2)).dmstatus = lambda: 0x00400382
    elif name.startswith("riscv-dm"):
        held(ep)
        if "connection 1" in state:
            attach(ep, wire)
        ep._target(wire, (1, 2)).dmi[0x11] = 0x00400382
    elif name.startswith("console marks"):
        stream_two(ep, marks=[(0, 0, reg.COMMON.enum["mark_kind"]["attach"], 1_000_000, 0)])
    elif name.startswith("console streams"):
        stream_two(ep)
    elif name.startswith("console read"):
        stream_two(ep, data=b"hello")
    elif name.startswith("probe.config state"):
        clock.t = 1                                                    # the slot's last try at 1 ms
        ep._target(wire, (1, 2)).target_id = 0x00203500
        slot = struct.pack("<BHHHBBIIBBB", 0, wire, 1, 2, 1, 0, 0, 0, 0, 2, 1) + b"s" + b"\x00"
        ep.load_config([m.tlv(0x04, slot), m.tlv(0x05, struct.pack("<BBBBBH", 0, 0, 0, 1, 1, 0))], saved=False)
    elif name.startswith("probe.config"):
        held(ep)
    elif name.startswith("logic"):
        logic = int(next(k for k, v in fns.items() if v == "oep.fixture.logic"))
        if "session S" in state:
            held(ep)
        if "fn 9 subscribed" in state:
            req(ep, 2, logic, m.OP_SUBSCRIBE, struct.pack("<HI", 0, 0))
        cap = ep.captures[logic]
        cap.generation, cap.state = 1, fake_capture.STATE["done"]
        cap.segs = [fake_capture.Segment(0, 0, 1000, 5_000_000, 50, fake_capture.NONE, 0, 1)]
    return ep


@pytest.mark.parametrize("case", OPS, ids=lambda c: c["name"])
def test_ops_vectors_from_the_fake_byte_for_byte(case):
    ep = setup_case(case)
    out = ep.handle(hx(case["request_hex"]), 0)
    assert out is not None and out.hex() == case["answer_hex"]


def client(case, session=None):
    """A host whose next request is the case's: its corr, its session (`session`, else none), the case's fn numbers
    known, and the case's answer whatever it sends. -> (host, the requests sent)."""
    r = m.Request.unpack(hx(case["request_hex"]))
    sent = []
    hst = h.Host(lambda b: sent.append(b) or hx(case["answer_hex"]))
    hst._corr, hst.revision, hst.session = r.corr - 1, 1, session
    hst.limits = {"max_frame": 1024, "window": 4096, "max_inflight": 4, "transport": 0}
    for k, v in case["fns"].items():
        hst._fns[v], hst._revisions[int(k)], hst._describes[int(k)] = int(k), 1, []
    hst._describes[0] = []
    return hst, sent


def _on_client(case):
    """The client's own request for the case and its reading of the answer; None when this client has no API for
    that request as it stands (the scan with a skip of its own, the logic read with a max it does not choose)."""
    from oep_client import capture as cap, config as cfg, console as con, fixture as fix, riscv as rv
    name = case["name"]
    a = m.Result.unpack(hx(case["answer_hex"]))
    if name.startswith("restart"):
        hst, sent = client(case, None if "without a session" in name else S)
        if a.resolution == m.COMPLETED:
            hst.request_restart()
            assert hst.session is None and hst._fns == {}                # nothing of the old boot lasts (core §6.5)
        else:
            with pytest.raises(h.Rejected) as e:
                hst.request_restart()
            assert e.value.result.detail == a.detail
        return sent
    if name.startswith("plan_"):
        if "n = 2 with one fn" in name:
            return None                                                 # the client never sends a short list
        hst, sent = client(case, None if "without a session" in name else S)
        body = m.Request.unpack(hx(case["request_hex"])).payload
        if name.startswith("plan_release"):
            core.plan_release(hst, [2])
            return sent
        assignment = struct.unpack_from("<HBH", body, 3)
        if a.resolution == m.COMPLETED:
            core.plan_apply(hst, [assignment])
        else:
            with pytest.raises(h.Rejected) as e:
                core.plan_apply(hst, [assignment])
            assert e.value.result.detail == a.detail
        return sent
    if name.startswith("logic subscribe") or name.startswith("logic unsubscribe"):
        hst, sent = client(case, None if "without a session" in name else S)
        c = cap.LogicCapture(hst, 9)
        if a.resolution == m.REJECTED:
            with pytest.raises(h.Rejected, match="session required"):
                c.subscribe()
        elif "unsubscribe" in name:
            c.unsubscribe()
        else:
            c.subscribe(1024, 20)
        return sent
    if name.startswith("gpio subscribe"):
        hst, sent = client(case, S)
        with pytest.raises(h.Rejected) as e:
            hst.subscribe(2)
        assert type(e.value) is h.Rejected and e.value.result.detail == m.UNKNOWN_OPERATION
        return sent
    if name.startswith("link source"):
        hst, sent = client(case)
        n = struct.unpack_from("<I", m.Request.unpack(hx(case["request_hex"])).payload)[0]
        data = core.link_source_data(hst.call(1, core.LINK_SOURCE, core.link_source_request(n), locked=False).payload)
        assert data == bytes(k & 0xFF for k in range(min(n, core.link_size(1024))))   # max_frame - 26 (§2)
        return sent
    if name.startswith("link sink") or name.startswith("link port_speed"):
        hst, sent = client(case, S if "port_speed" in name else None)
        if "port_speed" in name:
            with pytest.raises(h.Rejected) as e:
                hst.call(1, core.LINK_PORT_SPEED, struct.pack("<BIBHI", 0, 921600, 0, 2000, 3000))
            assert type(e.value) is h.Rejected and e.value.result.detail == m.UNKNOWN_OPERATION   # WireSkein's check
            return sent
        body = m.Request.unpack(hx(case["request_hex"])).payload
        count = struct.unpack_from("<H", body)[0]
        if count > len(body) - 2:
            return None                                                 # a count the client never sends
        hst.call(1, core.LINK_SINK, core.link_sink_request(body[2:2 + count]), locked=False)
        return sent
    if name.startswith("gpio"):
        if "n = 2 with one element" in name:
            return None                                                 # the client never sends a short list
        hst, sent = client(case, None if "without a session" in name else S)
        g = fix.Gpio(hst, 2)
        if name.startswith("gpio read"):
            assert g.read([3]) == [1]
        elif "channel 9" in name:
            with pytest.raises(h.Unavailable) as e:
                g.set([(3, g.OUTPUT_HIGH), (9, g.INPUT)])
            assert e.value.channels == [9]
        elif "without a session" in name:
            with pytest.raises(h.Rejected, match="session required"):
                g.set([(3, g.OUTPUT_HIGH)])
        else:
            g.set([(3, g.OUTPUT_HIGH)])
        return sent
    if name.startswith("rvswd connections"):
        if "first beyond" in name:
            return None                                                 # the client pages from 0 on
        hst, sent = client(case)
        info = rv.Wire(hst, "oep.wire.rvswd").connections()
        if a.payload[1]:
            (c,) = info
            assert (c.connection, c.pins, c.speed_hz, c.users, c.slot, c.target_id) == \
                (1, (1, 2), 1_000_000, 1, None, (1, struct.pack("<I", 0x00203500)))
        else:
            assert info == []
        return sent
    if name.startswith("rvswd scan"):
        if "count 0" in name or "with skip" in name:
            return None                                                 # skip: the client's own loop sends it
        hst, sent = client(case, S)
        (found,) = rv.Wire(hst, "oep.wire.rvswd").scan([(1, 2)], max_speed=None)
        assert (found.kind, found.pins, found.dmstatus) == (1, (1, 2), 0x00400382)
        return sent
    if name.startswith("riscv-dm"):
        if "unknown step kind" in name:
            return None
        hst, sent = client(case, S)
        r = m.Request.unpack(hx(case["request_hex"]))
        dm = rv.RiscvDm(hst, struct.unpack_from("<H", r.payload)[0])
        if "halt" in name and "unknown connection" in name:
            with pytest.raises(h.NoConnection):
                dm.halt()
        elif "halt" in name:
            dm.halt()
        elif "run not offered" in name:
            with pytest.raises(h.Rejected) as e:
                dm.run(0x20000000, [], timeout_ms=100, outs=())
            assert type(e.value) is h.Rejected and e.value.result.detail == m.UNKNOWN_OPERATION
        elif "n = 0" in name:
            assert dm.dmi([]) == (0, [])
        else:
            assert dm.dmi([dm.step_read(0x11)]) == (1, [0x00400382])
        return sent
    if name.startswith("console"):
        hst, sent = client(case)
        c = con.Console(hst)
        c.stream = 2
        if name.startswith("console marks"):
            marks, more = c.marks_page(0)
            assert not more and [(k.serial, k.position, k.kind, k.time_ns, k.detail) for k in marks] == \
                [(0, 0, 3, 1_000_000, 0)]
        elif name.startswith("console streams"):
            assert c.streams() == [con.StreamInfo(2, 1, 2, 1, 0)]
        elif "from 4" in name:
            return None                                                 # the client never sends from 4
        else:
            assert tuple(c.read(c.FROM_POSITION, 5, 64)) == (5, False, False, b"")
        return sent
    if name.startswith("probe.config"):
        if "set" in name:
            return None                                                 # the client's Idle never sends these forms
        hst, sent = client(case, S if "save" in name else None)
        p = cfg.ProbeConfig(hst, fn=8)
        if "save" in name:
            with pytest.raises(h.Rejected) as e:
                p.save()
            assert type(e.value) is h.Rejected and e.value.result.detail == m.UNKNOWN_OPERATION
            return sent
        st = p.state()
        (slot,) = st.slots
        (bind,) = st.binds
        assert (st.storage, st.saved_hash, st.unreadable) == ("none", 0, None)
        assert (slot.slot, slot.state, slot.connection, slot.last_try_at_ns, slot.reset_at_ns, slot.target_id) == \
            (0, "connected", 1, 1_000_000, None, struct.pack("<I", 0x00203500))
        assert (bind.port, bind.mode, bind.selected, bind.flow) == (0, "last-reset", 0, "streaming")
        return sent
    if name.startswith("logic segments"):
        hst, sent = client(case)
        (seg,), more = cap.LogicCapture(hst, 9).segments_page(0)
        assert not more and (seg.serial, seg.position, seg.samples, seg.start_ns, seg.generation) == \
            (0, 0, 1000, 5_000_000, 1)
        return sent
    return None


@pytest.mark.parametrize("case", OPS, ids=lambda c: c["name"])
def test_ops_vectors_as_the_client_sends_and_reads_them(case):
    """Where this client has the op, the request it builds is the case's byte for byte and its reading of the answer
    gives the case's values (a refusal raises the class that names it, unknown_operation as plain Rejected)."""
    sent = _on_client(case)
    if sent is None:
        pytest.skip("not a request this client makes")
    assert sent[0].hex() == case["request_hex"]


# ---- the ops encoding (ops_encoding.json, core §7.4) -----------------------------------------------------------------

@pytest.mark.parametrize("case", load("ops_encoding.json")["cases"], ids=lambda c: c["name"])
def test_ops_encoding_as_the_host_reads_it(case):
    """A valid value decodes to the case's set and is the one encoding of it (pack_ops gives the same bytes); an
    invalid one is refused (check_ops says why, unpack_ops raises) - the host does not use that fn."""
    value = hx(case["value_hex"])
    if case["valid"]:
        assert catalog.check_ops(value) == ""
        assert sorted(catalog.unpack_ops(value)) == case["ops"]
        assert catalog.pack_ops(case["ops"]) == value
        assert catalog.decode_description(catalog.tlv(catalog.OPS, value)).ops == set(case["ops"])
    else:
        assert catalog.check_ops(value)
        with pytest.raises(catalog.InvalidOps):
            catalog.unpack_ops(value)
        d = catalog.decode_description(catalog.tlv(catalog.OPS, value))
        assert d.ops is None and d.ops_invalid


def test_every_ops_in_the_fakes_and_the_vectors_is_canonical():
    """Every describe the fake's profiles give, and the discovery vectors' ops, keep core §7.4's one encoding."""
    for make in fake.PROFILES.values():
        ep = endpoint.Endpoint(make(), Clock())
        for fn in ep.names:
            for t in ep._declarations(fn):
                if t[0] == catalog.OPS:
                    assert catalog.check_ops(t[3:]) == "", (make.__name__, fn)
    for case in DISCOVERY["exchanges"]:
        if "ops" in case["answer"]:
            assert catalog.pack_ops(case["answer"]["ops"]) in hx(case["answer_hex"])
