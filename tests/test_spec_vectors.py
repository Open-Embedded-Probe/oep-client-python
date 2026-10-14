"""oep-spec's current shared vectors (tests/vectors/*.json and src/oep_client/registry.py, copied by
tools/sync_registry.sh from the commit in tests/vectors/SPEC_COMMIT, never edited) against this client and its virtual
bench: the core, the contracts common to every extension and every standard interface."""

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


@pytest.mark.parametrize("case", [c for c in load("checks.json")["cases"] if c["algorithm"] == "crc8-dmseq"],
                         ids=lambda c: c["name"])
def test_dmseq_crc8(case):
    """target-console-dmseq CRC-8: the dmseq checker's reference and the virtual bench's dmseq model."""
    from oep_client import conformance_dmseq, virtual_dmseq_model
    data = hx(case["input_hex"])
    assert conformance_dmseq.crc_reference(data) == virtual_dmseq_model.checksum(data) == case["crc"]


@pytest.mark.parametrize("case", load("checks.json")["dmseq_words"], ids=lambda c: c["name"])
def test_dmseq_words(case):
    """target-console-dmseq byte order: DATA0 is byte0, the payload and the CRC-8 of them, little endian."""
    from oep_client import conformance_dmseq
    head = bytes([case["byte0"]]) + hx(case["payload_hex"])
    assert conformance_dmseq.crc_reference(head) == case["crc"]
    assert int.from_bytes((head + bytes([case["crc"]])).ljust(4, b"\0")[:4], "little") == case["data0"]
    if not case["byte0"] & 0x80:                                       # a host answer: the checker builds the same word
        b = case["byte0"]
        assert conformance_dmseq.reply_word(b >> 5 & 1, b >> 4 & 1, hx(case["payload_hex"])) == case["data0"]


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
        hst._describes[0] = [(reg.CORE.tlv["describe"]["max_op_ms"], struct.pack("<I", 5000))]   # read before (§4.4)
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
from oep_client import config, console, core, fixture, riscv, virtual_bench_capture


def vector_probe(fns: dict, max_frame: int = 1024, ops: dict | None = None) -> virtual_bench.VirtualProbe:
    """A virtual bench whose fn numbers are a vector's: fn 0 with one UART bridge (index 0), then each named interface
    with channels 0-15 for its roles (the wire on pins 1 / 2; gpio without drive_levels). `ops`: fn -> the ops its
    ops tag sets instead of every op of its table (core §1.2, §7.4)."""
    core = virtual_bench._core("1.0.0", "vectors", "0123456789ab", 16, {},
                      virtual_bench._transports([(virtual_bench.TRANSPORT["uart_bridge"], 0xFF)]))
    chans = list(range(16))
    offered = [core]
    for fn, name in sorted(((int(k), v) for k, v in fns.items())):
        if name == "oep.fixture.gpio":
            o = virtual_bench._gpio(fn, chans)
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
            o = virtual_bench._config(fn, 0, storage=0)   # no storage: no save / erase
        elif name == "oep.fixture.logic":                              # 80 MHz / 4 = 20 MHz exact; w 2 for two channels
            o = virtual_bench.Offered(fn, 0, name, virtual_bench._roles({k: chans for k in range(4)})
                             + virtual_bench._capture_decl(["one_shot", "repeat", "streaming"], 8, 1 << 20, 8, 1, 80_000_000),
                             inner=virtual_bench._capture_inner([1, 2, 8], 8, 480))
        elif name == "oep.fixture.spi-target":                         # LSB first offered (features bit0)
            o = virtual_bench.Offered(fn, 0, name, virtual_bench._roles({k: chans for k in (1, 2, 3, 4)})
                             + virtual_bench._spi_decl(max_length=16, max_hz=1_000_000, features=1, queue_depth=4))
        elif name == "oep.fixture.analog":
            o = virtual_bench.Offered(fn, 0, name, virtual_bench._roles({k: chans for k in range(4)})
                             + virtual_bench._analog_decl([(0, 0, 4095)], 4096, 1, 1_000_000),
                             inner=virtual_bench._capture_inner([16], 1, 480) + (("exact", True),))
        elif name == "oep.fixture.capture-group":
            tracks = sorted(int(k) for k, v in fns.items() if v in ("oep.fixture.logic", "oep.fixture.analog"))
            o = virtual_bench.Offered(fn, 0, name, virtual_bench._group_decl(tracks))
        elif name.startswith("io.example."):                          # an example fn: op 0x10, a lock-free read
            o = virtual_bench.Offered(fn, 0, name, (catalog.ops_tlv({0x10}),), inner=(("reads", (0x10,)),))
        else:
            raise AssertionError(f"a vector names {name}: add it here")
        if ops and fn in ops:
            o = virtual_bench.Offered(o.fn, o.instance, o.name, (catalog.ops_tlv(ops[fn]),) + tuple(
                t for t in o.tlvs if t[0] != catalog.OPS), inner=o.inner)
        offered.append(o)
    return virtual_bench.VirtualProbe("vectors", max_frame, offered)


S = 0x11223344                                                          # ops.json about: the lock holder's id
FOLLOWED = {"oep.probe.plan", "oep.probe.restart", "oep.probe.link", "oep.fixture.gpio", "oep.fixture.uart",
            "oep.fixture.i2c-target", "oep.fixture.spi-target", "oep.wire.rvswd",
            "oep.target.riscv-dm", "oep.target.console", "oep.probe.config", "oep.fixture.logic",
            "oep.fixture.analog", "oep.fixture.capture-group"}                                       # grows with each stage
