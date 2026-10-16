"""Fourth review of capture (oep-spec a09fa45 interfaces/oep-if-capture.ja.md): divergences found in the bench, the
client and the independent analog checker/models, each pinned to the spec sentence it breaks."""
import struct

import pytest

from oep_client.registry import FIXTURE_LOGIC as _LOGIC
STOPPED_ERROR = _LOGIC.enum['stopped_reason']['error']   # capture §3.4, by name
from oep_client import capture as c, core, host as h, message as m, registry as reg
from test_virtual_bench_capture import bench

ST = c.STATE
CAUSE = reg.CORE.tlv["unavailable_payload"]["cause"]


def ask(hst, fn, op, payload=b""):
    """The probe's answer, a rejection included."""
    try:
        return hst.request(fn, op, payload)
    except h.Rejected as e:
        return e.result


def configure_payload(mode=c.ONE_SHOT, rate=1000, samples=8, trigger=None):
    body = m.tlv(c.MODE, bytes([mode])) + m.tlv(c.RATE, struct.pack("<I", rate))
    if samples is not None:
        body += m.tlv(c.SAMPLES, struct.pack("<I", samples))
    if trigger is not None:
        body += m.tlv(c.TRIGGER, struct.pack("<BBI", *trigger))
    return body


def events(ep, fn):
    return [c.unpack_event(f) for f in ep.pushes() if f[0] == m.ROLE_EVENT and struct.unpack_from("<H", f, 1)[0] == fn]


# ---- §3.3: "trigger の type が 0 でなく、その role がその fn の plan に無ければ rejected unavailable（cause 6）" ----
def test_logic_trigger_role_is_the_planned_role_number():
    """A logic plan of roles 0 and 2: role 2 is in the plan (channel 1, the second smallest role, §1.1), role 1 is not
    (refused cause 6); the trigger watches role 2's channel."""
    ep, hst, lc, clock = bench()
    core.plan_apply(hst, [(lc.fn, 0, 20), (lc.fn, 2, 21)])
    r = ask(hst, lc.fn, lc.CONFIGURE, configure_payload(trigger=(c.LEVEL, 1, 1)))
    assert r.resolution == m.REJECTED and r.detail == m.UNAVAILABLE
    assert dict(m.split_tlvs(r.payload))[CAUSE] == bytes([reg.CORE.enum["unavailable_cause"]["wrong_state"]])
    lc.configure(rate=1000, samples=8, trigger=(c.LEVEL, 2, 1))
    lc.start()
    t0 = ep.captures[lc.fn].t0_ns
    clock.t = 40
    (seg,) = lc.wait()
    assert seg.trigger_index == 0 and seg.start_ns - t0 == 2_000_000   # channel 1 = (i >> 1) & 1 is 1 first at i 2


# ---- §3.3: "1 レベル（value 0 / 1）、2 エッジ（value 0 立ち上がり / 1 立ち下がり / 2 両方）"; core §2.3: "定義が使わずに
# 残した値と、この probe が扱わない値は rejected unsupported（受け取ったままの tag）" ----
@pytest.mark.parametrize("trigger", [(c.LEVEL, 0, 2), (c.LEVEL, 0, 0x100), (c.EDGE, 0, 3)])
def test_logic_trigger_value_left_undefined_is_unsupported(trigger):
    ep, hst, lc, clock = bench()
    core.plan_apply(hst, [(lc.fn, 0, 20), (lc.fn, 1, 21)])
    lc.configure(rate=1000, samples=8)
    before = lc.status()
    r = ask(hst, lc.fn, lc.CONFIGURE, configure_payload(trigger=trigger))
    assert (r.resolution, r.detail, r.payload) == (m.REJECTED, m.UNSUPPORTED, bytes([c.TRIGGER]))
    assert lc.status() == before


def test_analog_threshold_past_the_value_bits_is_unsupported():
    """§3.3: the threshold "value は o / b で切り出した後の ADC の値" - one past 2^b - 1 is no ADC value."""
    ep, hst, lc, clock = bench()
    an = c.AnalogCapture(hst)
    core.plan_apply(hst, [(an.fn, 1, 16), (an.fn, 3, 17)])
    r = ask(hst, an.fn, an.CONFIGURE, configure_payload(trigger=(c.CROSS_UP, 1, 4096)))
    assert (r.resolution, r.detail, r.payload) == (m.REJECTED, m.UNSUPPORTED, bytes([c.TRIGGER]))
    an.configure(rate=1000, samples=8, trigger=(c.CROSS_UP, 1, 4095))


