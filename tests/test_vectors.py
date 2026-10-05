"""oep-spec's test vectors (tests/vectors/*.json, copied here by tools/sync_registry.sh, never edited) against this
client's own code: COBS and serial frames (cobs), headers and TLVs (message), confirm (host and the fake), the CRCs,
probe.config's canonical form and hash (config and the fake), and the refusals - each request sent to the fake probe
with the vector's fn numbers, its answer compared byte for byte. Where a vector and this code disagree, the spec's text
decides (core §0 rule 4) and the vector is the one the spec corrects."""

import json
import struct
import zlib
from pathlib import Path

import pytest

from oep_client import catalog, cobs, config, endpoint, fake, host as h, message as m, registry as reg

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


# ---- COBS and serial frames (core §3.1) --------------------------------------------------------------------------

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
    assert m.tlv(case["tag"], value).hex() == case["tlv_hex"]
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

def vector_probe(fns: dict, max_frame: int = 1024) -> fake.FakeProbe:
    """A fake probe whose fn numbers are a vector's: fn 0 with one UART bridge (index 0), then each named interface
    with channels 0-15 for its roles (the wire on pins 1 / 2)."""
    core = fake._core("1.0.0", "vectors", "0123456789ab", 16, [], "", {},
                      fake._transports([(fake.TRANSPORT["uart_bridge"], 0xFF)]))
    chans = list(range(16))
    offered = [core]
    for fn, name in sorted(((int(k), v) for k, v in fns.items())):
        if name == "oep.fixture.gpio":
            offered.append(fake._gpio(fn, chans))
        elif name == "oep.fixture.i2c-target":
            offered.append(fake._i2c_target(fn, chans, max_length=16, max_hz=100_000, features=0, queue_depth=4))
        elif name == "oep.wire.rvswd":
            offered.append(fake.Offered(fn, 0, name, (catalog.channel_group(1, [(1, 1), (2, 2)]),
                                                      catalog.u32(catalog.MAX_CLOCK_HZ, 4_000_000))))
        elif name == "oep.fixture.uart":
            offered.append(fake._uart(fn, 0, chans, 3_000_000))
        elif name == "oep.target.riscv-dm":                            # every optional op declared (core §1.2)
            offered.append(fake.Offered(fn, 0, name, (catalog.u32(catalog.FEATURES, 0b1111),
                                                      catalog.u16(catalog.MAX_LENGTH, 256))))
        else:
            raise AssertionError(f"a vector names {name}: add it here")
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
    hst.open(3000, session=SESSION)
    gpio = next((int(k) for k, v in fns.items() if v == "oep.fixture.gpio"), None)
    if gpio is not None:                                                # "channel 3 is in fn 2's plan"
        hst._corr = 0
        ep.handle(m.Request(1, 0, m.OP_PLAN_APPLY, m.tlv(0x10, struct.pack("<HBH", gpio, 1, 3), critical=True),
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