NOT_ON_THE_BENCH = {"oep.wire.swd", "oep.target.arm-adi"}               # read by the client only (no swd on the bench)
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
    if "ops of fn 1: source and sink only" in case.get("state", ""):
        ops[1] = {reg.PROBE_LINK.op["source"], reg.PROBE_LINK.op["sink"]}
    fns = dict(case["fns"])
    if {"oep.target.riscv-dm", "oep.target.console"} & set(fns.values()) and "oep.wire.rvswd" not in fns.values():
        fns["4"] = "oep.wire.rvswd"                                     # the wire its connection comes from
    if "ops open/read/close/streams" in case.get("state", ""):
        ops[fn_of(case, "oep.target.console")] = {reg.TARGET_CONSOLE.op[k] for k in ("open", "read", "close", "streams")}
    if "mode1 only; ops10,11,12,14,15,16" in case.get("state", ""):
        ops[fn_of(case, "oep.fixture.logic")] = {0x10, 0x11, 0x12, 0x14, 0x15, 0x16}
    probe = vector_probe(fns, max_frame=64 if "max_frame 64" in case.get("state", "") or "max_frame64" in case.get(
        "state", "") else 1024, ops=ops)
    no_storage = "no storage" in case.get("state", "")
    probe = virtual_bench.VirtualProbe(probe.label, probe.max_frame, [
        virtual_bench._config(o.fn, 0, storage=0 if no_storage else 4096) if o.name == "oep.probe.config" else
        virtual_bench._console(o.fn, (0,) if "mechanisms SDI" in case.get("state", "") else (0, 1, 2))
        if o.name == "oep.target.console" and o.fn not in ops else o for o in probe.offered])
    if "port_s" in case["name"] and "not offered" not in case["name"]:   # a UART bridge probe with port_speed
        probe.offered = [virtual_bench._link(o.fn, port_speed=True) if o.name == virtual_bench.LINK else o
                         for o in probe.offered]
    if "modes=1 (mode0 only)" in case.get("state", ""):
        probe = virtual_bench.VirtualProbe(probe.label, probe.max_frame, [
            virtual_bench.Offered(o.fn, o.instance, o.name, tuple(catalog.u8(virtual_bench.SPI_MODES, 1)
                                  if t[0] == virtual_bench.SPI_MODES else t for t in o.tlvs), o.revision)
            if o.name == "oep.fixture.spi-target" else o for o in probe.offered])
    state = case.get("state", "")
    if "raw-only fn" in state or "dmi only" in state or "declares only dmi" in state:
        ops[fn_of(case, "oep.target.riscv-dm")] = {reg.TARGET_RISCV_DM.op["dmi"]}
    if "oep.wire.rvswd" in fns.values():                               # any two of channels 0-15 (debug §1), two seats
        probe = virtual_bench.VirtualProbe(probe.label, probe.max_frame, [
            virtual_bench.Offered(o.fn, 0, o.name, virtual_bench._roles({1: list(range(16)), 2: list(range(16))}) + (
                catalog.u32(catalog.MAX_CLOCK_HZ, 4_000_000), catalog.u8(virtual_bench.MAX_CONNECTIONS, 2)))
            if o.name == "oep.wire.rvswd" else o for o in probe.offered])
    if ops:
        probe = virtual_bench.VirtualProbe(probe.label, probe.max_frame, [
            virtual_bench.Offered(o.fn, o.instance, o.name, (catalog.ops_tlv(ops[o.fn]),) + tuple(
                t for t in o.tlvs if t[0] != catalog.OPS), o.revision, inner=o.inner) if o.fn in ops else o
            for o in probe.offered])
    if "formats declares 8N1 only" in case.get("state", ""):
        probe = virtual_bench.VirtualProbe(probe.label, probe.max_frame, [
            virtual_bench._uart(o.fn, 0, list(range(16)), 3_000_000, formats=(0,)) if o.name == "oep.fixture.uart" else o
            for o in probe.offered])
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


def gpio_state(case, ep, hst):
    """gpio's cases: session S; channel 3 in fn 2's plan; the read after the set."""
    state = case["state"]
    hst.open(3000)                                                      # S holds the lock (session_id 0 aside)
    if "channel 3 in fn 2's plan" in state or "after the set" in state or "channel 9 in no plan" in state:
        core.plan_apply(hst, [(2, 1, 3)])
    if "after the set" in state:
        fixture.Gpio(hst, 2).set([(3, fixture.Gpio.OUTPUT_HIGH)])


def uart_state(case, ep, hst):
    """fixture §2's cases on fn 5 (RX role 1 / TX role 2 on channel 3), made through the client; the boot-scoped
    write position by the stream the bench carries on."""
    state, name = case["state"], case["name"]
    uart = fixture.FixtureUart(hst, 5)
    hst.open(3000)
    if "write position7" in state or "write position 7" in state:
        ep.uart_carry[5] = (3 if "abcd" in state else 7, 0)
    role = 2 if "TX-only plan" in state else 1
    if "plan" in state or "the configure above failed" in state:
        core.plan_apply(hst, [(5, role, 3)])
    if "configured at 9600" in state or "abcd" in state:
        uart.configure(9600)
    if "abcd" in state:
        ep.uart_rx(5, b"abcd")
    if "fails" in state or "the configure above failed" in state:
        ep.uart_fail_configure.add(5)
    if "the configure above failed" in state:
        with pytest.raises(h.Failed):
            uart.configure(115200)


def i2c_state(case, ep, hst):
    """fixture §3's cases on fn 3 (SDA role 1 on channel 1, SCL role 2 on channel 2)."""
    state = case["state"]
    hst.open(3000)
    core.plan_apply(hst, [(3, 1, 1), (3, 2, 2)])
    t = fixture.I2cTarget(hst, 3)
    if "running" in state or "the configure above failed" in state:
        t.configure(0x42)
        for frame in (b"a", b"b"):
            ep.i2c_write(3, frame)
        ep.i2c[3].rx_frames, ep.i2c[3].errors = 5, 1
        t.preload_tx(b"x")
        if t.offers(t.STRETCH):
            t.stretch(50)
    if "fails" in state or "the configure above failed" in state:
        ep.target_fail_configure.add(3)
    if "the configure above failed" in state:
        with pytest.raises(h.Failed):
            t.configure(0x42)


def spi_state(case, ep, hst):
    """fixture §4's cases on fn 14 (SCK MOSI MISO CS on channels 1-4)."""
    state, name = case["state"], case["name"]
    hst.open(3000)
    core.plan_apply(hst, [(14, r, r) for r in (1, 2, 3, 4)])
    t = fixture.SpiTarget(hst, 14)
    if "previous mode0" in state or "configured" in state or "running" in state or "the configure above failed" in state:
        t.configure(0, 1 if "bit_order 1" in state else 0)
    if "running" in state or "the configure above failed" in state or "previous mode0" in state:
        t.arm(16, b"")
        ep.spi_transfer(14, b"\x01")
        t.arm(16, b"")
        st = ep.spi[14]
        st.transactions, st.errors = 4, 1
    if "driver cannot prepare output" in state:
        ep.spi_fail_arm.add(14)
    if "fails" in state or "the configure above failed" in state:
        ep.target_fail_configure.add(14)
    if "the configure above failed" in state:
        with pytest.raises(h.Failed):
            t.configure(0, 0)
        ep.spi_transfer(14, b"\x55")                                 # neither queued nor counted in state 0
    if "one transaction queued" in state:
        ep.spi_ns = False
        t.arm(16, b"")
        wire = [1, 1, 0, 0, 0, 0, 0, 0, 1, 0, 1, 1]
        ep.spi_transfer(14, fixture.pack_wire_bits(wire, 1 if "bit_order 1" in state else 0), bits=12)


DMSTATUS = 0x00400382                                                   # the vectors' DM: halted, version 2, impebreak


def wire_target(ep, fn, pair, target_id=None):
    """A halted RV32 hart on `pair` with no pending havereset, DMSTATUS 0x00400382."""
    tg = ep._target(fn, pair)
    tg.present, tg.halted, tg.havereset, tg.dmstatus_bits, tg.target_id = True, True, False, 0x400000, target_id
    assert tg.dmstatus() == DMSTATUS
    return tg


