"""Sixth review, core §0-§4 and common: code divergences from oep-spec a09fa45 found and fixed.

- core §2.3: "応答、出来事、データの TLV で、値が定義の決める長さ（可変の部分を持つ値なら、その数と長さが決める長さ）を持たない
  ものは壊れている: host はそれを使わない" and "繰り返すと定義が言わない tag が 2 つ以上あれば、読む側は最初のものを使う" - the
  readers of confirm's refusal (supported), the wires' attach answers (target_id, dpc), step's step_left, capture's status
  error, configure answer and calibration took values of other lengths, and configure / calibration a later copy.
- common §1.3 (core §2.6): the stream checker held the span from next_serial, not from the newest kept mark, under a
  quarter of the space ("いちばん新しいものからいちばん古いものを引いた差は 2^(w−2)（幅の 4 分の 1）未満").
"""

from __future__ import annotations

import struct
from fractions import Fraction

import pytest

from oep_client import arm, capture as c, conformance, conformance_stream, host as h, message as m, registry as reg, riscv

u32 = lambda v: struct.pack("<I", v)   # noqa: E731


def completed(payload: bytes, outcome: int = m.SUCCESS) -> m.Result:
    return m.Result(1, m.COMPLETED, outcome, payload)


def answering(cls, result: m.Result, **attrs):
    """A client object of `cls` whose every call gets `result` (nothing goes out)."""
    obj = cls.__new__(cls)
    obj.host, obj.fn, obj.name, obj.prefix = None, 5, cls.NAME, b""
    obj._call = lambda *a, **k: result
    obj._request = lambda *a, **k: result
    for k, v in attrs.items():
        setattr(obj, k, v)
    return obj


# ---- core §2.3: a TLV of another length than its definition's is not used ---------------------------------------

def test_confirms_refusal_takes_supported_only_of_two_bytes():
    sup = reg.CORE.tlv["unsupported_payload"]["supported"]
    good = h.Unsupported(m.Result(1, m.REJECTED, m.UNSUPPORTED, b"\0" + m.tlv(sup, b"\x01\x02")))
    long = h.Unsupported(m.Result(1, m.REJECTED, m.UNSUPPORTED, b"\0" + m.tlv(sup, b"\x01\x02\x03")))
    assert good.supported == (1, 2)
    assert long.supported is None


def rvswd_attach(tail: bytes, flags: int) -> riscv.Wire:
    wire = answering(riscv.Wire, completed(struct.pack("<IIBI", 7, 0, flags, 1_000_000) + tail),
                     attach_body=lambda *a, **k: b"", attach_ms=lambda reset: 0)
    wire.attach()
    return wire


def test_an_attach_dpc_of_another_length_than_u32_is_not_used():
    halted = riscv.Wire.FLAGS["halted"]
    assert rvswd_attach(m.tlv(riscv.Wire.TAG_DPC, u32(0x80)), halted).dpc == 0x80
    for bad in (b"\x80\x00\x00", u32(0x80) + b"\x00"):
        assert rvswd_attach(m.tlv(riscv.Wire.TAG_DPC, bad), halted).dpc is None


def test_a_target_id_of_a_known_scheme_needs_its_u32():
    tag = riscv.Wire.TAG_TARGET_ID
    dmi = reg.COMMON.enum["target_id_scheme"]["dmi_7f"]
    assert rvswd_attach(m.tlv(tag, bytes([dmi]) + u32(0x1234)), 0).target_id == (dmi, u32(0x1234))
    for bad in (bytes([dmi]), bytes([dmi]) + b"\x34\x12", bytes([dmi]) + u32(0x1234) + b"\x00"):
        assert rvswd_attach(m.tlv(tag, bad), 0).target_id is None
    assert rvswd_attach(m.tlv(tag, b"\x7e\x01\x02"), 0).target_id == (0x7E, b"\x01\x02")   # unknown scheme: raw
    swd = answering(arm.SwdWire, completed(struct.pack("<IIBI", 7, 0x2BA01477, 0, 1_000_000)
                                           + m.tlv(arm.SwdWire.TAG_TARGET_ID, b"\x02\x77\x14")),
                    _attach_fixed=lambda *a: b"", _reset_tlv=lambda r: b"", attach_ms=lambda reset: 0)
    swd.attach()
    assert swd.target_id is None


def test_step_left_is_taken_only_with_its_length_0():
    state = riscv.STATUS["state"]
    fixed = struct.pack("<BBII", state, 0, 0, 0)
    for tail, left in ((m.tlv(riscv.RiscvDm.TAG_STEP_LEFT, b""), True), (m.tlv(riscv.RiscvDm.TAG_STEP_LEFT, b"\x01"), False),
                       (b"", False)):
        dm = answering(riscv.RiscvDm, completed(fixed + tail, m.FAILED), restore_data=lambda: None)
        with pytest.raises(riscv.StepError) as e:
            dm.step()
        assert e.value.step_left is left


