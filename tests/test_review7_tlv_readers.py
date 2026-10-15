"""Review 7, one class across the codebase: readers of answer / event / data TLVs and of host input (core §2.2, §2.3).

core §2.3 (a09fa45): "応答、出来事、データの TLV で、値が定義の決める長さ（...）を持たないものは壊れている: host はそれを使わない。"
/ "繰り返すと定義が言わない tag が 2 つ以上あれば、読む側は最初のものを使う。後続の同じtagも上の検証対象である。"
/ "知らない TLV（この probe が実装しない tag）: critical なら、probe は要求を rejected unsupported（payload に受け取ったままの tag）で断る。"
"""
import struct
import types

import pytest

from oep_client import capture as c, core, dump, host as h, link, message as m, registry as reg
from oep_client.virtual_core import Reject, tlv

UNIT_ID = reg.CORE.tlv["describe"]["unit_id"]
TRANSPORT = reg.CORE.tlv["describe"]["transport"]


def a_host(describes):
    def send(_):
        raise AssertionError("no request goes out: every declaration is cached")
    hst = h.Host(send)
    hst._describes.update(describes)
    return hst


# ---- host.Unavailable: a broken later copy does not hide a good first one -------------------------------------------

def test_unavailable_uses_the_first_cause_whatever_a_later_copy_is():
    """core §2.3: "読む側は最初のものを使う" - the broken later copy is not used, and it does not hide the first."""
    una = h.Unavailable(m.Result(1, m.REJECTED, m.UNAVAILABLE, m.tlv(0x01, b"\x02") + m.tlv(0x01, b"\x02\x00")
                                 + m.tlv(0x05, b"\x07\x00") + m.tlv(0x05, b"\x07")))
    assert una.cause == "limit"
    assert una.fn == 7
    broken_first = h.Unavailable(m.Result(1, m.REJECTED, m.UNAVAILABLE, m.tlv(0x01, b"\x02\x00") + m.tlv(0x01, b"\x02")))
    assert broken_first.cause is None                         # a broken first copy: not used, no later one stands in


# ---- unit_id: text of 1 to 32 bytes (core §7.5); another length is broken, not used (core §2.3) ---------------------

def test_a_unit_id_over_32_bytes_is_not_used():
    long = b"a" * 33
    assert core.unit_id_value([(UNIT_ID, long)]) is None
    assert core.unit_id_value([(UNIT_ID, long), (UNIT_ID, b"abc")]) is None     # no later copy stands in
    assert core.unit_id_value([(UNIT_ID, b"abc"), (UNIT_ID, long)]) == b"abc"
    hst = a_host({0: [(UNIT_ID, long)]})
    closed = []
    hst.link = types.SimpleNamespace(close=lambda: closed.append(True))
    with pytest.raises(link.UnitIdMismatch):
        link.check_unit_id(hst, "a" * 33)
    assert closed
    assert link._unit_id(a_host({0: [(UNIT_ID, long)]})) == "?"


def test_the_hardware_runner_reads_unit_id_through_its_defined_length():
    import inspect
    from oep_client import hardware
    src = inspect.getsource(hardware)
    assert "values.get(virtual_bench.CORE_UNIT_ID" not in src and "core.unit_id_value(" in src


# ---- dump.required_missing: confirm's transport u8, describe's transport 3 bytes ------------------------------------

def _confirm(transport_value):
    return struct.pack("<4sBHIBI", b"OEP!", 1, 64, 64, 1, 7) + m.tlv(reg.CORE.tlv["confirm_answer"]["transport"],
                                                                     transport_value)


def test_required_missing_does_not_use_broken_transport_values():
    from oep_client import catalog
    ops = (m.TAG_OPS, catalog.pack_ops(set(reg.CORE.op.values())))
    good = [(UNIT_ID, b"abc"), (reg.CORE.tlv["describe"]["max_op_ms"], struct.pack("<I", 1000)), ops]
    assert dump.required_missing(_confirm(b"\x00"), good + [(TRANSPORT, bytes([0, 6, 0xFF]))]) == []
    # a describe transport of 1 byte names no index: confirm's index 0 has no entry
    missing = dump.required_missing(_confirm(b"\x00"), good + [(TRANSPORT, b"\x00")])
    assert "describe of fn 0: the transport confirm names (index 0)" in missing
    # confirm's transport of 2 bytes is broken: not given
    assert "confirm's transport TLV" in dump.required_missing(_confirm(b"\x00\x00"), good + [(TRANSPORT, bytes([0, 6, 0xFF]))])


# ---- the TLV-request model checks a later copy too ------------------------------------------------------------------