def rvswd_state(case, ep, hst):
    """debug §1-§3's cases on fn 4 (any pair of channels 0-15, two seats), made through the client."""
    state, fn = case["state"], fn_of(case, "oep.wire.rvswd")
    hst.open(3000)
    wire = riscv.Wire(hst, "oep.wire.rvswd")
    wire_target(ep, fn, (1, 2), 0x00203500 if "target_id" in state else None)
    wire_target(ep, fn, (3, 4))
    if "connection 1 on" in state or "owns connection 1" in state or state == "as above":
        assert wire.attach(halt=False, pins=(1, 2), max_speed=1_000_000)[0] == 1
    if "next resource 65536" in state:
        ep._next_resource = 0x10000


def dm_state(case, ep, hst):
    """debug §4's cases on fn 6 over the rvswd connection 1 (fn 4), made through the client."""
    state, name = case["state"], case["name"]
    hst.open(3000)
    tg = wire_target(ep, 4, (1, 2))
    if "connection 9" in state or state == "—":
        return
    conn, _ = riscv.Wire(hst, "oep.wire.rvswd").attach(halt=False, pins=(1, 2), max_speed=1_000_000)
    assert conn == 1
    dm = riscv.RiscvDm(hst, conn, "oep.target.riscv-dm")
    if "running" in state:
        tg.halted = False
    if "writing a0 fails" in state:
        tg.fail_regs = {0x100A}
    if "DMSTATUS0x200" in state:                                         # allhalted on the first wait read
        tg.dmi[0x11] = 0x200
    if "DMSTATUS reads" in state:
        tg.dmi[0x11] = DMSTATUS
    if "restoring saved DATA failed" in state or "closed after the previous high-level op failed cleanup" in state:
        tg.restore_fails = True
    if "closed after the previous high-level op failed cleanup" in state:
        with pytest.raises(riscv.TargetError):
            dm.write_block(0x20000000, struct.pack("<2I", 7, 8))
        assert conn not in ep.conns


def console_state(case, ep, hst):
    """console §1-§2's cases on fn 7 over an rvswd connection (fn 4), made through the client; marks put in place."""
    state = case["state"]
    hst.open(3000)
    wire_target(ep, 4, (1, 2))
    if "65536" in state or "65537" in state:
        ep._next_resource = 0x10000                                   # the ids past u16 (core §9)
    if state == "—":
        return
    conn, _ = riscv.Wire(hst, "oep.wire.rvswd").attach(halt=False, pins=(1, 2), max_speed=1_000_000)
    if "stream" not in state.replace("streams", "") or "absent on RX-only" in case["name"]:
        return                                                         # a connection, no stream (open's cases)
    con = console.Console(hst)
    sid = con.open(conn, con.SDI if "is SDI" in state or "mechanisms SDI" in state else con.DMSEQ)
    if "5 bytes written" in state:
        ep.emit(sid, b"hello")
    if "one explicit host mark" in state:
        ep.now_ns = lambda: 1_000_000
        con.mark(0)
    if "marks 5 to 8 kept" in state:
        s = ep.streams[sid]
        s.marks = [(k, k * 10, 3, k * 1_000_000, k) for k in range(5, 9)]
        s.serial = 9


def config_state(case, ep, hst):
    """probe.config §1-§3's cases on fn 8, made through the client."""
    state = case["state"]
    hst.open(3000)
    cfg = config.ProbeConfig(hst)
    if "response extensions" in case["name"]:
        ep.config_get_tail = m.tlv(0x7F, b"x")                        # an answer TLV after the items (core §2.3)
    if "idle channel4 exists" in state or "one item generation2" in state or "one idle item" in state:
        cfg.set([config.Idle(channel=4, mode="hi-z")])
    if "disable channel4" in state:
        cfg.set([config.Disable(channel=4)])
    if "CRC-valid saved blob" in state:
        ep.saved = {(config.ITEM["disable"], 5): struct.pack("<H", 5), (config.ITEM["idle"], 6): struct.pack("<HB", 6, 1),
                    (config.ITEM["label"], 99): struct.pack("<H", 99) + b"gone"}     # 99: past the probe's channels
        ep.reboot(EXAMPLE_BOOT_ID)
        hst.session = None
        hst.open(3000)
    if "generation0xFFFFFFFF" in state:
        ep.generation = 0xFFFFFFFF


def capture_state(case, ep, hst):
    """capture §2-§4's cases (logic fn 9 / 19, analog fn 20 / 13, capture-group fn 12), made through the client; what
    the probe holds (an analog's calibration data, a capture's retained bytes) put on the bench's tracks."""
    from oep_client import capture
    state, name, clock = case["state"], case["name"], ep.now
    if "without a session" in name:
        return
    hst.open(3000)
    fns = {v: int(k) for k, v in case["fns"].items()}
    logic = capture.LogicCapture(hst, fns["oep.fixture.logic"]) if "oep.fixture.logic" in fns else None
    analog = capture.AnalogCapture(hst, fns["oep.fixture.analog"]) if "oep.fixture.analog" in fns else None
    if "fn 9 subscribed" in state:
        logic.subscribe()
    if logic is not None and ("roles 0 and 1" in state or "as above" in state):
        core.plan_apply(hst, [(logic.fn, 0, 1), (logic.fn, 1, 2)])
    elif logic is not None and ("one-shot done" in state or "generation" in state or "role0 channel1" in state
                                or "repeat" in state or "streaming" in state or "mode1" in state or "fn9" in state
                                or "fn 9" in state):
        core.plan_apply(hst, [(logic.fn, 0, 1)])
    if "one-shot done" in state or "generation 1" == state or "generation2" in state or "generation1;" in state:
        clock.t = 5                                                     # 1000 samples from 5 ms (+-50 ns)
        logic.configure(rate=1_000_000, samples=1000)
        logic.start()
        if "generation2" in state:
            clock.t = 7
            logic.wait()
            logic.start()
        clock.t = 9
        logic.wait()
    if "generation1; 48 retained bytes" in state:                     # what a read of max 0xFFFF may return in 64
        cap = ep.captures[logic.fn]
        cap.data, cap.base = bytearray(range(48)), 0
    if "mode1, immediate" in state:
        logic.configure(rate=1_000_000, samples=1000)
    if "generation0xFFFFFFFF" in state:
        logic.configure(rate=1_000_000, samples=1000)
        ep.captures[logic.fn].generation = 0xFFFFFFFE
        logic.start()
        clock.t += 2
        logic.wait()
    if "ring overflowed" in state:                                      # repeat, w 1: 8000 samples are 1000 bytes
        ep.captures[logic.fn].widths = {1}
        clock.t = 5
        logic.configure(rate=1_000_000, samples=8000, mode=capture.REPEAT, segments=4)
        logic.start()
        clock.t = 25                                                    # serials 0 and 1 end at 13 and 21 ms
        ep.capture_overflow(logic.fn, 2)
    if "streaming stopped in error" in name:                          # w 8, 16 samples a segment, 1 kHz
        ep.captures[logic.fn].widths = {8}
        logic.subscribe()
        logic.configure(rate=1000, mode=capture.STREAMING)
        ep.captures[logic.fn].samples = 16
        logic.start()
        clock.t = 30
        ep.pushes()
        clock.t = 40
        ep.pushes()                                                     # bytes 24-31 and 32-39 in frames of their own
        ep.capture_overflow(logic.fn, 2)
    if analog is not None and "calibration" in name:
        held = {"everything held": m.tlv(1, bytes([0xFF, 15]) + b"com.example.adc" + struct.pack("<H", 4) + bytes(range(4)))
                + m.tlv(2, struct.pack("<IQI", 1500, 7_000_000, 1200)),
                "nothing held": b"",
                "beyond max_frame": m.tlv(1, bytes([0xFF, 15]) + b"com.example.adc" + struct.pack("<H", 60) + bytes(60))}
        ep.captures[analog.fn].calibration = lambda: next(v for k, v in held.items() if k in name)
    if analog is not None and "configure" in name:
        roles = 2 if "2 roles" in name else 1
        core.plan_apply(hst, [(analog.fn, r, r + 1) for r in range(roles)])
        cap = ep.captures[analog.fn]
        cap.skews, cap.diagnostics = (0, 5)[-roles:], False         # one nonzero skew, no optional diagnostics
    if "capture-group" in name:
        group = capture.CaptureGroup(hst, fn=fns["oep.fixture.capture-group"])
        if logic is None:
            return                                                     # the bind's TLV is refused before its fns
        core.plan_apply(hst, [(logic.fn, 0, 1), (analog.fn, 0, 2)])
        if "non-immediate" in name or "both tracks immediate" in state or "status" in name or "start" in name:
            logic.configure(rate=1_000_000, samples=100_000, trigger=(capture.EDGE, 0, 0) if "edge" in state else None)
            analog.configure(rate=1000, samples=100)
        if "status" in name or "start" in name:
            group.bind([logic, analog])
            grp = ep.groups[group.fn]
            grp.generation = 4
            ep.captures[logic.fn].generation, ep.captures[analog.fn].generation = 3, 1
            clock.t = 7
        if "status" in name:
            group.start([logic, analog])


