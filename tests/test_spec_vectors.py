"""oep-spec's current shared vectors (tests/vectors/*.json and src/oep_client/registry.py, copied by
tools/sync_registry.sh from the commit in tests/vectors/SPEC_COMMIT, never edited) against this client and its virtual
bench. The client follows the spec stage by stage (docs/conformance-spec-compatibility.ja.md): the core first, then the
contracts common to every extension, then each standard interface. Each stage moves its vectors here from
test_vectors.py, which still reads the frozen b80555a copy in tests/vectors_legacy for what has not moved yet."""

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent / "vectors"
SPEC_COMMIT = (HERE / "SPEC_COMMIT").read_text().strip()


def load(name: str) -> dict:
    return json.loads((HERE / name).read_text(encoding="utf-8"))


def test_copied_inputs_have_recorded_provenance():
    """Every vendored input is pinned, even in an isolated checkout without SPEC."""
    assert re.fullmatch(r'[0-9a-f]{40}', SPEC_COMMIT)
    root = HERE.parents[1]
    manifest = dict(line.split('  ', 1)[::-1] for line in (HERE / 'SPEC_SHA256').read_text().splitlines())
    expected = {str(p.relative_to(root)) for p in HERE.glob('*.json')} | {'src/oep_client/registry.py'}
    assert set(manifest) == expected
    for name, digest in manifest.items():
        assert hashlib.sha256((root / name).read_bytes()).hexdigest() == digest, name


def test_the_copy_is_the_specs():
    """Compare against the pinned commit in an explicitly supplied checkout, never HEAD."""
    spec = os.environ.get('OEP_SPEC_DIR')
    if not spec:
        pytest.skip('OEP_SPEC_DIR not set: optional upstream snapshot verification')
    paths = subprocess.check_output(['git', '-C', spec, 'ls-tree', '--name-only',
                                     SPEC_COMMIT + ':tests/vectors'], text=True).splitlines()
    mine = {p.name: p.read_bytes() for p in HERE.glob('*.json')}
    theirs = {name: subprocess.check_output(['git', '-C', spec, 'show',
                                            SPEC_COMMIT + ':tests/vectors/' + name])
              for name in paths if name.endswith('.json')}
    assert mine == theirs, 'vendored vectors differ from recorded SPEC_COMMIT'
    registry = subprocess.check_output(['git', '-C', spec, 'show',
                                       SPEC_COMMIT + ':generated/oep-v1/oep_v1_registry.py'])
    assert (HERE.parents[1] / 'src/oep_client/registry.py').read_bytes() == registry


# ---- COBS and serial frames (transports §1) --------------------------------------------------------------------------


# ---- the core (stage 1) ------------------------------------------------------------------------------------------
import struct

from oep_client import catalog, cobs, endpoint, host as h, message as m, registry as reg, virtual_bench


def hx(s: str) -> bytes:
    return bytes.fromhex(s)


class Clock:
    def __call__(self):
        return 0


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


@pytest.mark.parametrize("case", [c for c in load("checks.json")["cases"] if c["algorithm"] != "crc8-dmseq"],
                         ids=lambda c: c["name"])
def test_crc_check_values(case):
    assert case["algorithm"] == "crc16-ccitt-false" and cobs.crc16(hx(case["input_hex"])) == case["crc"]


def test_only_the_dmseq_crc8_is_not_this_clients():
    """The dmseq CRC-8 belongs to the probe and the target (target-console-dmseq): this client reads console bytes."""
    assert {c["algorithm"] for c in load("checks.json")["cases"]} == {"crc16-ccitt-false", "crc8-dmseq"}


@pytest.mark.parametrize("case", load("headers.json")["requests"], ids=lambda c: c["name"])
def test_request_headers(case):
    req = m.Request(case["corr"], case["fn"], case["op"], hx(case["payload_hex"]), case["session_id"])
    assert req.pack().hex() == case["message_hex"]
    assert m.Request.unpack(hx(case["message_hex"])) == req and hx(case["message_hex"])[0] == case["role"]


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


@pytest.mark.parametrize("case", load("ops_encoding.json")["cases"], ids=lambda c: c["name"])
def test_ops_encoding_as_the_host_reads_it(case):
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


