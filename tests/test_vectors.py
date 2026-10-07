"""oep-spec's test vectors (tests/vectors/*.json, copied here by tools/sync_registry.sh, never edited) against this
client's own code: COBS and serial frames (cobs), headers and TLVs (message), confirm (host and the virtual bench), discovery
(list, describe and the header refusals of the smallest probe: the virtual bench's answers and the host's reading), the CRCs,
the refusals, the session scenarios (sessions.json:
every step to the virtual bench in order) and the per-op vectors (ops.json: the virtual bench in the state each case names, and the
client's request and reading of the answer where it has the op) - each request sent to the virtual bench with the
vector's fn numbers, its answer compared byte for byte. Where a vector and this code disagree, the spec's text decides
(core §0 rule 4) and the vector is the one the spec corrects: `TEXT_OVER_VECTOR` names each such case, with what the
text says, and the test checks the virtual bench against the text."""

import json
import struct
from pathlib import Path

import pytest

from oep_client import catalog, cobs, config, core, endpoint, virtual_bench, virtual_bench_capture, host as h, message as m, registry as reg

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
    assert m.tlv(case["tag"] & 0x7F, value, critical=bool(case["tag"] & 0x80)).hex() == case["tlv_hex"]
    assert m.split_tlvs(hx(case["tlv_hex"])) == [(case["tag"], value)]


# ---- CRCs ---------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("case", [c for c in load("checks.json")["cases"] if c["algorithm"] != "crc8-dmseq"],
                         ids=lambda c: c["name"])
def test_crc_check_values(case):
    data = hx(case["input_hex"])
    assert case["algorithm"] == "crc16-ccitt-false" and cobs.crc16(data) == case["crc"]   # transports §1


def test_the_dmseq_crc8_vectors_have_no_counterpart_here():
    """The dmseq CRC-8 and DATA0 words (target-console-dmseq) are the probe's and the target's: this client reads a
    console's bytes, it never decodes dmseq words. Listed so a new algorithm in checks.json is not missed."""
    algos = {c["algorithm"] for c in load("checks.json")["cases"]}
    assert algos == {"crc16-ccitt-false", "crc8-dmseq"}                 # no CRC-32 any more (core §5.2: corr only)


# ---- confirm (core §7.1) ------------------------------------------------------------------------------------------