STATES = {"oep.probe.plan": plan_state, "oep.probe.restart": restart_state, "oep.probe.link": link_state,
          "oep.fixture.gpio": gpio_state, "oep.fixture.uart": uart_state, "oep.fixture.i2c-target": i2c_state,
          "oep.fixture.spi-target": spi_state, "oep.wire.rvswd": rvswd_state, "oep.target.riscv-dm": dm_state,
          "oep.target.console": console_state, "oep.probe.config": config_state, "oep.fixture.logic": capture_state,
          "oep.fixture.analog": capture_state, "oep.fixture.capture-group": capture_state}


def make_case(case):
    ep, hst, clock = bench_for(case)
    target = case["fns"][str(m.Request.unpack(hx(case["request_hex"])).fn)]   # the interface the case asks
    STATES[target](case, ep, hst)
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


INTERFACE_REFUSALS = [c for c in load("refusals.json")["cases"] if c["fns"] and set(c["fns"].values()) <= FOLLOWED]


@pytest.mark.parametrize("case", INTERFACE_REFUSALS, ids=lambda c: c["name"])
def test_interface_refusals_from_the_virtual_bench(case):
    """refusals.json's cases of followed interfaces (refusals.json about: session S holds the lock; channel 3 is in the
    gpio fn's plan)."""
    ep, hst, _ = bench_for(case)
    hst.open(3000)
    gpio = next((int(k) for k, v in case["fns"].items() if v == "oep.fixture.gpio"), None)
    if gpio is not None:
        ep.load_config([], saved=False)
        ep.plan.add((gpio, 1, 3))
    corr = m.Request.unpack(hx(case["request_hex"])).corr
    ep.newest_corr, _ = (corr - 2) % 0xFFFF + 1, ep.resend.clear()
    assert ep.handle(hx(case["request_hex"]), 0).hex() == case["answer_hex"]


@pytest.mark.parametrize("case", [c for c in OPS if c["name"].startswith(("gpio set: channel 3", "gpio read", "gpio set: channel 9"))],
                         ids=lambda c: c["name"])
def test_gpio_vectors_as_the_client_sends_and_reads_them(case):
    """The client's gpio set / read send the case's bytes and read its answer (fixture §1)."""
    r = m.Request.unpack(hx(case["request_hex"]))
    sent = []
    hst = h.Host(lambda b: sent.append(b) or hx(case["answer_hex"]))
    hst._corr, hst.revision, hst.session = r.corr - 1, 1, r.session or None
    hst._fns["oep.fixture.gpio"], hst._revisions[r.fn] = r.fn, 1
    hst._describes[r.fn] = [(catalog.OPS, catalog.pack_ops(reg.FIXTURE_GPIO.op.values()))]
    g = fixture.Gpio(hst, r.fn)
    res = m.Result.unpack(hx(case["answer_hex"]))
    n = r.payload[0]
    if r.op == fixture.Gpio.SET:
        pairs = [struct.unpack_from("<HB", r.payload, 1 + 3 * k) for k in range(n)]
        if res.succeeded:
            g.set(pairs)
        else:
            with pytest.raises(h.Unavailable) as e:
                g.set(pairs)
            assert e.value.channels == [9]
    else:
        channels = [struct.unpack_from("<H", r.payload, 1 + 2 * k)[0] for k in range(n)]
        assert g.read(channels) == list(res.payload[1:1 + n])
    assert sent[0] == hx(case["request_hex"])


def test_fixture_boundaries_on_the_virtual_bench():
    """interface_boundaries.json fixtures (fixture §3/§4): consecutive i2c reads from preload slots, counters that
    saturate, and the spi read_rx of an empty queue."""
    row = load("interface_boundaries.json")["fixtures"]
    case = {"name": "fixtures", "fns": {"3": "oep.fixture.i2c-target", "14": "oep.fixture.spi-target", "10": "oep.probe.plan"}}
    ep, hst, _ = bench_for(case)
    hst.open(3000)
    core.plan_apply(hst, [(3, 1, 1), (3, 2, 2)] + [(14, r, 4 + r) for r in (1, 2, 3, 4)])
    i2c = fixture.I2cTarget(hst, 3)
    i2c.configure(0x42)
    for slot in row["i2c_slots_hex"]:
        i2c.preload_tx(bytes.fromhex(slot))
    assert [ep.i2c_read(3, n).hex() for n in row["i2c_read_lengths"]] == row["expected_i2c_reads_hex"]
    ep.i2c[3].errors = row["counter_before"]
    for _ in range(row["increments"]):
        ep.i2c_write(3, bytes(64))                                    # past max_length: an error each
    assert ep.i2c[3].errors == row["expected_counter"]
    fixture.SpiTarget(hst, 14).configure(0, 0)
    ep.newest_corr = 0
    got = ep.handle(m.Request(1, 14, reg.FIXTURE_SPI_TARGET.op["read_rx"], b"", S).pack(), 0)
    assert got.hex() == row["spi_empty_answer_hex"]


# ---- debug (oep-if-debug): the client's requests and its reading of the answers --------------------------------------
from oep_client import arm