def test_tlv_request_model_checks_a_later_copy():
    from oep_client.virtual_tlv_request_model import TlvRequests
    e = TlvRequests()
    with pytest.raises(Reject) as r:
        e.request_tags(1, 16, m.tlv(1, b"\x07") + m.tlv(1, b"", critical=True))
    assert r.value.reason == 3
    with pytest.raises(Reject) as r:
        e.request_tags(2, 17, m.tlv(1, b"\x01") + m.tlv(1, b"\x02", critical=True))
    assert (r.value.reason, r.value.payload) == (11, b"\x81")
    assert e.request_tags(1, 16, m.tlv(1, b"\x07") + m.tlv(1, b"\x09"))[1] == b"\x07"


def test_tlv_request_checker_fails_a_model_that_skips_later_copies():
    from test_conformance_tlv_requests import build
    model, _, checker = build()
    parse = model.extension.request_tags

    def first_only(fn, op, raw):
        out, seen = b"", False
        try:
            rows = m.split_tlvs(raw)
        except Exception:
            return parse(fn, op, raw)                    # a broken tail: the model's own refusal
        for tag, value in rows:
            if tag & 0x7F == 1:
                if seen:
                    continue
                seen = True
            out += bytes([tag]) + struct.pack("<H", len(value)) + value
        return parse(fn, op, out)
    model.extension.request_tags = first_only
    report = checker.run()
    row = next(r for r in report["checks"] if r["id"] == "CORE-TLV-REQUEST-DUPLICATES")
    assert row["status"] == "failed"


# ---- capture models: every copy of an unknown tag counts; later copies are checked for their values ---------------

def _logic():
    from oep_client.virtual_logic_model import Logic
    return Logic.__new__(Logic)


def _analog():
    from oep_client.virtual_analog_model import Analog
    return Analog.__new__(Analog)


@pytest.mark.parametrize("which", ["logic", "analog", "timeline", "group"])
def test_models_refuse_a_critical_unknown_tag_after_a_non_critical_copy(which):
    unknown = tlv(0x7E, b"x") + tlv(0xFE, b"x")
    if which == "timeline":
        from oep_client.virtual_capture_timeline_model import Track
        parse = lambda data: Track(2, None, []).request_tags(16, data)         # noqa: E731
    elif which == "group":
        from oep_client.virtual_capture_group_model import CaptureGroupModel
        e = CaptureGroupModel(boot_id=17).extension
        parse = lambda data: e.request_tags(4, 16, data)                       # noqa: E731
    else:
        e = _logic() if which == "logic" else _analog()
        parse = lambda data: e.request_tags(2, 16, data)                       # noqa: E731
    with pytest.raises(Reject) as r:
        parse(unknown)
    assert (r.value.reason, r.value.payload) == (11, b"\xfe")


def test_logic_checker_runs_raw_tags_and_first_wins_on_the_models():
    from test_conformance_logic import setup
    from oep_client.conformance_logic import LogicChecks
    from oep_client.conformance_logic_sample import SampleLogicAdapter
    model, checks = setup()
    checker = LogicChecks(checks, SampleLogicAdapter(model))
    checker.cases = ("RAW-TAGS", "FIRST-WINS")
    report = checker.run()
    assert report["status"] == "passed", [(x["id"], x.get("error")) for x in report["checks"]]


def test_logic_checker_fails_a_model_that_does_not_check_later_copies(monkeypatch):
    from test_conformance_logic import setup
    from oep_client import virtual_logic_model
    from oep_client.conformance_logic import LogicChecks
    from oep_client.conformance_logic_sample import SampleLogicAdapter
    original = virtual_logic_model.Logic.settings
    monkeypatch.setattr(virtual_logic_model.Logic, "settings",
                        lambda self, tags: original(self, {k: v for k, v in tags.items() if k != "later"}))
    model, checks = setup()
    checker = LogicChecks(checks, SampleLogicAdapter(model))
    checker.cases = ("FIRST-WINS",)
    assert checker.run()["status"] == "failed"


def test_logic_checker_fails_a_model_that_skips_a_repeated_unknown_tag(monkeypatch):
    from test_conformance_logic import setup
    from oep_client import virtual_logic_model
    from oep_client.conformance_logic import LogicChecks
    from oep_client.conformance_logic_sample import SampleLogicAdapter
    original = virtual_logic_model.Logic.request_tags

    def first_copy_only(self, fn, op, data):
        if fn != 2:
            return original(self, fn, op, data)          # the plan's role assignments repeat
        out, seen = b"", set()
        try:
            rows = m.split_tlvs(data)
        except Exception:
            return original(self, fn, op, data)          # a broken tail: the model's own refusal
        for tag, value in rows:
            if tag & 0x7F in seen:
                continue
            seen.add(tag & 0x7F)
            out += bytes([tag]) + struct.pack("<H", len(value)) + value
        return original(self, fn, op, out)
    monkeypatch.setattr(virtual_logic_model.Logic, "request_tags", first_copy_only)
    model, checks = setup()
    checker = LogicChecks(checks, SampleLogicAdapter(model))
    checker.cases = ("RAW-TAGS",)
    assert checker.run()["status"] == "failed"