def core_probe(max_frame: int = 1024) -> virtual_bench.VirtualProbe:
    """The vectors' example probe: fn 0 alone, one UART bridge (index 0)."""
    core = virtual_bench._core("1.0.0", "vectors", "0123456789ab", 16, {},
                               virtual_bench._transports([(virtual_bench.TRANSPORT["uart_bridge"], 0xFF)]))
    return virtual_bench.VirtualProbe("vectors", max_frame, [core])


CONFIRM = load("confirm.json")["exchanges"]


@pytest.mark.parametrize("case", CONFIRM, ids=lambda c: c["name"])
def test_confirm_request_bytes(case):
    q = case["request"]
    req = m.Request(q["corr"], 0, m.OP_CONFIRM, m.CONFIRM_REQUEST + bytes([q["min_rev"], q["max_rev"]]))
    assert req.pack().hex() == case["request_hex"]
    if "request_serial_frame_hex" in case:
        assert cobs.frame(req.pack()).hex() == case["request_serial_frame_hex"]


@pytest.mark.parametrize("case", CONFIRM, ids=lambda c: c["name"])
def test_confirm_from_the_virtual_bench_and_read_by_the_host(case):
    a = case["answer"]
    ep = endpoint.Endpoint(core_probe(a.get("max_frame", 1024)), Clock(), boot_id=a.get("boot_id", 0),
                           window=a.get("window", 4096), max_inflight=a.get("max_inflight", 4))
    assert ep.handle(hx(case["request_hex"]), a.get("transport", 0)).hex() == case["answer_hex"]
    hst = h.Host(lambda b: hx(case["answer_hex"]))
    hst._corr = case["request"]["corr"] - 1
    q = case["request"]
    if "reason" in a:
        with pytest.raises(h.Unsupported) as e:
            hst.confirm(q["min_rev"], q["max_rev"])
        assert e.value.tag is None and list(e.value.supported) == a["supported"]
        return
    limits = hst.confirm(q["min_rev"], q["max_rev"])
    keys = ("revision", "max_frame", "window", "max_inflight", "boot_id", "transport")
    assert {k: limits[k] for k in keys} == {k: a[k] for k in keys}


DISCOVERY = load("discovery.json")


def smallest_probe() -> endpoint.Endpoint:
    """discovery.json's example probe: no interface; fn 0's describe is ops (the core's eight), unit_id "a1b2c3d4",
    one UART bridge, max_op_ms 1000, in that order."""
    t = reg.CORE.tlv["describe"]
    core = virtual_bench.Offered(0, 0, virtual_bench.CORE_NAME, (
        catalog.ops_tlv(reg.CORE.op[k] for k in virtual_bench.CORE_REQUIRED),
        catalog.text(t["unit_id"], "a1b2c3d4"),
        catalog.tlv(t["transport"], bytes([0, virtual_bench.TRANSPORT["uart_bridge"], 0xFF])),
        catalog.u32(t["max_op_ms"], 1000)))
    return endpoint.Endpoint(virtual_bench.VirtualProbe("smallest", 64, [core]), Clock())


def test_discovery_from_the_virtual_bench_byte_for_byte():
    ep = smallest_probe()
    for case in DISCOVERY["exchanges"] + DISCOVERY["refusals"]:
        out = ep.handle(hx(case["request_hex"]), 0)
        assert out.hex() == case["answer_hex"], case["name"]
        if "answer_serial_frame_hex" in case:
            assert cobs.frame(out).hex() == case["answer_serial_frame_hex"], case["name"]