DEBUG_CLIENT = [c for c in OPS if c["name"].startswith((                # what the client sends (it pages from 0)
    "rvswd connections: one", "rvswd scan: one", "rvswd attach: a second", "rvswd attach: a resource",
    "rvswd attach: busy", "swd ", "arm-adi ", "riscv-dm halt: halted", "riscv-dm halt on", "riscv-dm dmi: one read",
    "riscv-dm dmi: n = 0", "riscv-dm step on", "riscv-dm run: the preparation", "riscv-dm write_block"))]


def scripted(case):
    """A host whose one request goes out as the case's and gets the case's answer."""
    r = m.Request.unpack(hx(case["request_hex"]))
    sent = []
    hst = h.Host(lambda b: sent.append(b) or hx(case["answer_hex"]))
    hst._corr, hst.revision, hst.session = r.corr - 1, 1, S
    name = case["fns"][str(r.fn)]
    hst._fns[name], hst._revisions[r.fn] = r.fn, 1
    table = reg.INTERFACES[name]
    hst._describes[r.fn] = [(catalog.OPS, catalog.pack_ops(table.op.values())), (catalog.MAX_LENGTH, struct.pack("<H", 256))]
    hst._describes[0] = [(core.CORE_MAX_OP_MS, struct.pack("<I", 1000))]
    hst.limits = {"max_frame": 64 if "frame64" in case["name"] else 1024}    # what confirm said (a set's size check)
    return r, hst, sent


@pytest.mark.parametrize("case", DEBUG_CLIENT, ids=lambda c: c["name"])
def test_debug_vectors_as_the_client_sends_and_reads_them(case):
    """oep-if-debug §1-§6: the client's wire and target requests are the case's bytes, and it reads their answers."""
    r, hst, sent = scripted(case)
    name = case["name"]
    if name.startswith("rvswd connections"):
        rows = riscv.Wire(hst, "oep.wire.rvswd").connections()
        assert [(c.connection, c.pins, c.speed_hz, c.target_id) for c in rows] == (
            [(1, (1, 2), 1_000_000, (1, struct.pack("<I", 0x00203500)))])
    elif name.startswith("rvswd scan"):
        found = riscv.Wire(hst, "oep.wire.rvswd").scan([(1, 2)])
        assert [(f.kind, f.pins, f.dmstatus) for f in found] == [(1, (1, 2), DMSTATUS)]
    elif name.startswith("rvswd attach"):
        wire = riscv.Wire(hst, "oep.wire.rvswd")
        pins = struct.unpack_from("<HH", r.payload, 1)
        if "busy" in name:
            with pytest.raises(h.Unavailable) as e:
                wire.attach(halt=False, pins=pins, max_speed=1_000_000)
            assert e.value.channels == [1]
        else:
            conn, status = wire.attach(halt=False, pins=pins, max_speed=1_000_000)
            assert (conn, status, wire.flags, wire.speed_hz) == (struct.unpack_from("<I", hx(case["answer_hex"]), 5)[0],
                                                                DMSTATUS, 0, 1_000_000)
    elif name.startswith("swd connections"):
        (row,) = arm.SwdWire(hst).connections()
        assert (row.connection, row.pins, row.target_id, row.targetsel) == (
            0x10000, (1, 2), (2, struct.pack("<I", 0x2BA01477)), 0x01000001)   # read identity, supplied selector
    elif name.startswith("swd attach"):
        with pytest.raises(h.Unavailable) as e:
            arm.SwdWire(hst).attach(targetsel=0x01000002, pins=(1, 2), max_speed=1_000_000)
        assert e.value.channels == [1]
    elif name.startswith("arm-adi"):
        conn = struct.unpack_from("<I", r.payload)[0]
        adi = arm.ArmAdi(hst, conn)
        if "reserved" in name:
            with pytest.raises(h.Unsupported):
                adi.transfer(adi.req(False, False, 0, 7) + b"\x12")
        else:
            assert adi.transfer(b"") == [] and adi.last_ack == 0
    else:
        dm = riscv.RiscvDm(hst, struct.unpack_from("<I", r.payload)[0])
        if "halt: halted" in name:
            dm.halt()
        elif "halt on" in name:
            with pytest.raises(h.NoResource):
                dm.halt()
        elif "one read" in name:
            assert dm.dmi([dm.step_read(0x11)]) == (1, [DMSTATUS])
        elif "n = 0" in name:
            assert dm.dmi([]) == (0, [])
        elif "step on" in name:
            with pytest.raises(riscv.StepError) as e:
                dm.step()
            assert e.value.status == riscv.STATUS["state"] and not e.value.step_left
        elif "run:" in name:
            with pytest.raises(riscv.TargetError) as e:
                dm.run(0x20000000, [(0x100A, 7)], timeout_ms=100, outs=())
            res = riscv.RiscvDm.run_result(e.value.result)
            assert res.not_run and res.status == riscv.STATUS["line"] and res.values == []
        else:                                                           # the cleanup failed: partial, every word done
            with pytest.raises(riscv.TargetError) as e:
                dm.write_block(0x20000000, struct.pack("<2I", 7, 8))
            assert e.value.done == 2 and e.value.result.detail == m.PARTIAL and e.value.status == riscv.STATUS["line"]
    assert sent[-1] == hx(case["request_hex"])


SPI_PROFILE_U32 = pytest.mark.xfail(strict=True, reason="oep-spec a09fa45's 'mode0 SPI target' profile packs max_length as "
                                    "u32 (tools/oepvectors1.py); core §7.4 makes it u16")


@pytest.mark.parametrize("profile", [pytest.param(p, marks=SPI_PROFILE_U32) if p["name"] == "mode0 SPI target" else p
                                     for p in load("interface_profiles.json")["profiles"]], ids=lambda p: p["name"])
def test_interface_profiles_as_the_host_reads_them(profile):
    """interface_profiles.json (core §7.2-§7.4): the list entries (no flags byte) and each describe's ops."""
    total, entries = catalog.unpack_list_result(m.Result.unpack(hx(profile["list_answer_hex"])).payload)
    assert total == len(entries) == len(profile["entries"])
    assert [(e.fn, e.instance, e.revision, e.name) for e in entries] == [
        (p["fn"], p["instance"], p["revision"], p["name"]) for p in profile["entries"]]
    for p in profile["entries"]:
        payload = m.Result.unpack(hx(p["describe_answer_hex"])).payload
        assert payload[0] == 0                                          # more = 0
        assert catalog.decode_description(payload[1:]).ops == set(p["ops"])


# ---- console and probe.config: the client's requests and its reading of the answers ----------------------------------

CONSOLE_CLIENT = [c for c in OPS if c["name"].startswith(("console open", "console marks: one", "console marks: from_serial",
                                                          "console streams: one", "console streams: resource",
                                                          "console read: empty", "console write: SDI"))]