# ---- §3.2: "`serial_done` は次に終わる区画の serial（start から終わった区画の数...）"; "3 取得中 | ... | → 1（stopped 1、
# 短い区画 bit1）" ----
def test_streaming_stop_counts_the_short_last_segment_as_finished():
    ep, hst, lc, clock = bench()
    core.plan_apply(hst, [(lc.fn, 0, 20)])
    lc.configure(rate=1000, mode=c.STREAMING)
    lc.subscribe()
    lc.start()
    clock.t = 20
    st = lc.status()
    assert st.state == ST["capturing"] and st.serial_done == 0 and st.write_pos > 0
    lc.stop()
    st = lc.status()
    assert st.state == ST["configured"] and st.serial_done == 1


# ---- §3.2 エラー: "トラックが state 6 に入るのは取得の失敗...のときだけで、そのときは理由によらず必ず出来事 stopped を
# reason 3 と、status の error と同じ error で送る（購読していれば）"; §3.2 "受理したstartで初期化/開始が失敗した場合は
# state 6とerrorを設定し" ----
def test_a_failed_start_sends_stopped_reason_3():
    ep, hst, lc, clock = bench()
    core.plan_apply(hst, [(lc.fn, 0, 20)])
    lc.configure(rate=1000, samples=8)
    lc.subscribe()
    ep.capture_fail_start = {lc.fn}
    r = ask(hst, lc.fn, lc.START)
    assert (r.resolution, r.detail) == (m.COMPLETED, m.FAILED)
    st = lc.status()
    assert st.state == ST["error"]
    assert [(e.kind, e.reason, e.error, e.generation) for e in events(ep, lc.fn)] == \
        [("stopped", STOPPED_ERROR, st.error, st.generation)]


def test_a_failed_group_start_sends_the_group_stopped_reason_3():
    """§4.1: "初期化/取得の失敗でtrackがstate 6になれば組も6とし"; §4.2: the group's stopped "reason(u8: ... 3 error)、
    error(u8: reason3のtrack error ...)"."""
    from test_trace_capture import grouped
    ep, hst, clock, lc, an, grp = grouped()
    hst.subscribe(grp.fn)
    hst.subscribe(lc.fn)
    ep.capture_fail_start = {lc.fn}
    grp.bind([an, lc])
    r = hst.request(grp.fn, grp.START, b"")
    assert (r.resolution, r.detail) == (m.COMPLETED, m.FAILED)
    gen = struct.unpack_from("<QI", r.payload)[1]
    error = lc.status().error
    assert [(e.kind, e.reason, e.error, e.generation) for e in events(ep, grp.fn)] == [("stopped", STOPPED_ERROR, error, gen)]


# ---- §3.2 state table: "| 5 止まっている | unavailable 6 | unavailable 6 | → 1 | ..." (no stopped, as rows 2 and 3
# have; §3.4 stopped: "取得が止まった") ----
def test_stop_in_state_5_sends_no_stopped():
    ep, hst, lc, clock = bench()
    core.plan_apply(hst, [(lc.fn, 0, 20)])
    lc.configure(rate=1000, mode=c.REPEAT, samples=8, segments=1)
    lc.subscribe()
    lc.start()
    clock.t = 30
    assert lc.status().state == ST["paused"]
    events(ep, lc.fn)
    lc.stop()
    assert lc.status().state == ST["configured"]
    assert events(ep, lc.fn) == []


def test_group_stop_of_paused_tracks_still_says_stopped():
    """§4.1: the group is 3 while a track is 3 / 5; its stop ends it (§4.2 stopped reason 1)."""
    from test_trace_capture import grouped
    ep, hst, clock, lc, an, grp = grouped(mode=c.REPEAT)
    hst.subscribe(grp.fn)
    grp.bind([lc, an])
    grp.start([lc, an])
    clock.t = 1000
    assert grp.status().state == ST["capturing"]
    assert {lc.status().state, an.status().state} == {ST["paused"]}
    events(ep, grp.fn)
    grp.stop()
    assert [(e.kind, e.reason) for e in events(ep, grp.fn)] == [("stopped", 1)]


