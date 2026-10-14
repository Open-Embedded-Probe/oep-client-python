"""Rules of oep-spec a09fa45 sections no other test names, on the client and the virtual bench: core §2.7 (one clock),
§12.1 (values a response may gain), capture §3.1 (plan roles) and §3.6 (extension boundary)."""

import struct

import pytest

from oep_client import capture, core, endpoint, host as h, message as m, registry as reg, virtual_bench


class Clock:
    t = 0

    def __call__(self):
        return self.t


def bench(profile=virtual_bench.p4_x035):
    ep = endpoint.Endpoint(profile(), Clock())
    hst = h.Host(lambda b: ep.handle(b, 0))
    hst.open(3000)
    return ep, hst


def test_core_2_7_one_clock_that_never_goes_back_and_all_ones_for_not_yet():
    """core §2.7: the probe's one clock, ns since boot (u64), never decreasing within a boot_id; a probe time that is not
    there yet is all ones (a capture-group never started)."""
    ep, hst = bench()
    readings = []
    for t in (0, 0, 5, 5, 40):
        ep.now.t = t
        readings.append(hst.clock())
    assert len({r.boot_id for r in readings}) == 1
    ups = [r.uptime_ns for r in readings]
    assert ups == sorted(ups) and ups[-1] > ups[0]
    group = capture.CaptureGroup(hst)
    raw = hst.request(group.fn, group.STATUS, b"", locked=False).payload
    assert struct.unpack_from("<Q", raw, 1)[0] == 0xFFFFFFFFFFFFFFFF and group.status().start_ns is None


@pytest.mark.parametrize("outcome", [3, 0x7F])
def test_core_12_1_an_outcome_this_host_does_not_know_is_a_failure(outcome):
    """core §12.1 (with §2.4): a response value added later is safe - an unknown outcome reads as a failure, never a
    success; the fixed part and the TLVs after it stay where they are."""
    res = m.Result(1, m.COMPLETED, outcome, b"\x01\x02")
    assert not res.succeeded
    hst = h.Host(lambda b: m.Result(m.Request.unpack(b).corr, m.COMPLETED, outcome, b"").pack())
    hst.revision = 1
    with pytest.raises(h.Failed):
        hst.call(0, m.OP_LOCK_STATE, locked=False)


def test_core_12_1_an_event_kind_this_host_does_not_know_is_kept_raw():
    frame = bytes([m.ROLE_EVENT]) + struct.pack("<HHB", 9, 0, 0x40) + b"\x01\x02\x03"
    e = capture.unpack_event(frame)
    assert e.kind == "kind 0x40" and e.raw == b"\x01\x02\x03" and e.generation is None


def test_capture_3_1_analog_roles_keep_their_numbers_and_a_pin_without_an_adc_is_refused():
    """capture §3.1: role k is channel k (analog 0-63, need not be dense; layout.order names the planned roles); a pin
    without an ADC channel is refused by the analog's plan."""
    ep, hst = bench()
    analog = capture.AnalogCapture(hst)
    core.plan_apply(hst, [(analog.fn, 1, 16), (analog.fn, 3, 17)])
    assert analog.configure(rate=1000, samples=8).order == [1, 3]
    with pytest.raises(h.Unsupported):
        core.plan_apply(hst, [(analog.fn, 0, 4)])                         # GPIO4: no ADC on this probe
    assert {a for a in ep.plan if a[0] == analog.fn} == {(analog.fn, 1, 16), (analog.fn, 3, 17)}


def test_capture_3_6_values_left_unused_are_refused_with_the_tag_as_received():
    """capture §3.6 (core §2.5, §12): a mode or trigger type left unused is refused unsupported with the TLV's tag as
    received, and an unknown critical configure TLV with its own; an unknown non-critical one is ignored."""
    ep, hst = bench()
    logic = capture.LogicCapture(hst)
    core.plan_apply(hst, [(logic.fn, 0, 20)])
    tlv = reg.FIXTURE_LOGIC.tlv["configure"]
    base = m.tlv(tlv["rate"], struct.pack("<I", 1_000_000)) + m.tlv(tlv["samples"], struct.pack("<I", 100))
    cases = [(m.tlv(tlv["mode"], b"\x07") + base, tlv["mode"]),
             (m.tlv(tlv["mode"], b"\x01") + base + m.tlv(tlv["trigger"], struct.pack("<BBI", 31, 0, 0)), tlv["trigger"]),
             (m.tlv(tlv["mode"], b"\x01") + base + m.tlv(0x7E, b"x", critical=True), 0xFE)]
    for body, tag in cases:
        with pytest.raises(h.Unsupported) as e:
            hst.call(logic.fn, logic.CONFIGURE, body)
        assert e.value.tag == tag
    assert hst.call(logic.fn, logic.CONFIGURE, m.tlv(tlv["mode"], b"\x01") + base + m.tlv(0x7E, b"x")).succeeded