@pytest.mark.parametrize("case", CONSOLE_CLIENT, ids=lambda c: c["name"])
def test_console_vectors_as_the_client_sends_and_reads_them(case):
    """console §1 / common §1: the client's open, marks, streams, read and write are the case's bytes."""
    r, hst, sent = scripted(case)
    name, con = case["name"], console.Console(hst)
    res = m.Result.unpack(hx(case["answer_hex"]))
    if name.startswith("console open"):
        assert con.open(*struct.unpack_from("<IB", r.payload)) == struct.unpack_from("<I", res.payload)[0]
    elif name.startswith("console streams"):
        assert [(i.stream, i.connection, i.mechanism) for i in con.streams()] == [
            struct.unpack_from("<IIB", res.payload, 2)]
    else:
        con.stream = struct.unpack_from("<I", r.payload)[0]
        if name.startswith("console marks"):
            marks, more = con.marks_page(struct.unpack_from("<I", r.payload, 4)[0])
            assert more == bool(res.payload[0]) and len(marks) == res.payload[1] and all(mk.kind == 3 for mk in marks)
        elif name.startswith("console read"):
            c = con.read_from(struct.unpack_from("<Q", r.payload, 5)[0], struct.unpack_from("<H", r.payload, 13)[0])
            assert (c.start, c.more, c.gap, c.data) == (5, False, False, b"")
        else:
            with pytest.raises(h.Unsupported):
                con.write(b"x")                                    # an SDI stream: no write (console §1)
    assert sent[-1] == hx(case["request_hex"])


CONFIG_CLIENT = [c for c in OPS if c["name"].startswith((
    "probe.config state: no saved", "probe.config state: independent", "probe.config get:", "probe.config unset:",
    "probe.config set: longest", "probe.config set: changed contents", "probe.config set: disable conflicts",
    "probe.config set: idle conflicts"))]


@pytest.mark.parametrize("case", CONFIG_CLIENT, ids=lambda c: c["name"])
def test_config_vectors_as_the_client_sends_and_reads_them(case):
    """probe.config §1-§3: the client's get / set / unset / state are the case's bytes, and it reads the answers - get's
    count apart from the answer's own TLVs."""
    r, hst, sent = scripted(case)
    name, cfg = case["name"], config.ProbeConfig(hst)
    res = m.Result.unpack(hx(case["answer_hex"]))
    if name.startswith("probe.config state"):
        st = cfg.state()
        assert (st.storage, st.saved_generation, st.reason) == (
            ("none", 0, None) if "no saved" in name else ("partial", 0, "item"))
    elif name.startswith("probe.config get"):
        first = struct.unpack_from("<H", r.payload)[0]
        hst._corr = r.corr - 1
        if first:
            rd = m.Reader(cfg._call(cfg.GET, r.payload, locked=False).payload)
            assert rd.take("BIB") == (0, 2, 0)
        else:
            assert cfg.get() == (2, [(config.ITEM["idle"], struct.pack("<HB", 4, 0))])
    elif name.startswith("probe.config unset"):
        assert cfg.unset([("disable", 4)]) == 3
    else:
        items = [config.decode(t, v) for t, v in config.split_items(r.payload[1:], r.payload[0])[0]]
        if res.resolution == m.REJECTED:
            with pytest.raises(h.Unavailable):
                cfg.set(items)
        else:
            assert cfg.set(items) == 2
    assert sent[-1] == hx(case["request_hex"])


CAPTURE_CLIENT = [c for c in OPS if c["name"].startswith((
    "logic subscribe: min_bytes", "logic unsubscribe", "logic segments:", "logic configure: one-shot", "logic start:",
    "logic status:", "analog configure:", "analog calibration: everything", "analog calibration: nothing",
    "capture-group status:", "capture-group start:", "logic release: old generation"))]


@pytest.mark.parametrize("case", CAPTURE_CLIENT, ids=lambda c: c["name"])
def test_capture_vectors_as_the_client_sends_and_reads_them(case):
    """capture §2-§4: the client's subscribe / unsubscribe / configure / start / status / segments / release /
    calibration and the group's start / status are the case's bytes, and it reads the answers (core §10.3)."""
    from oep_client import capture
    r, hst, sent = scripted(case)
    name = case["name"]
    track = (capture.AnalogCapture if name.startswith("analog") else capture.LogicCapture)(hst, fn=r.fn)
    if name.startswith("logic subscribe"):
        track.subscribe(1024, 20)
    elif name.startswith("logic unsubscribe"):
        track.unsubscribe()
    elif name.startswith("logic segments"):
        segs, more = track.segments_page(struct.unpack("<I", r.payload)[0])
        want = {"one segment": [(0, 1000, 5_000_000, 50, None, 1)], "serial_done": [],
                "dropped": [(0, 8000, 5_000_000, 50, None, 1), (1, 8000, 13_000_000, 50, None, 1)]}
        assert not more and [(s.serial, s.samples, s.start_ns, s.start_uncertainty_ns, s.trigger_index, s.generation)
                             for s in segs] == next(v for k, v in want.items() if k in name)
    elif name.startswith("logic configure"):
        cfg = track.configure(rate=20_000_000, samples=200_000)
        assert (cfg.rate, cfg.width, cfg.positions, cfg.samples) == (20_000_000, 2, [0, 1], 200_000)
    elif name.startswith("logic start: bounded"):
        assert track.start() == 1 == track.generation
    elif name.startswith("logic start: generation exhausted"):
        with pytest.raises(h.Unavailable) as e:
            track.start()
        assert e.value.cause == "limit"
    elif name.startswith("logic status"):
        st = track.status()
        assert (st.state, st.dropped, st.error_name, st.generation) == (capture.STATE["error"], True, "storage", 1)
        assert st.write_pos == (2000 if "inside a segment" in name else 32)
    elif name.startswith("logic release"):
        with pytest.raises(h.Unavailable) as e:
            track.release(0, generation=1)
        assert e.value.cause == "wrong_state"
    elif name.startswith("analog configure"):
        cfg = track.configure(rate=1000, samples=10)
        roles = [0, 1] if "2 roles" in name else [0]
        assert (cfg.rate, cfg.order, cfg.samples, cfg.scale_nv) == (1000, roles, 10, {k: 1_000_000 for k in roles})
        assert cfg.skew_ns == {k: 5 if k == roles[-1] else 0 for k in roles} and cfg.frontend == {} and cfg.reference is None
    elif name.startswith("analog calibration"):
        cal = track.calibration()
        assert (cal.factory, cal.vrefint, cal.vrefint_nominal_mv) == (
            ([(None, "com.example.adc", bytes(range(4)))], (1500, 7_000_000), 1200) if "everything" in name else ([], None, None))
    else:
        group = capture.CaptureGroup(hst, fn=r.fn)
        if "status" in name:
            assert group.status() == capture.GroupStatus(capture.STATE["capturing"], 7_000_000, 5)
        else:
            assert group.start() == 7_000_000 and group.generation == 5 and group.generations == {9: 4, 13: 2}
    assert sent[-1] == hx(case["request_hex"])