@pytest.mark.parametrize("case", DISCOVERY["exchanges"], ids=lambda c: c["name"])
def test_discovery_as_the_host_sends_and_reads_it(case):
    q, a = case["request"], case["answer"]
    if "fn" not in q:
        payload, op = catalog.pack_list_request(q["first"]), m.OP_LIST
    else:
        payload, op = catalog.pack_describe_request(q["fn"], q["first"]), m.OP_DESCRIBE
    assert m.Request(q["corr"], 0, op, payload).pack().hex() == case["request_hex"]
    res = m.Result.unpack(hx(case["answer_hex"]))
    assert (res.corr, res.succeeded) == (a["corr"], True)
    if "entries" in a:
        total, entries = catalog.unpack_list_result(res.payload)
        assert total == a["total"] and [(e.fn, e.instance, e.revision, e.name) for e in entries] == [
            (e["fn"], e["instance"], e["revision"], e["name"]) for e in a["entries"]]
        return
    assert res.payload[0] == a["more"]
    tlvs = dict(m.split_tlvs(res.payload[1:]))
    if "unit_id" not in a:
        assert tlvs == {}
        return
    assert tlvs[reg.CORE.tlv["describe"]["unit_id"]].decode() == a["unit_id"]
    assert sorted(catalog.unpack_ops(tlvs[catalog.OPS])) == a["ops"]
    from oep_client import core
    for read, want in ((lambda x: core.ops(x, 0), set(a["ops"])),
                       (core.transports, [(x["index"], x["kind"], x["interface"]) for x in a["transports"]]),
                       (core.max_op_ms, a["max_op_ms"])):
        hst = h.Host(lambda b: hx(case["answer_hex"]))
        hst._corr = a["corr"] - 1
        assert read(hst) == want


@pytest.mark.parametrize("case", DISCOVERY["refusals"], ids=lambda c: c["name"])
def test_discovery_refusals_as_the_host_reads_them(case):
    res = m.Result.unpack(hx(case["answer_hex"]))
    assert res.resolution == m.REJECTED and res.payload == b"" and res.detail == reg.REJECT_REASONS[case["answer"]]


CORE_REFUSALS = [c for c in load("refusals.json")["cases"] if not c["fns"]]


@pytest.mark.parametrize("case", CORE_REFUSALS, ids=lambda c: c["name"])
def test_core_refusals_from_the_virtual_bench_and_as_the_host_reads_them(case):
    """refusals.json's cases that need no interface (the rest come with their interface's stage)."""
    ep = endpoint.Endpoint(core_probe(), Clock())
    assert ep.handle(hx(case["request_hex"]), 0).hex() == case["answer_hex"]
    res = m.Result.unpack(hx(case["answer_hex"]))
    assert res.resolution == m.REJECTED and m.REJECT_NAMES[res.detail].split()[0] == case["answer"]


SESSIONS = load("sessions.json")
EXAMPLE_BOOT_ID = 0x12345678


@pytest.mark.parametrize("scenario", SESSIONS["scenarios"], ids=lambda c: c["name"])
def test_session_scenarios_on_the_virtual_bench_step_by_step(scenario):
    """From a fresh probe, every step on one transport; no time passes on the timers' clock, and the probe's clock
    reads 2 s and then 1 ms more at every read (sessions.json's example values)."""
    reads = iter(range(2_000_000_000, 3_000_000_000, 1_000_000))
    ep = endpoint.Endpoint(core_probe(), Clock(), boot_id=EXAMPLE_BOOT_ID, now_ns=lambda: next(reads),
                           remember_count=4)                                  # sessions.json: exactly four records
    for step in scenario["steps"]:
        out = ep.handle(hx(step["request_hex"]), 0)
        assert out is not None and out.hex() == step["answer_hex"], step["note"]


