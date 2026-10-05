"""The fake probe against the probe-side rules added since the 2026-10-02 rule changes: oep-spec 2e70f40 (required and
optional ops, C-21), 975d88c / 598bb26 / 8d91db0 (the lines while a wire does not answer, the rest states), 73a0c37
(cs_setup_ns, search_retries, boot_reset), and docs/v1-rule-change-proposal-2026-10-06.md (b4b08f1, 40291a4). Each test
names its item."""

import struct

import pytest

from oep_client import catalog, endpoint, fake, message as m, registry as reg

from test_fake_spec import ITEM, SPEED, Clock, Host, slot_item

CORE = reg.CORE
RV, CFG = reg.TARGET_RISCV_DM, reg.PROBE_CONFIG
LOGIC, GROUP, I2C = reg.FIXTURE_LOGIC, reg.FIXTURE_CAPTURE_GROUP, reg.FIXTURE_I2C_TARGET
UNA = CORE.tlv["unavailable_payload"]


def bench(probe=None, **kw):
    ep = endpoint.Endpoint(probe or fake.p4_bench(), Clock(), **kw)
    h = Host(ep)
    assert h.open().succeeded
    return ep, h


def fn_of(ep, name):
    return ep.fns[name]


def with_tlvs(probe: fake.FakeProbe, name: str, change) -> fake.FakeProbe:
    """The profile with the describe TLVs of every `name` fn passed through change(tlvs) -> tlvs."""
    return fake.FakeProbe(probe.label, probe.max_frame, [
        fake.Offered(o.fn, o.instance, o.name, tuple(change(o.tlvs)), o.revision, o.flags) if o.name == name else o
        for o in probe.offered])


def features(value: int):
    """change(): features set to `value` (added when missing)."""
    def change(tlvs):
        out = [t for t in tlvs if t[0] != catalog.FEATURES]
        return out + [catalog.u32(catalog.FEATURES, value)]
    return change


def una(r) -> dict[int, bytes]:
    out = {}
    for tag, v in m.split_tlvs(r.payload):
        out.setdefault(tag, v)
    return out


def attached(ep, h, wire=1, pair=(2, 3)):
    r = h.raw(wire, 0x02, b"\x01" + SPEED + m.tlv(0x03, struct.pack("<HH", *pair), critical=True))
    assert r.succeeded, r.describe()
    return struct.unpack_from("<H", r.payload)[0]


# ---- C-21 (2e70f40): required and optional ops; an undefined op is unknown_operation at order 1 ----------------

def test_c21_an_op_fn_0_does_not_define_is_unknown_operation_not_session_required():
    ep = endpoint.Endpoint(fake.p4_bench(), Clock())
    h = Host(ep)
    assert h.raw(0, 0x50, session=False).detail == m.UNKNOWN_OPERATION        # no session: still order 1's second
    assert h.raw(0, 0x50, session=False).detail == m.UNKNOWN_OPERATION
    assert h.raw(0, m.OP_KEEPALIVE, session=False).detail == m.SESSION_REQUIRED   # a defined op: its session check


def test_c21_an_op_an_interface_does_not_define_is_unknown_operation():
    ep = endpoint.Endpoint(fake.p4_bench(), Clock())
    h = Host(ep)
    for fn in (1, 2, 3, 4, 5, 6):                                             # wire, dm, console, gpio, uart, config
        assert h.raw(fn, 0x7E, session=False).detail == m.UNKNOWN_OPERATION, fn
    assert h.open().succeeded
    assert h.raw(1, 0x04).detail == m.UNKNOWN_OPERATION                       # 0x04 is reserved on the wires


def test_c21_riscv_dm_ops_are_gated_on_features():
    ep, h = bench(fake.esp32_v003())                                          # features 0b0111: no step
    dm = fn_of(ep, "oep.target.riscv-dm")
    assert not ep.offers(dm, RV.op["step"]) and ep.offers(dm, RV.op["run"])
    assert h.raw(dm, RV.op["step"], struct.pack("<H", 1)).detail == m.UNKNOWN_OPERATION
    ep, h = bench(with_tlvs(fake.p4_bench(), "oep.target.riscv-dm", features(0)))
    for op in ("read_block", "write_block", "run", "reset", "step"):
        assert h.raw(2, RV.op[op], struct.pack("<H", 1)).detail == m.UNKNOWN_OPERATION, op
    for op, body in (("dmi", struct.pack("<HH", 1, 0)), ("halt", b"\x01\x00"), ("resume", b"\x01\x00")):
        assert h.raw(2, RV.op[op], body).detail == m.NO_CONNECTION, op      # required: the connection is looked up


def test_c21_save_and_erase_without_storage_are_unknown_operation():
    probe = with_tlvs(fake.p4_bench(), "oep.probe.config",
                      lambda tlvs: [catalog.u32(CFG.tlv["describe"]["storage"], 0) if t[0] == CFG.tlv["describe"]["storage"]
                                    else t for t in tlvs])
    ep, h = bench(probe)
    for op in ("save", "erase"):
        assert h.raw(6, CFG.op[op]).detail == m.UNKNOWN_OPERATION
    assert h.raw(6, CFG.op["get"], struct.pack("<H", 0), session=False).succeeded
    ep, h = bench()
    assert h.raw(6, CFG.op["save"]).succeeded and h.raw(6, CFG.op["erase"]).succeeded


def test_c21_capture_query_and_force_need_their_features_bits():
    ep, h = bench(with_tlvs(fake.p4_x035(), "oep.fixture.logic", features(0b100)))   # notifications only
    logic = fn_of(ep, "oep.fixture.logic")
    assert h.raw(logic, LOGIC.op["query"], session=False).detail == m.UNKNOWN_OPERATION
    assert h.raw(logic, LOGIC.op["force"]).detail == m.UNKNOWN_OPERATION
    assert h.raw(logic, LOGIC.op["status"], session=False).succeeded
    ep, h = bench(with_tlvs(fake.p4_x035(), "oep.fixture.capture-group", features(0b100)))
    group = fn_of(ep, "oep.fixture.capture-group")
    assert h.raw(group, GROUP.op["force"]).detail == m.UNKNOWN_OPERATION
    ep, h = bench(fake.p4_x035())                                             # both declared
    assert h.raw(fn_of(ep, "oep.fixture.logic"), LOGIC.op["force"]).succeeded
    assert h.raw(fn_of(ep, "oep.fixture.capture-group"), GROUP.op["force"]).succeeded


def test_c21_order_1_refusals_are_not_remembered_and_do_not_restart_the_lease():
    ep, h = bench(fake.esp32_v003())
    dm = fn_of(ep, "oep.target.riscv-dm")
    ep.now.t = 2000
    r = h.raw(dm, RV.op["step"], struct.pack("<H", 1))
    assert r.detail == m.UNKNOWN_OPERATION and h.corr not in ep.resend
    assert ep.expires_ms == 3000                                              # the open's lease, not restarted