@pytest.mark.parametrize("case", load("logic_layout.json")["cases"], ids=lambda c: c["name"])
def test_logic_layout_as_the_client_reads_it_and_the_bench_packs_it(case):
    """capture §1.1: channel k of sample i is stream bit i*w + pos[k]; a sample may cross a byte boundary."""
    from oep_client import capture
    track = object.__new__(capture.LogicCapture)                        # its layout only: no probe asked
    track.config = capture.Config(width=case["w"], positions=case["pos"], samples=case["samples"])
    stream = hx(case["stream_hex"])
    assert ["".join(map(str, track.channel(stream, k, case["samples"]))) for k in range(len(case["pos"]))] == case["channels"]
    levels = [sum(int(case["channels"][k][i]) << case["pos"][k] for k in range(len(case["pos"]))) for i in range(case["samples"])]
    assert virtual_bench_capture.pack_samples(levels, case["w"]) == stream


CAPTURE_EVENTS, CAPTURE_DATA = load("ops.json")["events"], load("ops.json")["data"]


@pytest.mark.parametrize("case", CAPTURE_EVENTS, ids=lambda c: c["name"])
def test_capture_events_as_the_client_reads_them(case):
    """capture §3.4 / §4.2, core §10.2: role 0x03, the kind's fixed part with the generation it was made in; a
    group's event read as the group's."""
    from oep_client import capture
    frame = hx(case["event_hex"])
    group = case["fns"][str(struct.unpack_from("<H", frame, 1)[0])] == "oep.fixture.capture-group"
    e = capture.unpack_event(frame, group=group)
    assert frame[0] == m.ROLE_EVENT and e.generation == case["generation"]
    assert e.kind in ("stopped", "triggered") and (e.kind == "stopped" or not group)