@pytest.mark.parametrize("scenario", SESSIONS["scenarios"], ids=lambda c: c["name"])
def test_session_scenarios_as_the_host_reads_them(scenario):
    """The host's reading of each answer it can be given, and the bytes of the requests it sends itself."""
    for step in scenario["steps"]:
        req = m.Request.unpack(hx(step["request_hex"]))
        res = m.Result.unpack(hx(step["answer_hex"]))
        if req.session == 0 and req.op != m.OP_CLOCK and req.op != m.OP_LOCK_STATE:
            continue                                                   # a request this host never makes
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
                continue                                               # this host sends clock with session_id 0
            reading = hst.clock()
            assert (reading.boot_id, reading.uptime_ns) == struct.unpack_from("<IQ", res.payload)
            assert sent[0] == hx(step["request_hex"]), step["note"]
            continue
        else:
            if len(req.payload) or req.op not in (m.OP_KEEPALIVE, m.OP_END):
                continue                                               # an altered copy: never sent by this host
            hst.session = req.session
            call = {m.OP_KEEPALIVE: hst.keepalive, m.OP_END: hst.end}[req.op]
        if res.resolution == m.REJECTED:
            with pytest.raises(h.Rejected) as e:
                call()
            assert e.value.result == res and isinstance(e.value, h._REJECTS.get(res.detail, h.Rejected))
            if res.detail == m.LOCKED:
                assert e.value.remaining_ms == m.Reader(res.payload).u32()
        else:
            got = call()
            if req.op == m.OP_OPEN:
                assert (got.lease_ms, got.boot_id) == struct.unpack("<II", res.payload[:8])
            if req.op == m.OP_LOCK_STATE:
                assert got[:2] == (bool(res.payload[0]), struct.unpack_from("<I", res.payload, 1)[0])
        assert sent and sent[0] == hx(step["request_hex"]), step["note"]


EXTENSIONS = load("core_extensions.json")["cases"]


@pytest.mark.parametrize("case", EXTENSIONS, ids=lambda c: c["name"])
def test_an_unknown_answer_tlv_is_skipped_by_the_host(case):
    """core §2.3: the host reads the fixed part and skips the TLVs it does not know, on every resolution."""
    res = m.Result.unpack(hx(case["answer_hex"]))
    fixed = case.get("fixed_answer_bytes", 0)
    if res.resolution == m.REJECTED:
        assert isinstance(h.rejection(res), h.Rejected)
    tail = m.split_tlvs(res.payload[fixed:])
    assert tail and all(t & 0x7F == 0x7F or t for t, _ in tail)


# ---- per-op vectors (ops.json) of the interfaces that follow the current spec --------------------------------------
from oep_client import config, core, fixture, virtual_bench_capture
from test_vectors import vector_probe                                   # the vectors' fn numbers on the virtual bench

S = 0x11223344                                                          # ops.json about: the lock holder's id
FOLLOWED = {"oep.probe.plan", "oep.probe.restart", "oep.probe.link"}                                           # grows with each stage
OPS = load("ops.json")["cases"]


def followed(case) -> bool:
    """The case's request goes to an interface that follows the current spec (its state may involve others)."""
    return case["fns"].get(str(m.Request.unpack(hx(case["request_hex"])).fn)) in FOLLOWED


class Fixed:
    @staticmethod
    def randrange(a, b):
        return S


def bench_for(case):
    """The virtual bench with the case's fns, and a host holding session S on it for setting the case's state up
    through the client's own modules. -> (endpoint, host, clock)."""
    clock = type("Settable", (), {"t": 0, "__call__": lambda self: self.t})()
    ops = {}
    if "ops of fn 1: source and sink only" in case["state"]:
        ops[1] = {reg.PROBE_LINK.op["source"], reg.PROBE_LINK.op["sink"]}
    probe = vector_probe(case["fns"], ops=ops)
    if "port_s" in case["name"] and "not offered" not in case["name"]:   # a UART bridge probe with port_speed
        probe.offered = [virtual_bench._link(o.fn, port_speed=True) if o.name == virtual_bench.LINK else o
                         for o in probe.offered]
    ep = endpoint.Endpoint(probe, clock, boot_id=EXAMPLE_BOOT_ID)
    hst = h.Host(lambda b: ep.handle(b, 0))
    hst.rng = Fixed()
    return ep, hst, clock


def fn_of(case, name):
    return int(next(k for k, v in case["fns"].items() if v == name))


def plan_state(case, ep, hst):
    """The state plan's cases name, made through the client."""
    name, state = case["name"], case["state"]
    if state != "—":
        hst.open(3000)
    if "fn 2 has the plan above" in state or "fn2 role1 channel3 is high" in state:
        core.plan_apply(hst, [(2, 1, 3)])
    if "fn2 role1 channel3 is high" in state:
        fixture.Gpio(hst, 2).set([(3, reg.INTERFACES["oep.fixture.gpio"].enum["mode"]["output_high"])])
    if "fn5 RX on channel3 at 9600 baud" in state:
        core.plan_apply(hst, [(5, 1, 3)])
        fixture.FixtureUart(hst, 5).configure(9600)
    if "fn9 state3, role0 channel1" in state:
        core.plan_apply(hst, [(9, 0, 1)])
        ep.captures[9].state = virtual_bench_capture.STATE["capturing"]
    if "channel3 idle high in volatile config" in state:                 # the settings, not a session's
        ep.load_config([config.item(config.Idle(channel=3, mode="output-high"))], saved=False)