@pytest.mark.parametrize("which", ["analog", "timeline"])
def test_other_capture_models_check_later_copies(which):
    good = tlv(64, b"\1") + tlv(66, struct.pack("<I", 1000)) + tlv(67, struct.pack("<I", 8))
    if which == "timeline":
        from oep_client.virtual_capture_timeline_model import Track
        track = Track(2, None, [])
        track.roles = [0]
        parse, settle = (lambda data: track.request_tags(16, data)), track.settings
    else:
        e = _analog()
        e.roles = [1, 3]
        parse, settle = (lambda data: e.request_tags(2, 16, data)), e.settings
    settle(parse(good + tlv(0xC2, struct.pack("<I", 500))))                   # a later copy the probe handles: ignored
    with pytest.raises(Reject) as r:
        settle(parse(good + tlv(0xC2, struct.pack("<I", 1))))
    assert (r.value.reason, r.value.payload) == (11, b"\xc2")
    with pytest.raises(Reject) as r:
        settle(parse(good + tlv(66, struct.pack("<I", 0))))
    assert r.value.reason == 3


# ---- the bench: a later copy of a capture configure TLV is checked for its value too ---------------------------------

def _ask(hst, fn, op, payload):
    try:
        return hst.request(fn, op, payload)
    except h.Rejected as e:
        return e.result


def _configure(rate=1000):
    return (m.tlv(c.MODE, bytes([c.ONE_SHOT])) + m.tlv(c.RATE, struct.pack("<I", rate))
            + m.tlv(c.SAMPLES, struct.pack("<I", 8)))


@pytest.mark.parametrize("later, expected", [
    (m.tlv(c.RATE, struct.pack("<I", 0), critical=True), (m.REJECTED, m.MALFORMED, b"")),
    (m.tlv(c.SAMPLES, struct.pack("<I", 0)), (m.REJECTED, m.MALFORMED, b"")),
    (m.tlv(c.RATE, struct.pack("<I", 0xFFFFFFFF), critical=True), (m.REJECTED, m.UNSUPPORTED, bytes([c.RATE | 0x80]))),
    (m.tlv(c.MODE, b"\x09", critical=True), (m.REJECTED, m.UNSUPPORTED, bytes([c.MODE | 0x80]))),
    (m.tlv(c.TRIGGER, struct.pack("<BBI", c.LEVEL, 0, 2)), (m.REJECTED, m.UNSUPPORTED, bytes([c.TRIGGER]))),
], ids=["rate-0", "samples-0", "rate-out-of-range", "mode-undefined", "trigger-value-undefined"])
def test_bench_checks_a_later_copy_of_a_configure_tlv(later, expected):
    from test_virtual_bench_capture import bench
    ep, hst, lc, clock = bench()
    core.plan_apply(hst, [(lc.fn, 0, 20), (lc.fn, 1, 21)])
    payload = _configure()
    if later[0] & 0x7F == c.TRIGGER:
        payload += m.tlv(c.TRIGGER, struct.pack("<BBI", c.LEVEL, 0, 1))
    r = _ask(hst, lc.fn, lc.CONFIGURE, payload + later)
    assert (r.resolution, r.detail, r.payload) == expected
    ok = _ask(hst, lc.fn, lc.CONFIGURE, _configure() + m.tlv(c.RATE, struct.pack("<I", 5000), critical=True))
    assert ok.resolution == m.COMPLETED and ok.detail == m.SUCCESS


def test_analog_checker_reads_the_first_vrefint_copy(monkeypatch):
    """vrefint does not repeat (capture §3.3): a later copy after the good first one is not used (core §2.3)."""
    from test_conformance_analog import setup
    from oep_client import virtual_analog_model
    from oep_client.conformance_analog import AnalogChecks
    from oep_client.conformance_analog_sample import SampleAnalogAdapter
    original = virtual_analog_model.Analog.dispatch_request

    def with_later_copy(self, fn, op, payload, tags):
        out = original(self, fn, op, payload, tags)
        if fn == 2 and op == 25 and self.calibration_ns is not None:
            out += tlv(2, struct.pack("<IQI", 1, 0, 1))
        return out
    monkeypatch.setattr(virtual_analog_model.Analog, "dispatch_request", with_later_copy)
    model, checks = setup()
    checker = AnalogChecks(checks, SampleAnalogAdapter(model))
    checker.cases = ("CALIBRATION",)
    report = checker.run()
    assert report["status"] == "passed", [(x["id"], x.get("error")) for x in report["checks"]]