def test_capture_status_error_is_taken_only_as_one_byte():
    fixed = struct.pack("<BIQBI", 6, 0, 0, 0, 3)
    good = answering(c.LogicCapture, completed(fixed + m.tlv(c.STATUS_ERROR, b"\x02"))).status()
    bad = answering(c.LogicCapture, completed(fixed + m.tlv(c.STATUS_ERROR, b"\x02\x00"))).status()
    assert good.error == 2 and bad.error is None


def answer(*items):
    return b"".join(m.tlv(t, v) for t, v in items)


def test_configure_answer_uses_the_first_copy_and_only_values_of_their_length():
    rate = struct.pack("<II", 1000, 1)
    cfg = c._config(answer((c.ACTUAL_RATE, rate), (c.ACTUAL_RATE, struct.pack("<II", 5, 1)),
                           (c.ACTUAL_SAMPLES, u32(10)), (c.ACTUAL_SAMPLES, u32(99)),
                           (c.LAYOUT, bytes([1, 2, 0, 1])), (c.LAYOUT, bytes([8, 1, 3]))), analog=False)
    assert (cfg.rate, cfg.samples, cfg.width, cfg.positions) == (Fraction(1000), 10, 1, [0, 1])
    # a broken first copy is not used, and no later copy stands in for it
    cfg = c._config(answer((c.ACTUAL_RATE, rate + b"\0"), (c.ACTUAL_RATE, rate),
                           (c.ACTUAL_SEGMENTS, b"\x02\x00"), (c.LAYOUT, bytes([1, 3, 0, 1]))), analog=False)
    assert (cfg.rate, cfg.segments, cfg.width, cfg.positions) == (Fraction(0), 0, 0, [])
    cfg = c._config(answer((c.LAYOUT, bytes([16, 0, 12, 1, 0, 9])), (c.FRONTEND_USED, b"\x00\x01\x02"),
                           (c.SCALE, struct.pack("<Bii", 0, 1, 2) + b"\0"), (c.SKEW, struct.pack("<BI", 0, 5)[:4]),
                           (c.REFERENCE, struct.pack("<BIB", 0, 3300, 1) + b"\0")), analog=True)
    assert cfg.order == [] and cfg.frontend == {} and cfg.scale_nv == {} and cfg.reference is None
    assert cfg.skew_ns == {}


def test_calibration_takes_only_whole_values_and_the_first_vrefint():
    factory = bytes([0, 3]) + b"a.b" + struct.pack("<H", 2) + b"\x01\x02"
    vref = struct.pack("<IQI", 1500, 77, 1200)
    cal = answering(c.AnalogCapture, completed(answer((c.FACTORY, factory), (c.FACTORY, factory + b"\0"),
                                                      (c.FACTORY, factory[:-1]),
                                                      (c.VREFINT, vref), (c.VREFINT, struct.pack("<IQI", 1, 2, 3)))))
    got = cal.calibration()
    assert got.factory == [(0, "a.b", b"\x01\x02")] and got.vrefint == (1500, 77) and got.vrefint_nominal_mv == 1200
    cal = answering(c.AnalogCapture, completed(answer((c.VREFINT, vref + b"\0"), (c.VREFINT, vref))))
    assert cal.calibration().vrefint is None


# ---- common §1.3 / core §2.6: the newest kept mark minus the oldest kept, under a quarter --------------------------

class _Adapter:
    adoption = {"mark_capacity": 5}

    def __init__(self, serials, next_serial):
        self.state = {"marks": [(s, 0, 3, 0, 0) for s in serials], "next_serial": next_serial}

    def stream_state(self):
        return self.state


def test_the_stream_checker_holds_the_span_of_the_marks_kept_not_from_next_serial():
    quarter = 1 << 30
    ok = conformance_stream.StreamChecks(None, _Adapter([0, quarter - 1], quarter))
    ok.retained_span()                                       # newest - oldest = 2^30 - 1 < 2^30: allowed
    over = conformance_stream.StreamChecks(None, _Adapter([0, quarter], quarter + 1))
    with pytest.raises(conformance.Violation):
        over.retained_span()


# ---- core §2.3: the checkers read the first copy of a tag that does not repeat ------------------------------------

def test_the_retention_checker_reads_the_first_owner_of_a_lock_state_answer():
    """A probe whose lock_state answer carries a later owner copy (a reader uses the first, core §2.3) passes."""
    import test_conformance_retention as tr
    now, model, adapter, runner = tr.build()
    core = model.ep.current_core
    handle = core.handle

    def doubled(data, transport, **kw):
        out = handle(data, transport, **kw)
        if out and data[0] == 1 and data[3:6] == b"\0\0\x13" and out[3:5] == b"\x01\0" and len(out) > 10:
            out += m.tlv(1, b"later")                            # a second owner after the first: not the one used
        return out
    core.handle = doubled
    report = runner.run()
    assert report["status"] == "passed", [(x["id"], x.get("error")) for x in report["checks"] if x["status"] != "passed"]