def vector_probe(fns: dict, max_frame: int = 1024, ops: dict | None = None, wifi_max: int = 0) -> virtual_bench.VirtualProbe:
    """A virtual bench whose fn numbers are a vector's: fn 0 with one UART bridge (index 0), then each named interface
    with channels 0-15 for its roles (the wire on pins 1 / 2; gpio without drive_levels). `ops`: fn -> the ops its
    ops tag sets instead of every op of its table (core §1.2, §7.4)."""
    core = virtual_bench._core("1.0.0", "vectors", "0123456789ab", 16, {},
                      virtual_bench._transports([(virtual_bench.TRANSPORT["uart_bridge"], 0xFF)]))
    chans = list(range(16))
    offered = [core]
    for fn, name in sorted(((int(k), v) for k, v in fns.items())):
        if name == "oep.fixture.gpio":
            o = virtual_bench._gpio(fn, chans, drive=False)
        elif name == "oep.fixture.i2c-target":
            o = virtual_bench._i2c_target(fn, chans, max_length=16, max_hz=100_000, features=0, queue_depth=4)
        elif name == "oep.wire.rvswd":
            o = virtual_bench.Offered(fn, 0, name, (catalog.channel_group(1, [(1, 1), (2, 2)]),
                                           catalog.u32(catalog.MAX_CLOCK_HZ, 4_000_000)))
        elif name == "oep.fixture.uart":
            o = virtual_bench._uart(fn, 0, chans, 3_000_000)
        elif name == "oep.target.riscv-dm":                            # every op offered: the default ops tag
            o = virtual_bench.Offered(fn, 0, name, (catalog.u16(catalog.MAX_LENGTH, 256),))
        elif name == "oep.target.console":
            o = virtual_bench._console(fn)
        elif name == "oep.probe.link":
            o = virtual_bench._link(fn)                                         # source and sink (no UART bridge speed)
        elif name == "oep.probe.plan":
            o = virtual_bench._plan(fn)
        elif name == "oep.probe.restart":
            o = virtual_bench._restart(fn)
        elif name == "oep.probe.config":
            o = virtual_bench._config(fn, 0, slots_max=2, storage=0, wifi_max=wifi_max)   # no storage: no save / erase
        elif name == "oep.fixture.logic":                              # 80 MHz / 4 = 20 MHz exact; w 2 for two channels
            o = virtual_bench.Offered(fn, 0, name, virtual_bench._roles({k: chans for k in range(4)})
                             + (catalog.u32(catalog.MAX_CLOCK_HZ, 80_000_000),)
                             + virtual_bench._capture_decl(["one_shot"], 8, 1 << 20, 1),
                             inner=virtual_bench._capture_inner([2, 8], 1, 480))
        elif name == "oep.fixture.analog":
            o = virtual_bench.Offered(fn, 0, name, virtual_bench._roles({k: chans for k in range(4)})
                             + virtual_bench._analog_decl([(0, 0, 3300, 0)], 4096), inner=virtual_bench._capture_inner([16], 1, 480))
        elif name == "oep.fixture.capture-group":
            tracks = sorted(int(k) for k, v in fns.items() if v in ("oep.fixture.logic", "oep.fixture.analog"))
            o = virtual_bench.Offered(fn, 0, name, virtual_bench._group_decl(tracks))
        else:
            raise AssertionError(f"a vector names {name}: add it here")
        if ops and fn in ops:
            o = virtual_bench.Offered(o.fn, o.instance, o.name, (catalog.ops_tlv(ops[fn]),) + tuple(
                t for t in o.tlvs if t[0] != catalog.OPS), inner=o.inner)
        offered.append(o)
    return virtual_bench.VirtualProbe("vectors", max_frame, offered)


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
def test_confirm_answer_from_the_virtual_bench_and_read_by_the_host(case):
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
    mandatory, core §1.2), one UART bridge (index 0, interface 0xFF), unit_id "a1b2c3d4", max_op_ms 1000 - its
    describe in that order (ops first), nothing else."""
    t = reg.CORE.tlv["describe"]
    core = virtual_bench.Offered(0, 0, virtual_bench.CORE_NAME, (catalog.ops_tlv(reg.CORE.op[k] for k in virtual_bench.CORE_REQUIRED),
                                           catalog.text(t["unit_id"], "a1b2c3d4"),
                                           catalog.tlv(t["transport"], bytes([0, virtual_bench.TRANSPORT["uart_bridge"], 0xFF])),
                                           catalog.u32(t["max_op_ms"], 1000)))
    return endpoint.Endpoint(virtual_bench.VirtualProbe("smallest", 64, [core]), Clock())


def test_discovery_from_the_virtual_bench_byte_for_byte():
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
    if "fn" not in q:                                                   # list: first(u16) alone (core §7.2)
        payload, op = catalog.pack_list_request(q["first"]), m.OP_LIST
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


# ---- refusals and an ignored unknown TLV (core §4.3, §2.3) -------------------------------------------------------------

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
        ep.handle(m.Request(1, ep.fns[virtual_bench.PLAN], PLAN_APPLY, m.tlv(0x10, struct.pack("<HBH", gpio, 1, 3)),
                            SESSION).pack(), 0)
    return ep


def test_refusals_from_the_virtual_bench_byte_for_byte():
    """Every case in order on one virtual bench (their corrs rise, as one session's do: core §5.2)."""
    ep = refusal_endpoint()
    for case in REFUSALS["cases"]:
        out = ep.handle(hx(case["request_hex"]), 0)
        assert out.hex() == case["answer_hex"], case["name"]


@pytest.mark.parametrize("case", REFUSALS["cases"], ids=lambda c: c["name"])
def test_refusals_as_the_host_reads_them(case):
    res = m.Result.unpack(hx(case["answer_hex"]))
    want = case["answer"]
    if want == "completed success":
        assert res.succeeded and res.payload == b""                      # gpio set answers no fixed part (fixture §1);
        return                                                          # the unknown TLV is ignored without a trace
    assert want in ("malformed", "unsupported"), f"a new kind of answer in refusals.json: {want}"
    assert res.resolution == m.REJECTED and m.REJECT_NAMES[res.detail].split()[0] == want
    err = h.rejection(res)
    if want == "unsupported":
        assert isinstance(err, h.Unsupported) and (err.tag is None) == (res.payload[:1] == b"\x00")


# ---- session scenarios (sessions.json: core §5.2, §6, §9) -----------------------------------------------------------

SESSIONS = load("sessions.json")
EXAMPLE_BOOT_ID = 0x12345678                                            # confirm.json's example probe (sessions.json about)


@pytest.mark.parametrize("scenario", SESSIONS["scenarios"], ids=lambda c: c["name"])
def test_session_scenarios_on_the_virtual_bench_step_by_step(scenario):
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


# ---- per-op vectors (ops.json): the virtual bench in each case's state, and the client's side --------------------------------

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
    """The virtual bench in the state the case names (`state`), with every fn the case lists and a wire where a connection
    is wanted. -> the endpoint."""
    fns = dict(case["fns"])
    names = set(fns.values())
    if names & {"oep.target.riscv-dm", "oep.target.console", "oep.probe.config"} and "oep.wire.rvswd" not in names:
        # a wire under the case's fns: the connection there is number 1 (core §9)
        fns["4" if "4" not in fns else "14"] = "oep.wire.rvswd"
    if "oep.probe.config" in names:
        fns.setdefault("7" if "7" not in fns else "17", "oep.target.console")
    if names & {"oep.fixture.logic", "oep.fixture.analog"} and "oep.probe.plan" not in names:
        fns["3"] = "oep.probe.plan"                                    # the capture vectors' plans (roles 0 and 1)
    ops = {}
    if "dmi, halt, resume" in case["state"]:
        ops[int(next(k for k, v in case["fns"].items() if v == "oep.target.riscv-dm"))] = {1, 2, 3}
    clock = type("Settable", (), {"t": 0, "__call__": lambda self: self.t})()
    wifi_max = 4 if "items has wifi, wifi_max 4" in case["state"] else 0
    ep = endpoint.Endpoint(vector_probe(fns, ops=ops, wifi_max=wifi_max), clock, boot_id=EXAMPLE_BOOT_ID)
    wire = next((int(k) for k, v in fns.items() if v == "oep.wire.rvswd"), None)
    name, state = case["name"], case["state"]
    plan = ep.fns.get(virtual_bench.PLAN)
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
        if "writing a0 fails" in state:
            tg = ep._target(wire, (1, 2))
            tg.halted, tg.fail_regs = True, {0x100A}                    # the hart halted; a0's write gets no answer
    elif name.startswith("console marks") and "marks 5 to 8 kept" in state:
        host_mark = reg.COMMON.enum["mark_kind"]["host"]               # marks 1 to 4 pushed out, 9 the next
        st = stream_two(ep, data=bytes(80), marks=[(k, 10 * k, host_mark, 1_000_000 * k, k) for k in range(5, 9)])
        st.serial = 9
        ep.probe.max_frame = 64                                        # 2 marks an answer
    elif name.startswith("console marks"):
        stream_two(ep, marks=[(0, 0, reg.COMMON.enum["mark_kind"]["attach"], 1_000_000, 0)])
    elif name.startswith("console streams"):
        stream_two(ep)
    elif name.startswith("console read"):
        stream_two(ep, data=b"hello")
    elif wifi_max:
        # the probe's hash as the case names it (its own, probe.config §2): 0x5A5A0001 with settings, 0x5A5A0002 without
        ep.hash_fn = lambda config: 0x5A5A0001 if config else 0x5A5A0002
        if "the probe's hash for the new settings is 0x5A5A0003" in state:
            ep.hash_fn = lambda config: 0x5A5A0003
        if "session S" in state:
            held(ep)
        if "settings: the wifi entry" in state or "settings as above" in state or "connected through" in state:
            ep.load_config([WIFI_ENTRY], saved=False)
        if "connected through entry 0 at -52 dBm, address 192.168.1.23" in state:
            ep.wifi_set_air({b"lab": b"password1"})
            ep.wifi_join_ms, ep.wifi_rssi, ep.wifi_ip = 0, -52, "192.168.1.23"
    elif name.startswith("probe.config state"):
        clock.t = 1                                                    # the slot's last try at 1 ms
        ep._target(wire, (1, 2)).target_id = 0x00203500
        slot = struct.pack("<BHHHBIIBBB", 0, wire, 1, 2, 1, 0, 0, 0, 2, 1) + b"s"   # at boot, dmseq, name "s"
        ep.load_config([m.tlv(0x04, slot), m.tlv(0x05, struct.pack("<BBH", 0, 1, 0))], saved=False)   # port 0: slot 0
    elif name.startswith("probe.config"):
        held(ep)
    elif name.startswith("capture-group"):
        held(ep)                                                       # fn 9 (logic) and fn 13 (analog), roles 0 and 1
        plan_apply = b"".join(m.tlv(0x10, struct.pack("<HBH", f, r, ch), critical=True)
                              for f, r, ch in ((9, 0, 0), (9, 1, 1), (13, 0, 2)))
        req(ep, 2, plan, PLAN_APPLY, plan_apply)
        one_shot = lambda rate: (m.tlv(0x40, bytes([1])) + m.tlv(0x42, struct.pack("<I", rate))   # noqa: E731
                                 + m.tlv(0x43, struct.pack("<I", 100)))
        req(ep, 3, 9, 0x01, one_shot(1_000_000))
        req(ep, 4, 13, 0x01, one_shot(10_000))
        req(ep, 5, 12, 0x01, struct.pack("<BHH", 2, 9, 13))            # bind fn 9 then fn 13, no trigger_track
        ep.groups[12].generation, ep.captures[9].generation, ep.captures[13].generation = 4, 3, 1
        clock.t = 7                                                    # acquisition starts at 7 ms
    elif name.startswith("logic"):
        logic = int(next(k for k, v in fns.items() if v == "oep.fixture.logic"))
        if "session S" in state:
            held(ep)
        if "fn 9 subscribed" in state:
            req(ep, 2, logic, m.OP_SUBSCRIBE, struct.pack("<HI", 0, 0))
        if "roles 0 and 1 in fn 9's plan" in state or "as above" in state:
            if "session S" not in state:
                held(ep)
            req(ep, 2, plan, PLAN_APPLY, m.tlv(0x10, struct.pack("<HBH", logic, 0, 0), critical=True)
                + m.tlv(0x10, struct.pack("<HBH", logic, 1, 1), critical=True))
        cap = ep.captures[logic]
        cap.generation, cap.state, cap.serial_done = 1, virtual_bench_capture.STATE["done"], 1
        cap.segs = [virtual_bench_capture.Segment(0, 0, 1000, 5_000_000, 50, virtual_bench_capture.NONE, 0, 1)]
    return ep


def _uncritical(frame: bytes) -> bytes:
    """A request with bit 7 of its TLV tags cleared: this client sends mode and rate critical (its own choice,
    capture §3.3), the vectors send them plain - the same request otherwise."""
    r = m.Request.unpack(frame)
    body = b"".join(m.tlv(t & 0x7F, v) for t, v in m.split_tlvs(r.payload))
    return m.Request(r.corr, r.fn, r.op, body, r.session).pack()


# The wifi vectors' entry (probe.config §1.4): index 0, ssid "lab", passphrase "password1"
WIFI_ENTRY = m.tlv(0x08, bytes([0, 3]) + b"lab" + bytes([9]) + b"password1")


# Vectors the spec's text corrects (core §0 rule 4): name -> what the text says; the virtual bench answers as the text does.
# Empty now: "rvswd scan: count > 0 with skip" (debug §1, b9b30ad) was corrected in oep-spec 85170c4.
TEXT_OVER_VECTOR: dict[str, str] = {}


NOT_IN_THE_VIRTUAL_BENCH = {"oep.target.arm-adi"}                       # the client's side is checked below


@pytest.mark.parametrize("case", OPS, ids=lambda c: c["name"])
def test_ops_vectors_from_the_virtual_bench_byte_for_byte(case):
    if NOT_IN_THE_VIRTUAL_BENCH & set(case["fns"].values()):
        pytest.skip("the virtual bench has no " + ", ".join(sorted(NOT_IN_THE_VIRTUAL_BENCH & set(case["fns"].values()))))
    ep = setup_case(case)
    out = ep.handle(hx(case["request_hex"]), 0)
    if case["name"] in TEXT_OVER_VECTOR:
        res = m.Result.unpack(out)
        assert res.succeeded and res.payload[0] == 1, TEXT_OVER_VECTOR[case["name"]]   # tried 1: skip not looked at
        return
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


def is_wifi_vector(case) -> bool:
    """A wifi vector (probe.config §1.4, §3.3): its probe declares the wifi item - routed by the case's state, not its
    name (some names say no "wifi": a pass_len 0xFF for a missing entry, a 7-byte passphrase)."""
    return case["name"].startswith("probe.config") and "items has wifi" in case["state"]


REFUSED_BEFORE_SENDING = "the client refuses this form before sending (checked, nothing sent)"


def _wifi_on_client(case, name):
    """The wifi vectors (probe.config §1.4, §3.3) as this client sends and reads them; REFUSED_BEFORE_SENDING for the
    form it never builds (a 7-byte passphrase: ValueError before sending, and the vector's answer is malformed). 0xFF
    for a missing entry and an index past wifi_max are the probe's to refuse - the client sends them as asked."""
    from oep_client import config as cfg
    a = m.Result.unpack(hx(case["answer_hex"]))
    hst, sent = client(case, S if "session S" in case["state"] else None)
    p = cfg.ProbeConfig(hst, fn=8)
    if name.startswith("probe.config set: wifi entry 0"):
        assert p.set([cfg.Wifi(index=0, ssid="lab", passphrase="password1")]) == 0x5A5A0001
    elif "the longest wifi item" in name:
        # 32-byte ssid and 64 hex digits: a 112-byte request (wifi_min_max_frame), sent at a max_frame of 112
        hst.limits["max_frame"] = reg.LIMITS["wifi_min_max_frame"]
        w = cfg.Wifi(index=0, ssid="s" * cfg.SSID_MAX, passphrase="0123456789abcdef" * 4)
        assert p.set([w]) == 0x5A5A0003
        assert len(sent[0]) == cfg.WIFI_MIN_MAX_FRAME
    elif name.startswith("probe.config get"):
        hash_, items = p.get()
        (w,) = [cfg.decode(t, v) for t, v in items]
        assert hash_ == 0x5A5A0001 and w.passphrase is cfg.KEEP and w.ssid == "lab" and "password" not in repr(w)
    elif "sent back" in name:
        assert p.set([cfg.Wifi(index=0, ssid="lab", passphrase=cfg.KEEP)]) == 0x5A5A0001
    elif "no entry" in name:
        with pytest.raises(h.Rejected) as e:
            p.set([cfg.Wifi(index=1, ssid="field", passphrase=cfg.KEEP)])
        assert e.value.result.detail == a.detail == m.MALFORMED
    elif "7-byte" in name:
        with pytest.raises(ValueError) as e:
            cfg.Wifi(index=1, ssid="field", passphrase="secret7").value()
        assert "secret7" not in str(e.value) and a.detail == m.MALFORMED
        return REFUSED_BEFORE_SENDING
    elif "at wifi_max" in name:
        with pytest.raises(h.Rejected) as e:
            p.set([cfg.Wifi(index=4, ssid="field")])
        assert e.value.result.detail == m.UNSUPPORTED
    elif name.startswith("probe.config state"):
        st = p.state()
        assert (st.slots, st.binds) == ([], [])
        assert st.wifi == cfg.WifiState("connected", 0, "none", -52, "192.168.1.23")
    elif name.startswith("probe.config unset"):
        assert p.unset([("wifi", 0)]) == 0x5A5A0002
    else:
        raise AssertionError(f"a wifi vector this test does not know: {name}")
    return sent


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
        assert data == bytes(k & 0xFF for k in range(min(n, core.link_size(1024))))   # max_frame - 7 (§2)
        return sent
    if name.startswith("link sink") or name.startswith("link port_speed"):
        hst, sent = client(case, S if "port_speed" in name else None)
        if "port_speed" in name:
            with pytest.raises(h.Rejected) as e:
                hst.call(1, core.LINK_PORT_SPEED, struct.pack("<IBH", 921600, 0, 2000))   # baud step verify_ms (§3)
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
        if "count 0" in name or "skip" in name:
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
        elif "preparation fails" in name:                               # stopped 3: not run, still halted (§4.4)
            with pytest.raises(rv.TargetError) as e:
                dm.run(0x20000000, [(0x100A, 7)], timeout_ms=100, outs=())
            run = rv.RiscvDm.run_result(e.value.result)
            assert run.not_run and not run.not_halted and e.value.status == reg.STATUS["line"]
        elif "n = 0" in name:
            assert dm.dmi([]) == (0, [])
        elif "step on a running hart" in name:                         # debug §4.2: moved and the dpcs not read
            with pytest.raises(rv.StepError) as e:
                dm.step()
            assert e.value.status == reg.STATUS["state"] and (e.value.dpc_before, e.value.dpc_after) == (None, None)
            assert not e.value.step_left
            return sent[-1:]                                            # restore_data writes nothing: DATA0 untouched
        else:
            assert dm.dmi([dm.step_read(0x11)]) == (1, [0x00400382])
        return sent
    if name.startswith("arm-adi transfer"):
        from oep_client import arm
        hst, sent = client(case, S)
        adi = arm.ArmAdi(hst, 1)
        assert adi.transfer(b"") == [] and adi.last_ack == 0           # n = 0: success, done 0, ack 0 (debug §6)
        return sent
    if name.startswith("console"):
        hst, sent = client(case)
        c = con.Console(hst)
        c.stream = 2
        if name.startswith("console marks") and "marks 5 to 8 kept" in case["state"]:
            frm = struct.unpack_from("<I", m.Request.unpack(hx(case["request_hex"])).payload, 2)[0]
            marks, more = c.marks_page(frm)
            want = {3: ([5, 6], True), 5: ([5, 6], True), 7: ([7, 8], False), 9: ([], False)}[frm]   # common §1.3
            assert ([k.serial for k in marks], more) == want
            assert all((k.position, k.time_ns, k.detail) == (10 * k.serial, 1_000_000 * k.serial, k.serial) for k in marks)
        elif name.startswith("console marks"):
            marks, more = c.marks_page(0)
            assert not more and [(k.serial, k.position, k.kind, k.time_ns, k.detail) for k in marks] == \
                [(0, 0, 3, 1_000_000, 0)]
        elif name.startswith("console streams") and "beyond the count" in name:
            rd = m.Reader(hst.request(7, con.Console.STREAMS, struct.pack("<H", 1), locked=False).payload)
            assert rd.take("BB") == (0, 0)                              # first(u16) past the count: the last page
        elif name.startswith("console streams"):
            assert c.streams() == [con.StreamInfo(2, 1, 2, 1, 0)]
        elif "from 4" in name:
            return None                                                 # the client never sends from 4
        else:
            assert tuple(c.read(c.FROM_POSITION, 5, 64)) == (5, False, False, b"")
        return sent
    if is_wifi_vector(case):
        return _wifi_on_client(case, name)
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
        assert (slot.slot, slot.state, slot.connection, slot.last_try_at_ns) == (0, "connected", 1, 1_000_000)
        assert (bind.port, bind.flow) == (0, "streaming")
        return sent
    if name.startswith("logic segments"):
        hst, sent = client(case)
        frm = struct.unpack_from("<I", m.Request.unpack(hx(case["request_hex"])).payload)[0]
        segs, more = cap.LogicCapture(hst, 9).segments_page(frm)
        if "from_serial = serial_done" in name:
            assert (segs, more) == ([], False)                          # common §1.3 paging 2
            return sent
        (seg,) = segs
        assert not more and (seg.serial, seg.position, seg.samples, seg.start_ns, seg.generation) == \
            (0, 0, 1000, 5_000_000, 1)
        return sent
    if name.startswith("logic configure without rate"):
        return None                                                     # the client always sends mode and rate
    if name.startswith("logic configure") or name.startswith("logic query"):
        hst, sent = client(case, S if "session S" in case["state"] else None)
        lc = cap.LogicCapture(hst, 9)
        if "streaming with samples" in name:
            with pytest.raises(ValueError, match="streaming takes no samples"):
                lc.configure(rate=1_000_000, mode=cap.STREAMING, samples=1000, query=True)
            assert sent == []
            return REFUSED_BEFORE_SENDING
        c = lc.configure(rate=20_000_000, samples=200_000)
        assert (c.rate, c.width, c.positions, c.samples, c.segments, c.blocking_ms) == (20_000_000, 2, [0, 1], 200_000, 1, 0)
        return [_uncritical(sent[0])]
    if name.startswith("capture-group start"):
        hst, sent = client(case, S)
        logic, analog = cap.LogicCapture(hst, 9), cap.AnalogCapture(hst, 13)
        grp = cap.CaptureGroup(hst, fn=12)
        assert grp.start([logic, analog]) == (0, 7_000_000)
        assert (grp.generation, grp.generations, logic.generation, analog.generation) == (5, {9: 4, 13: 2}, 4, 2)
        return sent
    return None


@pytest.mark.parametrize("case", OPS, ids=lambda c: c["name"])
def test_ops_vectors_as_the_client_sends_and_reads_them(case):
    """Where this client has the op, the request it builds is the case's byte for byte and its reading of the answer
    gives the case's values (a refusal raises the class that names it, unknown_operation as plain Rejected)."""
    sent = _on_client(case)
    if sent is None:
        pytest.skip("not a request this client makes")
    if sent is REFUSED_BEFORE_SENDING:
        return
    assert sent[0].hex() == case["request_hex"]


def test_every_wifi_vector_is_routed_to_the_wifi_checks():
    """The 9 wifi vectors (corr 0x74-0x7C) all reach _wifi_on_client, none skipped."""
    wifi = [c for c in OPS if is_wifi_vector(c)]
    assert len(wifi) == 9
    assert sorted(m.Request.unpack(hx(c["request_hex"])).corr for c in wifi) == list(range(0x74, 0x7D))
    assert all(_on_client(c) is not None for c in wifi)


# ---- the ops encoding (ops_encoding.json, core §7.4) -----------------------------------------------------------------

@pytest.mark.parametrize("case", load("ops_encoding.json")["cases"], ids=lambda c: c["name"])
def test_ops_encoding_as_the_host_reads_it(case):
    """A valid value decodes to the case's set (one set may have several values; pack_ops gives one that decodes to the
    same set); an invalid one is refused (check_ops says why, unpack_ops raises) - the host does not use that fn."""
    value = hx(case["value_hex"])
    if case["valid"]:
        assert catalog.check_ops(value) == ""
        assert sorted(catalog.unpack_ops(value)) == case["ops"]
        assert sorted(catalog.unpack_ops(catalog.pack_ops(case["ops"]))) == case["ops"]
        assert catalog.decode_description(catalog.tlv(catalog.OPS, value)).ops == set(case["ops"])
    else:
        assert catalog.check_ops(value)
        with pytest.raises(catalog.InvalidOps):
            catalog.unpack_ops(value)
        d = catalog.decode_description(catalog.tlv(catalog.OPS, value))
        assert d.ops is None and d.ops_invalid


def test_every_ops_in_the_virtual_bench_and_the_vectors_is_valid():
    """Every describe the virtual bench's profiles give, and the discovery vectors' ops, keep core §7.4's form."""
    for make in virtual_bench.PROFILES.values():
        ep = endpoint.Endpoint(make(), Clock())
        for fn in ep.names:
            for t in ep._declarations(fn):
                if t[0] == catalog.OPS:
                    assert catalog.check_ops(t[3:]) == "", (make.__name__, fn)
    for case in DISCOVERY["exchanges"]:
        if "ops" in case["answer"]:
            assert catalog.pack_ops(case["answer"]["ops"]) in hx(case["answer_hex"])


def test_a_wifi_set_past_the_probes_max_frame_is_refused_before_sending():
    """The longest wifi set is 112 bytes (probe.config §1.4); at a max_frame of 64 the client says so and sends nothing
    (the passphrase never in the message)."""
    from oep_client import config as cfg
    case = next(c for c in OPS if "the longest wifi item" in c["name"])
    hst, sent = client(case, S)
    hst.limits["max_frame"] = reg.MIN_MAX_FRAME
    with pytest.raises(ValueError) as e:
        cfg.ProbeConfig(hst, fn=8).set([cfg.Wifi(index=0, ssid="s" * cfg.SSID_MAX, passphrase="0123456789abcdef" * 4)])
    assert sent == [] and "max_frame 64" in str(e.value) and "112" in str(e.value) and "0123" not in str(e.value)


# ---- notification frames (ops.json events): capture events carry their generation (capture §3.4, §4.2) -------------

EVENTS = load("ops.json")["events"]


@pytest.mark.parametrize("case", EVENTS, ids=lambda c: c["name"])
def test_events_as_the_client_reads_them(case):
    """Each event read by the client: its kind and generation; taken as the current one only when its generation is
    the current one (the logic's start answered generation 4, the group is at 5)."""
    import collections
    from oep_client import capture as cap
    frame = hx(case["event_hex"])
    fn = struct.unpack_from("<H", frame, 1)[0]
    group = case["fns"][str(fn)] == "oep.fixture.capture-group"
    e = cap.unpack_event(frame, group=group)
    assert (e.fn, e.generation) == (fn, case["generation"])
    hst, _ = client({"request_hex": m.Request(1, 0, 1, b"").pack().hex(), "answer_hex": "", "fns": case["fns"]})
    track = cap.CaptureGroup(hst, fn=fn) if group else cap.LogicCapture(hst, fn)
    track.generation = 5 if group else 4
    link = type("Link", (), {"events": collections.deque([frame])})()
    current = track.events(link)
    if "previous generation" in case["name"]:
        assert e.kind == "stopped" and e.reason_name == "host" and current == [] and track.stale_events == 1
    elif e.kind == "triggered" and group:
        assert (e.trigger_fn, e.trigger_ns) == (9, 7_050_000) and current == [e]
    elif e.kind == "triggered":
        assert (e.serial, e.trigger_index, e.trigger_ns) == (0, 1000, 7_050_000) and current == [e]
    else:
        assert e.kind == "stopped" and e.reason_name == "complete" and e.error == 0 and current == [e]


def test_every_event_vector_is_read():
    assert len(EVENTS) == 4 and {c["generation"] for c in EVENTS} == {3, 4, 5}