@pytest.mark.parametrize("case", CAPTURE_DATA, ids=lambda c: c["name"])
def test_capture_data_frames_as_the_client_reads_them(case):
    """core §10.2, capture §3.4: role 0x04 with the generation and the segment (segment_info) every streaming frame
    carries, inside that one segment; after an error stop at write_pos 32 the host keeps what came before it (§2.2)."""
    from oep_client import capture
    frame = hx(case["frame_hex"])
    fn, seq, position, data, generation = capture.unpack_push(frame)
    info = capture.push_segment(frame)
    assert frame[0] == m.ROLE_DATA and generation == case["generation"] and info is not None
    assert info.position <= position and info.samples >= 1
    if "layout" in case:                                               # w 3: the segment's bytes so far, no more
        assert position + len(data) <= info.position + -(-info.samples * case["layout"]["w"] // 8)
    if "keep" in case:
        got = capture.Received(data=bytearray(data), start=position)
        got.drop_from(32)
        assert bytes(got.data) == data[:case["keep"]] and got.dropped_after_error == len(data) - case["keep"]


@pytest.mark.parametrize("case", [c for c in CAPTURE_DATA if "keep" in c], ids=lambda c: c["name"])
def test_capture_data_frames_from_the_virtual_bench(case):
    """The streaming state above (w 8, 16 samples a segment, 1 kHz) on the bench: the frame at the case's position is
    the case's, seq aside."""
    want = hx(case["frame_hex"])
    ep, hst, _ = bench_for({**OPS[0], "fns": case["fns"], "state": case["state"]})
    from oep_client import capture
    hst.open(3000)
    logic = capture.LogicCapture(hst, fn=9)
    core.plan_apply(hst, [(9, 0, 1)])
    ep.captures[9].widths = {8}
    logic.subscribe()
    logic.configure(rate=1000, mode=capture.STREAMING)
    ep.captures[9].samples = 16
    ep.now.t = 1                                                       # serial 1 (samples 16-31) starts at 17 ms
    logic.start()
    frames = []
    for t in (25, 33, 41):                                              # bytes 24-31, then 32-39, a frame each
        ep.now.t = t
        frames += ep.pushes()
    at = struct.unpack_from("<Q", want, 5)[0]
    got = next(f for f in frames if f[0] == m.ROLE_DATA and struct.unpack_from("<Q", f, 5)[0] == at)
    assert got[:3] + got[5:] == want[:3] + want[5:]


@pytest.mark.parametrize("case", load("refusals.json")["cases"], ids=lambda c: c["name"])
def test_refusals_as_the_host_reads_them(case):
    """refusals.json's answers read by the host: a rejection becomes its exception (unsupported with its tag, or the
    fixed part's mark 0)."""
    res = m.Result.unpack(hx(case["answer_hex"]))
    if res.succeeded:
        assert res.payload == b""
        return
    err = h.rejection(res)
    assert res.resolution == m.REJECTED and isinstance(err, h.Rejected)
    if res.detail == m.UNSUPPORTED:
        assert isinstance(err, h.Unsupported) and (err.tag is None) == (res.payload[:1] == b"\x00")


BOUNDARIES = load("interface_boundaries.json")


@pytest.mark.parametrize("row", BOUNDARIES["batching"], ids=lambda r: r["name"])
def test_boundary_batching_on_the_virtual_bench(row):
    """core §10.3 / capture §2.1 rule 2: when a streaming capture's pending data goes out (a stopped one's rest without
    waiting, never an empty frame), and the frames it makes at max_frame (15 + 7 + 27 bytes of header and TLVs)."""
    due = endpoint.push_due(row["min_bytes"], row["max_delay_ms"], row["age_ms"], row["pending_bytes"], row["stopped"])
    cap = virtual_bench_capture.VirtualCapture({virtual_bench_capture.MODE["streaming"]}, {8}, 1, 1000)
    cap.mode, cap.width, cap.channels, cap.rate = virtual_bench_capture.MODE["streaming"], 8, 1, virtual_bench_capture.Fraction(1000)
    cap.samples, cap.generation = 1 << 16, 1                           # one w8 segment
    cap.data, cap.produced = bytearray(row["pending_bytes"]), row["pending_bytes"]
    frames = cap.pushes(9, lambda: 0, row["max_frame"]) if due else []
    assert [struct.unpack_from("<H", f, 13)[0] for f in frames] == row["expected_data_lengths"]
    assert all(len(f) <= row["max_frame"] for f in frames)


@pytest.mark.parametrize("row", BOUNDARIES["reception"], ids=lambda r: r["name"])
def test_boundary_reception_as_the_client_reads_it(row):
    """capture §2.1 rule 2: reaching write_pos is not having the whole; only this generation's bytes cover it."""
    from oep_client import capture
    assert capture.coverage([tuple(s) for s in row["spans"]], row["generation"], row["write_pos"]) == (
        row["expected_tail_reached"], row["expected_complete"])


@pytest.mark.parametrize("row", BOUNDARIES["serial_loss"], ids=lambda r: r["name"])
def test_boundary_serial_loss_as_the_client_counts_it(row):
    """common §1.3 paging 3, core §2.6: a forward serial jump in one stream counts the elements lost, nothing else does."""
    assert m.serials_lost(row["last_serial"], row["first_serial"], row["same_stream"]) == row["expected_lost"]


@pytest.mark.parametrize("row", BOUNDARIES["storage"], ids=lambda r: r["name"])
def test_boundary_storage_on_the_virtual_bench(row):
    """probe.config §3: a save / erase answers the same fixed part either way; state then shows the storage as seen
    after it (the save before kept, none, or unreadable), the settings and generation unchanged."""
    case = {"name": row["name"], "fns": {"8": "oep.probe.config"}, "state": ""}
    ep, hst, _ = bench_for(case)
    hst.open(3000)
    ep.load_config([config.item(config.Label(channel=k, text=f"l{k}")) for k in range(1, 5)], saved=False)
    ep.generation = row["generation"]
    ep.storage_state, ep.saved_generation, ep.storage_reason = row["before"]
    ep.saved = {} if row["before"][0] in (1, 3) else None
    if row["result"] == "failed":
        ep.storage_fault = row["observed"]
    for request_hex, answer_hex in ((row["request_hex"], row["answer_hex"]), ("0102000800" "15" "00000000", row["state_answer_hex"])):
        r = m.Request.unpack(hx(request_hex))
        ep.newest_corr, _ = (r.corr - 2) % 0xFFFF + 1, ep.resend.clear()
        sid = hst.session if r.session else 0
        out = ep.handle(m.Request(r.corr, r.fn, r.op, r.payload, sid).pack(), 0)
        assert out.hex() == answer_hex
    assert ep.generation == row["generation"]


@pytest.mark.parametrize("row", BOUNDARIES["groups"], ids=lambda r: r["name"])
def test_boundary_group_start_on_the_virtual_bench(row):
    """capture §4.1: start only when every track is in state 1/4/6 (else cause 6, nothing changes); a track failing to
    start makes the start completed failed with the same fixed part - the failed track 6, those not started 1, those
    started stopped as by stop - and no generation goes back."""
    from oep_client import capture
    case = {"name": row["name"], "fns": {"12": "oep.fixture.capture-group", "9": "oep.fixture.logic",
                                         "13": "oep.fixture.analog"}, "state": ""}
    ep, hst, _ = bench_for(case)
    hst.open(3000)
    logic, analog = capture.LogicCapture(hst, fn=9), capture.AnalogCapture(hst, fn=13)
    core.plan_apply(hst, [(9, 0, 1), (13, 0, 2)])
    logic.configure(rate=1_000_000, samples=1 if row.get("completed_first") else 100_000)
    analog.configure(rate=1000, samples=100)
    capture.CaptureGroup(hst, fn=12).bind([logic, analog])
    tracks = [ep.captures[9], ep.captures[13]]
    ep.groups[12].generation, tracks[0].generation, tracks[1].generation = row["generations"]
    for track, state in zip(tracks, row["states"]):
        track.state = state
    if "failed_index" in row:
        ep.capture_fail_start.add((9, 13)[row["failed_index"]])
    ep.now.t = 7                                                       # acquisition starts at 7 ms
    r = m.Request.unpack(hx(row["request_hex"]))
    ep.newest_corr, _ = (r.corr - 2) % 0xFFFF + 1, ep.resend.clear()
    assert ep.handle(m.Request(r.corr, r.fn, r.op, r.payload, hst.session).pack(), 0).hex() == row["answer_hex"]
    assert [ep.groups[12].generation] + [t.generation for t in tracks] == row["expected_generations"]
    assert [t.state for t in tracks] == row.get("expected_states", row["states"])


LIFECYCLE = load("core_lifecycle.json")["scenarios"]


@pytest.mark.parametrize("scenario", LIFECYCLE, ids=lambda c: c["name"])
def test_core_lifecycle_on_the_virtual_bench(scenario):
    """core §4.4 / §6.1 / §5.1 in steps on the bench's routes (core_lifecycle.json about: no replay byte cache, fn 3 an
    example lock-free read): a copy of a request still waiting or being answered is dropped (one credit, one
    execution); the owner's own processing does not count against its lease, an observer's does; a closed route drops
    what it had not carried out and leaves the session; a queued heartbeat does not revive an expired owner."""
    clock = type("Settable", (), {"t": 0, "__call__": lambda self: self.t})()
    ep = endpoint.Endpoint(vector_probe({"3": "io.example.slow-query"}), clock, boot_id=EXAMPLE_BOOT_ID, remember_count=0)
    for event in scenario["events"]:
        action = event["action"]
        if action == "receive":
            assert ep.receive(hx(event["request_hex"]), event["route"]) == event["expect"], event
        elif action == "process":
            ms = event["duration_ms"]
            out = m.Result.unpack(ep.process(event["route"], lambda: setattr(clock, "t", clock.t + ms)))
            if "reason" in event:
                assert out.resolution == m.REJECTED and out.detail == getattr(m, event["reason"].upper()), event
            else:
                assert out.resolution == m.COMPLETED, event
        elif action == "sent":
            ep.sent(event["route"])
        elif action == "close":
            ep.close_route(event["route"])
        elif action == "advance":
            clock.t += event["ms"]
        else:                                                           # check
            ep._lapse()
            if "owner" in event:
                assert (ep.holder or 0) == event["owner"], event
            if "remaining_ms" in event:
                assert (max(0, ep.expires_ms - clock.t) if ep.holder else 0) == event["remaining_ms"], event
            if "executions" in event:
                assert ep.executions == event["executions"], event
            if "credits" in event:
                assert {str(k): v for k, v in ep.credits().items()} == event["credits"], event


POLICY = load("core_policy.json")["scenarios"]


@pytest.mark.parametrize("case", POLICY, ids=lambda c: c["name"])
def test_core_policy_on_the_virtual_bench(case):
    """core §4.3 with an admission policy (optional; core_policy.json about: owner 0x12345678, last corr 12, 1000 ms of
    lease left, fn 2 an example read): the header first, then the policy - unavailable cause 7 with nothing changed and
    no remembered answer given out - and fn 0's confirm / list / describe / clock / lock_state never refused; an allowed
    replay is the remembered answer. The downstream answers are the vectors' opaque sentinels: only "not refused" is
    compared for them."""
    clock = type("Settable", (), {"t": 0, "__call__": lambda self: self.t})()
    ep = endpoint.Endpoint(vector_probe({"2": "io.example.query"}), clock, boot_id=EXAMPLE_BOOT_ID)
    owner = 0x12345678
    ep.holder = ep.last = owner
    ep.lease_ms, ep.expires_ms, ep.newest_corr = 5000, 1000, 12
    if "cache_response_hex" in case:
        ep.resend[12] = (hx(case["request_hex"]), hx(case["cache_response_hex"]))
    ep.policy = lambda req: case["allowed"]
    before = (ep.holder, ep.last, ep.expires_ms, ep.newest_corr, dict(ep.resend), ep.executions)
    out = ep.handle(hx(case["request_hex"]), 0)
    if "downstream_response_hex" in case:
        res = m.Result.unpack(out)                                      # the request too is a sentinel (confirm's)
        assert not (res.resolution == m.REJECTED and res.detail == m.UNAVAILABLE)   # not refused by the policy
    else:
        assert out.hex() == case["expected_response_hex"]
        assert (ep.holder, ep.last, ep.expires_ms, ep.newest_corr, dict(ep.resend), ep.executions) == before
    assert b"secret" not in out or case["allowed"]