# ---- §3.2: "readの要求位置が使い回された/押し出された場合、次の読める位置へ進めてgapを立てる" ----
def test_client_read_does_not_take_bytes_of_another_position():
    """The client's read() returns the bytes at the positions asked for: an answer that moved on (gap) is not those
    bytes - it raises (h.ProtocolError subclass c.ReadGap) with where the readable data starts."""
    ep, hst, lc, clock = bench()
    core.plan_apply(hst, [(lc.fn, 0, 20)])
    lc.configure(rate=1000, mode=c.REPEAT, samples=8, segments=2)
    lc.start()
    clock.t = 30
    lc.release(0)
    clock.t = 40
    first = lc.segments()[0]
    assert first.position > 0
    with pytest.raises(c.ReadGap) as e:
        lc.read(0, 1)
    assert e.value.position == first.position
    assert lc.read(first.position, 1) == ep.captures[lc.fn].data[:1]


# ---- the independent analog checker and its models ----
def _analog_model():
    from test_conformance_analog import setup
    return setup()


def test_analog_checker_requires_unsupported_for_a_frontend_of_a_role_not_planned(monkeypatch):
    """§3.3 lists no refusal for a frontend naming a role outside the plan (the trigger's role is the only one refused
    cause 6); "挙げていない断り: ... probe は、自分が扱えないほかの値と値の組み合わせ ... も、core §2.3 のとおり rejected
    unsupported で断る。tag はその値を持つ TLV の受け取ったままのtag". The model answers so, and the checker fails a
    probe answering unavailable cause 6."""
    from oep_client.conformance_analog import AnalogChecks
    from oep_client.conformance_analog_sample import SampleAnalogAdapter
    from oep_client.virtual_core import Reject, tlv
    from oep_client import virtual_analog_model
    model, checks = _analog_model()
    checker = AnalogChecks(checks, SampleAnalogAdapter(model))
    checker.cases = ("FRONTEND",)
    assert checker.run()["status"] == "passed"
    original = virtual_analog_model.Analog.settings

    def old_rule(self, tags):
        if any(value[0] not in self.roles for _, value in tags.get(virtual_analog_model.C['frontend'], [])):
            raise Reject(virtual_analog_model.R['unavailable'], virtual_analog_model.cause('wrong_state'))
        return original(self, tags)
    monkeypatch.setattr(virtual_analog_model.Analog, "settings", old_rule)
    model, checks = _analog_model()
    checker = AnalogChecks(checks, SampleAnalogAdapter(model))
    checker.cases = ("FRONTEND",)
    result = checker.run()
    assert next(r for r in result["checks"] if r["id"] == "OEP-ANALOG-FRONTEND")["status"] == "failed"


@pytest.mark.parametrize("model_name", ["logic", "analog", "timeline"])
def test_models_check_a_later_copy_of_a_configure_tlv(model_name):
    """core §2.3 (capture §3.3's configure TLVs follow it): "後続の同じtagも上の検証対象である。例えば、正常な既知TLVの
    後に同じtagで不正な長さの値があれば、要求全体をmalformedで断る。"."""
    if model_name == "timeline":
        from oep_client.virtual_capture_timeline_model import Track
        track = Track(2, None, [])
        tags = lambda data: track.request_tags(_LOGIC.op["configure"], data)    # noqa: E731
    else:
        from oep_client.virtual_logic_model import Logic
        from oep_client.virtual_analog_model import Analog
        ext = (Logic if model_name == "logic" else Analog).__new__(Logic if model_name == "logic" else Analog)
        tags = lambda data: ext.request_tags(2, _LOGIC.op["configure"], data)   # noqa: E731
    from oep_client.virtual_core import Reject, tlv
    C = _LOGIC.tlv["configure"]
    good = tlv(C["mode"], bytes([_LOGIC.enum["mode"]["one_shot"]])) + tlv(C["rate"], struct.pack("<I", 1000))
    tags(good + tlv(C["rate"], struct.pack("<I", 5)))                          # a well-formed later copy: ignored
    with pytest.raises(Reject) as e:
        tags(good + tlv(C["rate"], b"\5"))
    assert e.value.reason == __import__("oep_client.registry", fromlist=["REJECT_REASONS"]).REJECT_REASONS["malformed"]
