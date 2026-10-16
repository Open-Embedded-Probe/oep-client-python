"""Fifth review of capture (oep-spec a09fa45 interfaces/oep-if-capture.ja.md): divergences found in the bench, the
client and the independent logic/analog models, each pinned to the spec sentence it breaks."""
import struct

import pytest

from oep_client import capture as c, core, host as h, message as m
from oep_client.virtual_logic_model import LogicModel
from oep_client.virtual_analog_model import AnalogModel
from test_virtual_bench_capture import bench

ST = c.STATE
SHORT = 0x02


# ---- §2.2 flags: "bit1 短い（stop で終わった）"; §3.2 state table "3 取得中 | ... | → 1（stopped 1、短い区画 bit1）" ----
@pytest.mark.parametrize("mode", [c.ONE_SHOT, c.REPEAT])
def test_an_early_trigger_segment_is_short_without_bit1(mode):
    """§3.3: a trigger too early for the whole pretrigger makes the segment short - not ended by stop, so flags bit1
    stays clear; a segment that stop ends has it."""
    ep, hst, lc, clock = bench()
    core.plan_apply(hst, [(lc.fn, r, 20 + r) for r in range(4)])
    lc.configure(rate=1000, mode=mode, samples=16, segments=4 if mode == c.REPEAT else None,
                 trigger=(c.EDGE, 1, 0), pretrigger=4)                     # bit 1 rises at sample 2
    lc.start()
    clock.t = 40
    first = lc.segments()[0]
    assert (first.samples, first.trigger_index) == (14, 2)
    assert not first.flags & SHORT
    if mode == c.REPEAT:
        clock.t = 45
        lc.stop()
        assert lc.segments()[-1].flags & SHORT                               # the one stop ended


# ---- §3.2 state table: release column "| 1 設定済み | ... | — |", "| 6 エラー | ... | — |" ("「—」は何もせず成功");
# only "| 3 取得中 | ... | 空きを返す |" and "| 5 止まっている | ... | 空きができれば → 3 |" free segments ----
@pytest.mark.parametrize("model_class", [LogicModel, AnalogModel])
@pytest.mark.parametrize("end", ["stop", "error"])
def test_model_release_in_state_1_or_6_does_nothing(model_class, end):
    model = model_class(boot_id=17)
    e = model.extension
    roles = (0, 1) if model_class is LogicModel else (1, 3)
    e.roles, e.plans[2] = list(roles), [0, 1]
    from oep_client import registry as R
    C, OP, ST, MODE = R.FIXTURE_ANALOG.tlv["configure"], R.FIXTURE_ANALOG.op, R.FIXTURE_LOGIC.enum["state"], R.FIXTURE_LOGIC.enum["mode"]
    tags = {C["mode"]: (C["mode"], bytes([MODE["repeat"]])), C["rate"]: (C["rate"], struct.pack("<I", 1000)),
            C["samples"]: (C["samples"], struct.pack("<I", 8)), C["segments"]: (C["segments"], struct.pack("<I", 2))}
    if model_class is AnalogModel:
        tags[C["frontend"]] = []
    e.dispatch_request(2, OP["configure"], b"", tags)
    generation = struct.unpack("<I", e.dispatch_request(2, OP["start"], b"", {}))[0]
    e.feed([[i & 1, (i >> 1) & 1] for i in range(12)])                   # one segment and 4 samples more
    if end == "stop":
        e.dispatch_request(2, OP["stop"], b"", {})
        assert e.state == ST["configured"] and len(e.segments) == 2
    else:
        (model.logic_fail if model_class is LogicModel else model.analog_fail)(R.FIXTURE_LOGIC.enum["error"]["storage"])
        assert e.state == ST["error"]
    rows, data, base = list(e.segments), bytes(e.data), e.base
    assert e.dispatch_request(2, OP["release"], struct.pack("<II", generation, 1), {}) == b""
    assert (list(e.segments), bytes(e.data), e.base) == (rows, data, base)


# ---- §3.2 "受理したstartで初期化/開始が失敗した場合はstate 6とerrorを設定し、completed failedで同じgenerationの固定形を
# 返す"; §0 "host は read と release に世代を付け、probe は違えば断る" ----
def test_client_takes_the_generation_of_a_failed_start():
    ep, hst, lc, clock = bench()
    core.plan_apply(hst, [(lc.fn, 0, 20)])
    lc.configure(rate=1000, samples=8)
    lc.start()
    clock.t = 20
    first = lc.generation
    ep.capture_fail_start = {lc.fn}
    with pytest.raises(h.Failed):
        lc.start()
    assert lc.generation == first + 1 == lc.status().generation
    assert lc.read(0, 0) == b""                                             # the current generation: not refused


# ---- §4.1 "start自体が開始に失敗した場合はcompleted failedで同じ固定形を返す。...開始済みのtrackは§3.2のstop後のstate" ----
def test_client_takes_the_generations_of_a_failed_group_start():
    from test_trace_capture import grouped
    ep, hst, clock, lc, an, grp = grouped(mode=c.REPEAT)
    grp.bind([an, lc])
    ep.capture_fail_start = {lc.fn}
    clock.t = 5
    with pytest.raises(h.Failed):
        grp.start([an, lc])
    assert grp.generation == 1 and grp.generations == {an.fn: 1, lc.fn: 1}
    assert an.generation == ep.captures[an.fn].generation and lc.generation == ep.captures[lc.fn].generation
    an.read(0, 0)                                                           # the started-then-stopped track's generation


# ---- §4.1 "bindは、同じ経路のstart成功応答（18+6×n byte）がframeへ収まることも確かめ、収まらなければunsupported" ----
def test_bench_bind_counts_the_start_answer_message_against_max_frame():
    """The 18 holds the result header (5) and start_ns generation n (13): two tracks' answer is 30 bytes, which a
    30-byte frame holds."""
    from test_trace_capture import grouped
    ep, hst, clock, lc, an, grp = grouped()
    ep.probe.max_frame = 29
    try:
        grp.bind([an, lc])
        refused = None
    except h.Rejected as e:
        refused = e.result
    assert refused is not None and refused.detail == m.UNSUPPORTED and refused.payload[:1] == b"\x00"
    ep.probe.max_frame = 30
    grp.bind([an, lc])
    assert ep.captures[an.fn].group is ep.groups[grp.fn]