def restart_state(case, ep, hst):
    if "holds the lock" in case["state"]:
        hst.open(3000)


def link_state(case, ep, hst):
    """port_speed's phases (oep-if-link §3) on the bench's UART bridge (transport 0): raised to 921600 by the client's
    own requests; a frame not finished yet (`speed_frame_busy`) keeps a return pending in phase 3."""
    state, clock = case["state"], ep.now
    if "max_frame" in state or state == "—" or "source and sink only" in state:
        return
    raise_to = ("921600" in state or "phase3" in state) and "expired try" not in state
    if "session S" in state or "previous owner ended" in state or "expired try" in state:
        hst.open(60000)
    if raise_to:
        lk = reg.PROBE_LINK
        hst.call(1, lk.op["port_speed"], struct.pack("<IBH", 921600, lk.enum["port_speed_step"]["try"], 2000))
        ep.after_answer()
        if "after try verify expiry" not in state:
            hst.call(1, lk.op["port_speed"], struct.pack("<IBH", 921600, lk.enum["port_speed_step"]["commit"], 0))
        ep.speed_frame_busy = "frame" in state or "phase3" in state     # a frame still on the line: the return waits
        clock.t += 3001 if "idle expiry" in state else 2001 if "verify expiry" in state else 0
        if "ended" in state:
            hst.end()
        ep.tick()
    if "expired try, now boot baud" in state:                            # a try whose verify ran out with nothing pending
        lk = reg.PROBE_LINK
        hst.call(1, lk.op["port_speed"], struct.pack("<IBH", 921600, lk.enum["port_speed_step"]["try"], 2000))
        ep.after_answer()
        clock.t += 2001
        ep.tick()


STATES = {"oep.probe.plan": plan_state, "oep.probe.restart": restart_state, "oep.probe.link": link_state}


def make_case(case):
    ep, hst, clock = bench_for(case)
    for name in sorted(set(case["fns"].values()), key=lambda n: n != "oep.probe.plan"):
        if name in STATES:
            STATES[name](case, ep, hst)
            break
    corr = m.Request.unpack(hx(case["request_hex"])).corr
    ep.newest_corr = (corr - 2) % 0xFFFF + 1 if ep.last is not None else None   # the case's request is the next one
    ep.resend.clear()
    return ep, hst


@pytest.mark.parametrize("case", [c for c in OPS if followed(c)], ids=lambda c: c["name"])
def test_ops_vectors_from_the_virtual_bench_byte_for_byte(case):
    ep, _ = make_case(case)
    assert ep.handle(hx(case["request_hex"]), 0).hex() == case["answer_hex"]


def test_plan_keeps_what_its_cases_say_it_keeps():
    """The states the plan vectors say stay (oep-if-plan §2.1): a kept gpio output, a configured UART's baud and buffer,
    a running capture."""
    by = {c["name"]: c for c in OPS}
    ep, _ = make_case(by["plan_apply: GPIO addition preserves an existing output"])
    before = ep.pin_state(3)
    ep.handle(hx(by["plan_apply: GPIO addition preserves an existing output"]["request_hex"]), 0)
    assert ep.pin_state(3) == before and before.startswith("gpio") and (2, 1, 4) in ep.plan
    for name in ("plan_apply: identical configured UART is unchanged", "plan_apply: configured UART channel change is refused"):
        ep, _ = make_case(by[name])
        before = (dict(ep.uart_baud), set(ep.plan))
        ep.handle(hx(by[name]["request_hex"]), 0)
        assert (dict(ep.uart_baud), set(ep.plan)) == before, name
    for name in ("plan_apply: active capture channel change is refused", "plan_apply: active capture identical assignment is unchanged"):
        ep, _ = make_case(by[name])
        ep.handle(hx(by[name]["request_hex"]), 0)
        assert ep.captures[9].state == virtual_bench_capture.STATE["capturing"] and (9, 0, 1) in ep.plan, name


@pytest.mark.parametrize("case", [c for c in OPS if followed(c) and c["name"].startswith("plan_")
                                  and "session_required" not in c["name"] and "n = 2" not in c["name"]],
                         ids=lambda c: c["name"])
def test_plan_vectors_as_the_client_sends_and_reads_them(case):
    """The client's plan_apply / plan_release send the case's bytes and read its answer."""
    r = m.Request.unpack(hx(case["request_hex"]))
    sent = []
    hst = h.Host(lambda b: sent.append(b) or hx(case["answer_hex"]))
    hst._corr, hst.revision, hst.session = r.corr - 1, 1, r.session or None
    hst._fns["oep.probe.plan"] = r.fn
    res = m.Result.unpack(hx(case["answer_hex"]))
    if r.op == core.OP_PLAN_APPLY:
        call = lambda: core.plan_apply(hst, [struct.unpack("<HBH", v) for _, v in m.split_tlvs(r.payload)])  # noqa: E731
    else:
        n = r.payload[0]
        call = lambda: core.plan_release(hst, [struct.unpack_from("<H", r.payload, 1 + 2 * k)[0] for k in range(n)])  # noqa: E731
    if res.resolution == m.REJECTED:
        with pytest.raises(h.Rejected) as e:
            call()
        assert e.value.result.detail == res.detail
    else:
        call()
    assert sent[0] == hx(case["request_hex"])


@pytest.mark.parametrize("case", [c for c in OPS if c["name"].startswith("restart") and c["state"] != "—"],
                         ids=lambda c: c["name"])
def test_restart_vectors_as_the_client_sends_them(case):
    """The client's restart request is the case's bytes (oep-if-restart §1: no fixed part)."""
    r = m.Request.unpack(hx(case["request_hex"]))
    sent = []
    hst = h.Host(lambda b: sent.append(b) or hx(case["answer_hex"]))
    hst._corr, hst.revision, hst.session = r.corr - 1, 1, r.session
    hst._fns["oep.probe.restart"] = r.fn
    try:
        hst.request_restart()
    except h.Rejected as e:
        assert e.result == m.Result.unpack(hx(case["answer_hex"]))
    assert sent[0] == hx(case["request_hex"])


@pytest.mark.parametrize("case", [c for c in OPS if c["name"].startswith(("link source", "link sink", "link port_state"))],
                         ids=lambda c: c["name"])
def test_link_vectors_as_the_client_sends_and_reads_them(case):
    """The client's source / sink / port_state requests are the case's bytes, and it reads their answers."""
    from oep_client import link as linkmod
    r = m.Request.unpack(hx(case["request_hex"]))
    sent = []
    hst = h.Host(lambda b: sent.append(b) or hx(case["answer_hex"]))
    hst._corr, hst.revision = r.corr - 1, 1
    res = m.Result.unpack(hx(case["answer_hex"]))
    if r.op == reg.PROBE_LINK.op["source"]:
        payload = core.link_source_request(struct.unpack_from("<I", r.payload)[0])
        got = hst.request(r.fn, r.op, payload, locked=False)
        assert core.link_source_data(got.payload) == bytes(k & 0xFF for k in range(len(core.link_source_data(got.payload))))
    elif r.op == reg.PROBE_LINK.op["sink"]:
        n = struct.unpack_from("<H", r.payload)[0]
        payload = core.link_sink_request(r.payload[2:2 + n])[:len(r.payload)] if res.succeeded else r.payload
        if res.succeeded:
            hst.request(r.fn, r.op, payload, locked=False)
        else:
            with pytest.raises(h.Rejected):
                hst.request(r.fn, r.op, payload, locked=False)
    else:
        hst._describes[r.fn] = [(catalog.OPS, catalog.pack_ops(reg.PROBE_LINK.op.values()))]
        assert linkmod.port_state(hst, r.fn) == struct.unpack_from("<IB", res.payload)
    assert sent[-1] == hx(case["request_hex"])
