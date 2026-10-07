"""oep-spec 0098b56 .. 2c6d18d (the external review's interface re-check, 2026-10-07), in the client and the virtual
bench: capture §3.3's configure contract, §3.4's generations (wrap, events carry them, a host drops another start's),
§3.2's segments paged by common §1.3, §4's capture-group start answer, status and events with the group's generation."""

import collections
import struct

import pytest

from oep_client import capture as c, core, endpoint, virtual_bench, virtual_bench_capture as vbc, host as h, message as m


class Clock:
    t = 0

    def __call__(self):
        return self.t


class Link:
    """What a link keeps of the notifications: the events, the data pushes (filled from the endpoint here)."""

    def __init__(self):
        self.events, self.pushes = collections.deque(), collections.deque()

    def take(self, ep):
        for f in ep.pushes():
            (self.events if f[0] == m.ROLE_EVENT else self.pushes).append(f)


def bench(profile=virtual_bench.p4_x035):
    clock = Clock()
    ep = endpoint.Endpoint(profile(), clock)
    hst = h.Host(lambda b: ep.handle(b, 0))
    hst.open(3000)
    return ep, hst, c.LogicCapture(hst), clock


def body(mode=None, rate=None, samples=None, segments=None, trigger=None, pretrigger=None):
    out = b""
    for tag, fmt, v in ((c.MODE, "<B", mode), (c.RATE, "<I", rate), (c.SAMPLES, "<I", samples),
                        (c.SEGMENTS, "<I", segments), (c.TRIGGER, "<BBI", trigger), (c.PRETRIGGER, "<I", pretrigger)):
        if v is not None:
            out += m.tlv(tag, struct.pack(fmt, *(v if isinstance(v, tuple) else (v,))))
    return out


# ---- capture §3.3: the configure / query contract ------------------------------------------------------------------

@pytest.mark.parametrize("what, payload", [
    ("no mode", body(rate=1_000_000, samples=100)),
    ("no rate", body(mode=c.ONE_SHOT, samples=100)),
    ("one-shot without samples", body(mode=c.ONE_SHOT, rate=1_000_000)),
    ("repeat without samples", body(mode=c.REPEAT, rate=1_000_000, segments=2)),
])
def test_a_required_tlv_missing_is_malformed(what, payload):
    ep, hst, lc, _ = bench()
    core.plan_apply(hst, [(lc.fn, 0, 20)])
    for op in (lc.CONFIGURE, lc.QUERY_OP):
        with pytest.raises(h.Rejected) as e:
            hst.request(lc.fn, op, payload, locked=op == lc.CONFIGURE)
        assert e.value.result.detail == m.MALFORMED, what


@pytest.mark.parametrize("what, payload, tag", [
    ("streaming with samples", body(mode=c.STREAMING, rate=1_000_000, samples=1000), c.SAMPLES),
    ("one-shot with segments", body(mode=c.ONE_SHOT, rate=1_000_000, samples=100, segments=1), c.SEGMENTS),
    ("streaming with segments", body(mode=c.STREAMING, rate=1_000_000, segments=2), c.SEGMENTS),
    ("pretrigger without a trigger", body(mode=c.ONE_SHOT, rate=1_000_000, samples=100, pretrigger=0), c.PRETRIGGER),
    ("pretrigger with type 0", body(mode=c.ONE_SHOT, rate=1_000_000, samples=100, trigger=(0, 0, 0), pretrigger=10),
     c.PRETRIGGER),
    ("pretrigger not below samples", body(mode=c.ONE_SHOT, rate=1_000_000, samples=100, trigger=(c.EDGE, 0, 0),
                                          pretrigger=100), c.PRETRIGGER),
])
def test_what_the_contract_rules_out_is_unsupported_with_its_tag(what, payload, tag):
    ep, hst, lc, _ = bench()
    core.plan_apply(hst, [(lc.fn, 0, 20)])
    for op in (lc.CONFIGURE, lc.QUERY_OP):
        with pytest.raises(h.Unsupported) as e:
            hst.request(lc.fn, op, payload, locked=op == lc.CONFIGURE)
        assert e.value.result.payload == bytes([tag]), what
    assert ep.captures[lc.fn].state == c.STATE["unconfigured"]          # nothing changed


def test_a_pretrigger_past_max_pretrigger_is_unsupported():
    ep, hst, lc, _ = bench(virtual_bench.esp32_v003)
    cap = ep.captures[lc.fn]
    core.plan_apply(hst, [(lc.fn, 0, 4)])
    cap.max_pretrigger = 50
    with pytest.raises(h.Unsupported) as e:
        lc.configure(rate=1_000_000, samples=100, trigger=(c.EDGE, 0, 0), pretrigger=51)
    assert e.value.result.payload[0] & 0x7F == c.PRETRIGGER
    lc.configure(rate=1_000_000, samples=100, trigger=(c.EDGE, 0, 0), pretrigger=50)


def test_a_trigger_role_not_in_the_plan_is_unavailable_wrong_state():
    ep, hst, lc, _ = bench()
    core.plan_apply(hst, [(lc.fn, 0, 20)])
    with pytest.raises(h.Unavailable) as e:
        lc.configure(rate=1_000_000, samples=100, trigger=(c.EDGE, 3, 0))
    assert e.value.cause == "wrong_state"


def test_the_answer_rows_follow_the_mode():
    ep, hst, lc, _ = bench()
    core.plan_apply(hst, [(lc.fn, 0, 20)])
    rows = {}
    for mode, extra in ((c.ONE_SHOT, dict(samples=100)), (c.REPEAT, dict(samples=100, segments=2)),
                        (c.STREAMING, {})):
        r = hst.request(lc.fn, lc.QUERY_OP, body(mode=mode, rate=1_000_000, **extra), locked=False)
        rows[mode] = [t for t, _ in m.split_tlvs(r.payload)]
    assert rows[c.ONE_SHOT] == [c.ACTUAL_RATE, c.LAYOUT, c.ACTUAL_SAMPLES, c.BLOCKING]
    assert rows[c.REPEAT] == [c.ACTUAL_RATE, c.LAYOUT, c.ACTUAL_SAMPLES, c.ACTUAL_SEGMENTS, c.BLOCKING]
    assert rows[c.STREAMING] == [c.ACTUAL_RATE, c.LAYOUT, c.BLOCKING]
    assert lc.configure(rate=1_000_000, samples=100).segments == 1      # one-shot: one segment, no row for it


@pytest.mark.parametrize("kw, words", [
    (dict(samples=None), "samples is required"),
    (dict(mode=c.REPEAT, samples=None), "samples is required"),
    (dict(mode=c.STREAMING, samples=10), "streaming takes no samples"),
    (dict(segments=2), "segments is for repeat only"),
    (dict(mode=c.STREAMING, samples=None, segments=2), "segments is for repeat only"),
    (dict(pretrigger=5), "a pretrigger needs a trigger"),
    (dict(trigger=(c.IMMEDIATE, 0, 0), pretrigger=5), "a pretrigger needs a trigger"),
])
def test_the_client_keeps_the_contract_before_sending(kw, words):
    ep, hst, lc, _ = bench()
    core.plan_apply(hst, [(lc.fn, 0, 20)])
    n = len(ep.requests)
    args = dict(rate=1_000_000, samples=100) | kw
    with pytest.raises(ValueError, match=words):
        lc.configure(**{k: v for k, v in args.items() if v is not None or k != "samples"})
    assert len(ep.requests) == n                                        # nothing sent


def test_a_pretrigger_of_0_without_a_trigger_is_left_out():
    ep, hst, lc, _ = bench()
    core.plan_apply(hst, [(lc.fn, 0, 20)])
    lc.configure(rate=1_000_000, samples=100, pretrigger=0)
    assert c.PRETRIGGER not in {t & 0x7F for t, _ in m.split_tlvs(ep.requests[-1].payload)}


# ---- capture §3.4: generations wrap, events carry them --------------------------------------------------------------

def test_the_generation_after_0xffffffff_is_1():
    assert m.next_generation(0) == 1 and m.next_generation(0xFFFFFFFF) == 1 and m.next_generation(41) == 42
    ep, hst, lc, _ = bench()
    core.plan_apply(hst, [(lc.fn, 0, 20)])
    lc.configure(rate=1_000_000, samples=64)
    ep.captures[lc.fn].generation = 0xFFFFFFFF
    lc.start()
    assert lc.generation == 1 and lc.status().generation == 1
    (seg,) = lc.wait()
    assert seg.generation == 1 and len(lc.read_segment(seg)) == 8


def test_track_events_carry_the_generation():
    ep, hst, lc, _ = bench()
    core.plan_apply(hst, [(lc.fn, 0, 20), (lc.fn, 1, 21)])
    lc.configure(rate=1_000_000, samples=64, trigger=(c.EDGE, 1, 0))
    lc.subscribe()
    lc.start()
    link = Link()
    link.take(ep)
    got = lc.events(link)
    assert [e.kind for e in got] == ["triggered", "segment", "stopped"] and not link.events
    trig, seg, stop = got
    assert {e.generation for e in got} == {1} and stop.reason_name == "complete" and stop.error == 0
    assert (trig.serial, trig.trigger_index) == (0, seg.segment.trigger_index)
    assert trig.trigger_ns == seg.segment.start_ns + trig.trigger_index * 1000


def test_an_event_of_an_earlier_start_is_dropped():
    """The stop of generation 1 is still on the link when generation 2's start has answered (core §11.4): the host
    does not take it for the current capture's (§3.4)."""
    ep, hst, lc, clock = bench()
    core.plan_apply(hst, [(lc.fn, 0, 20)])
    lc.configure(rate=1_000_000, mode=c.REPEAT, samples=1000, segments=3)
    lc.subscribe()
    lc.start()
    lc.stop()
    link = Link()
    link.take(ep)                                                        # generation 1's stopped, not read yet
    lc.start()
    clock.t = 1
    lc.status()                                                          # generation 2's first segment
    link.take(ep)
    got = lc.events(link)
    assert [(e.kind, e.generation) for e in got] == [("segment", 2)] and lc.stale_events == 1
    other = c.LogicCapture(hst)                                          # a host that did not start it: status first
    link.events.append(bytes([m.ROLE_EVENT]) + struct.pack("<HHB", lc.fn, 9, c.EVENT_STOPPED) + bytes([1, 0])
                       + struct.pack("<I", 1))
    assert other.events(link) == [] and other.generation == 2 and other.stale_events == 1


def test_a_short_event_is_a_protocol_error():
    with pytest.raises(h.ProtocolError):
        c.unpack_event(bytes([m.ROLE_EVENT]) + struct.pack("<HHB", 9, 0, c.EVENT_STOPPED) + bytes([1, 0]))   # no generation


# ---- capture §3.2: segments paged by common §1.3, serials wrap, release ----------------------------------------------

def repeat(ring=8, max_frame=None):
    ep, hst, lc, clock = bench()
    if max_frame:
        ep.probe.max_frame = max_frame
    core.plan_apply(hst, [(lc.fn, 0, 20)])
    lc.configure(rate=1_000_000, mode=c.REPEAT, samples=1000, segments=ring)
    return ep, hst, lc, clock


def test_segments_from_serial_done_is_empty_with_more_0():
    ep, hst, lc, clock = repeat()
    lc.start()
    clock.t = 3
    assert lc.status().serial_done == 3
    assert lc.segments_page(3) == ([], False)
    assert [s.serial for s in lc.segments_page(1)[0]] == [1, 2]          # from_serial included
    lc.release(1)
    assert [s.serial for s in lc.segments_page(0)[0]] == [2]             # released: from the oldest kept
    assert [s.serial for s in lc.segments_page(77)[0]] == [2]            # not given yet: the same


def test_segments_page_by_the_last_serial_plus_1_and_stop_at_more_0():
    ep, hst, lc, clock = repeat(max_frame=64 + 37)                      # 2 records an answer
    lc.start()
    clock.t = 5
    lc.status()
    pages = []
    real = lc.segments_page
    lc.segments_page = lambda f=0: pages.append(f) or real(f)
    assert [s.serial for s in lc.segments()] == [0, 1, 2, 3, 4] and pages == [0, 2, 4]


def test_segment_serials_wrap():
    ep, hst, lc, clock = repeat(ring=4)
    lc.start()
    ep.captures[lc.fn].serial_done = 0xFFFFFFFE
    clock.t = 3
    assert lc.status().serial_done == 1
    assert [s.serial for s in lc.segments(0xFFFFFFFE)] == [0xFFFFFFFE, 0xFFFFFFFF, 0]
    assert lc.segments_page(1) == ([], False)                            # serial_done
    lc.release(0xFFFFFFFF)                                               # at or before it, by core §2.6
    assert [s.serial for s in lc.segments()] == [0]


def test_release_never_frees_a_segment_not_finished():
    ep, hst, lc, clock = repeat(ring=3)
    lc.start()
    clock.t = 1
    lc.status()
    lc.release(5)                                                        # serial 5 is not finished: only 0 goes
    assert lc.segments() == []
    clock.t = 2
    assert [s.serial for s in lc.segments()] == [1]


# ---- capture §4: the group's start answer, status and events -----------------------------------------------------------

def group(clock_t=7):
    ep, hst, lc, clock = bench()
    an, grp = c.AnalogCapture(hst), c.CaptureGroup(hst)
    clock.t = clock_t
    core.plan_apply(hst, [(lc.fn, 0, 20), (lc.fn, 1, 21), (an.fn, 0, 16)])
    lc.configure(rate=1_000_000, samples=10_000, trigger=(c.EDGE, 1, 0), pretrigger=4000)
    an.configure(rate=10_000, samples=100)
    return ep, hst, lc, an, grp, clock


def test_the_group_start_answer_has_the_groups_and_each_tracks_generation():
    ep, hst, lc, an, grp, clock = group()
    grp.bind([an, lc], trigger=lc)                                       # bind order: analog first
    ep.groups[grp.fn].generation, ep.captures[lc.fn].generation, ep.captures[an.fn].generation = 4, 3, 1
    blocking, start_ns = grp.start([lc, an])
    assert (blocking, start_ns, grp.generation) == (0, 7_000_000, 5)
    assert grp.generations == {an.fn: 2, lc.fn: 4} and (lc.generation, an.generation) == (4, 2)
    raw = hst.request(grp.fn, grp.STATUS, b"", locked=False).payload
    assert struct.unpack_from("<I", raw, 19)[0] == 5 and grp.status().generation == 5
    grp.bind([lc, an], trigger=lc)                                       # bound again: the group's generation goes on
    grp.start([lc, an])
    assert grp.generation == 6 and list(grp.generations) == [lc.fn, an.fn]


def test_the_start_answer_fixed_part_on_the_wire():
    ep, hst, lc, an, grp, clock = group()
    grp.bind([lc, an], trigger=lc)
    r = hst.request(grp.fn, grp.START, b"")
    blocking, start_ns, generation, n = struct.unpack_from("<IQIB", r.payload)
    assert (blocking, start_ns, generation, n) == (0, 7_000_000, 1, 2) and len(r.payload) == 17 + 6 * n
    assert [struct.unpack_from("<HI", r.payload, 17 + 6 * k) for k in range(n)] == [(lc.fn, 1), (an.fn, 1)]
    assert not m.split_tlvs(r.payload[17 + 6 * n:])                     # no TLV 0x01 generations any more


def test_the_groups_events_carry_its_generation_and_wrap():
    ep, hst, lc, an, grp, clock = group()
    grp.bind([lc, an], trigger=lc)
    ep.groups[grp.fn].generation = 0xFFFFFFFF
    hst.subscribe(grp.fn)
    grp.start([lc, an])
    assert grp.generation == 1
    link = Link()
    link.take(ep)
    got = grp.events(link)
    assert [(e.kind, e.generation) for e in got] == [("triggered", 1), ("stopped", 1)]
    assert got[0].trigger_fn == lc.fn and got[0].trigger_ns == grp.status().trigger_ns
    link.events.append(bytes([m.ROLE_EVENT]) + struct.pack("<HHB", grp.fn, 7, c.EVENT_STOPPED) + bytes([1, 0])
                       + struct.pack("<I", 0xFFFFFFFF))                  # the start before's
    assert grp.events(link) == [] and grp.stale_events == 1


def test_the_vb_events_are_the_vectors_frames():
    """ops.json's `events` built by the virtual bench: a logic stopped (host) of generation 3, the group's stopped
    (complete) of generation 5 - the kind and fixed part after the header."""
    cap = vbc.VirtualCapture({1, 2}, {8}, 1, 1_000_000)
    cap.generation = 3
    assert cap.stopped_event(vbc.STOPPED["host"]) == bytes.fromhex("050900070002010003000000")[5:]
    grp = vbc.VirtualGroup([9, 13], 2, [])
    grp.generation = 5
    assert grp.stopped_event(vbc.STOPPED["complete"]) == bytes.fromhex("050c00010002000005000000")[5:]


# ---- common §1.3: marks paged by serial; console streams' first(u16) (console §1) --------------------------------------

from oep_client import console as con   # noqa: E402


def marks_bench(serials, next_serial, max_frame=64):
    ep, hst, _, _ = bench()
    ep.probe.max_frame = max_frame
    st = endpoint.Stream(data=bytearray(100))
    st.marks = [(k, k & 0x3F, 7, 1000 * (k & 0xFF), k & 0xFF) for k in serials]
    st.serial = next_serial
    ep.streams[2] = st
    c = con.Console(hst)
    c.stream = 2
    return ep, c


def test_marks_page_from_the_serial_included_and_stop_at_more_0():
    ep, c = marks_bench(range(5, 9), 9)
    assert [k.serial for k in c.marks_page(6)[0]] == [6, 7]
    assert c.marks_page(9) == ([], False)                                # next: none, more 0
    assert [k.serial for k in c.marks_page(2)[0]] == [5, 6]              # pushed out: from the oldest kept
    assert [k.serial for k in c.marks_page(100)[0]] == [5, 6]            # not given yet: the same
    froms = [struct.unpack_from("<I", r.payload, 2)[0] for r in ep.requests[-4:]]
    assert froms == [6, 9, 2, 100]
    n = len(ep.requests)
    assert [k.serial for k in c.marks(5)] == [5, 6, 7, 8]
    assert [struct.unpack_from("<I", r.payload, 2)[0] for r in ep.requests[n:]] == [5, 7]   # last + 1, more 0 ends


def test_marks_serials_wrap():
    ep, c = marks_bench([0xFFFFFFFE, 0xFFFFFFFF, 0, 1], 2)
    assert [k.serial for k in c.marks(0xFFFFFFFE)] == [0xFFFFFFFE, 0xFFFFFFFF, 0, 1]
    assert c.marks_page(2) == ([], False)
    _, none_kept = marks_bench([], 0)
    assert none_kept.marks_page(0) == ([], False)                       # nothing kept: none, more 0


def test_streams_first_is_u16_and_pages_past_255():
    ep, hst, _, _ = bench()
    for k in range(300):
        ep.streams[1000 + k] = endpoint.Stream(conn=1)
        ep.stream_order[1000 + k] = k
    c = con.Console(hst)
    got = c.streams()
    assert [s.stream for s in got] == [1000 + k for k in range(300)]
    firsts = [struct.unpack("<H", r.payload)[0] for r in ep.requests if r.op == con.Console.STREAMS]
    assert firsts[0] == 0 and firsts[-1] > 255 and all(len(r.payload) == 2 for r in ep.requests if r.op == con.Console.STREAMS)


# ---- debug §4.2, §4.4: step's fields are 0 unless status ok; run's elapsed_us and an invalid dpc -------------------------

from oep_client import riscv   # noqa: E402


@pytest.mark.parametrize("stuck", [None, "halts", "runs"])
def test_a_step_that_is_not_ok_answers_zeros_and_the_client_reads_none(stuck):
    ep = endpoint.Endpoint(virtual_bench.p4_bench(), Clock())
    hst = h.Host(lambda b: ep.handle(b, 0))
    hst.open(3000)
    pair = ep.pairs[1][0]
    conn, _ = riscv.Wire(hst).attach(halt=stuck is not None, pins=pair)
    tg = ep._target(1, pair)
    tg.dpc = 0x1234
    if stuck is None:
        tg.halted = False                                               # a running hart: status state
    tg.step_stuck = stuck
    dm = riscv.RiscvDm(hst, conn)
    with pytest.raises(riscv.StepError) as e:
        dm.step()
    status, moved, before, after = struct.unpack_from("<BBII", e.value.result.payload)
    assert (moved, before, after) == (0, 0, 0) and status == riscv.STATUS["state"]
    assert (e.value.dpc_before, e.value.dpc_after, e.value.step_left) == (None, None, stuck == "runs")


def test_a_run_result_never_shows_an_invalid_dpc():
    halted = riscv.RunResult(riscv.STATUS["ok"], True, 0x20000010, 50)
    not_halted = riscv.RunResult(riscv.STATUS["timeout"], False, 0, 1000, not_halted=True)
    not_run = riscv.RunResult(riscv.STATUS["line"], False, 0, 0, not_run=True)
    assert halted.dpc_valid and halted.where() == "dpc 0x20000010"
    assert not not_halted.dpc_valid and "dpc unknown" in not_halted.where()
    assert not not_run.dpc_valid and not_run.where() == "not run" and not_run.elapsed_us == 0


# ---- fixture §4: SPI target data bit packing --------------------------------------------------------------------------

from oep_client import fixture   # noqa: E402


@pytest.mark.parametrize("order, packed", [(0, "c0b0"), (1, "030d")])
def test_spi_wire_bits_pack_msb_or_lsb_first_with_a_partial_last_byte(order, packed):
    wire = [1, 1, 0, 0, 0, 0, 0, 0, 1, 0, 1, 1]
    assert fixture.pack_wire_bits(wire, order).hex() == packed
    assert fixture.wire_bits(bytes.fromhex(packed), 12, order) == wire
    assert fixture.wire_bits(bytes.fromhex(packed), 99, order)[:12] == wire   # never past the bytes there are


@pytest.mark.parametrize("order", [0, 1])
def test_the_virtual_bench_clears_the_bits_that_did_not_come(order):
    ep = endpoint.Endpoint(virtual_bench.esp32_v003(), Clock())
    fn = next(f for f, n in ep.names.items() if n == "oep.fixture.spi-target")
    st = ep.spi[fn]
    st.state, st.bit_order, st.armed = 1, order, (4, b"")
    ep.spi_transfer(fn, b"\xff\xff", bits=12)
    bits, data, _ = st.queue[0]
    assert bits == 12 and data == fixture.pack_wire_bits([1] * 12, order)


# ---- oep-spec 66c49e7, capture §1.1: w is any integer 1-128 -------------------------------------------------------------

import json   # noqa: E402
from pathlib import Path   # noqa: E402

VECTORS = Path(__file__).resolve().parent / "vectors"


@pytest.mark.parametrize("case", json.loads((VECTORS / "logic_layout.json").read_text())["cases"], ids=lambda c: c["name"])
def test_logic_layout_vectors_both_ways(case):
    w, pos, n = case["w"], case["pos"], case["samples"]
    lc = c.LogicCapture.__new__(c.LogicCapture)
    lc.config = c.Config(width=w, positions=pos, samples=n)
    data = bytes.fromhex(case["stream_hex"])
    assert ["".join(map(str, lc.channel(data, k, n))) for k in range(len(pos))] == case["channels"]
    values = [sum(int(case["channels"][k][i]) << pos[k] for k in range(len(pos))) for i in range(n)]
    assert vbc.pack_samples(values, w).hex() == case["stream_hex"]


@pytest.mark.parametrize("w", [3, 5, 7, 12, 13, 24, 100])
def test_a_capture_with_any_w_reads_back(w):
    ep, hst, lc, _ = bench()
    ep.captures[lc.fn].widths = {w}
    core.plan_apply(hst, [(lc.fn, 0, 20), (lc.fn, 1, 21), (lc.fn, 2, 22)])
    cfg = lc.configure(rate=1_000_000, samples=101)
    assert cfg.width == w and cfg.bytes == (101 * w + 7) // 8
    lc.start()
    (seg,) = lc.wait()
    data = lc.read_segment(seg)
    for k in range(3):
        assert lc.channel(data, k, 101) == [(i >> k) & 1 for i in range(101)]


# ---- oep-spec 0b9a058, capture §2.2: a segment not kept seamless is not handed out ----------------------------------------

def test_data_dropped_inside_a_segment_stops_the_track_in_error():
    ep, hst, lc, clock = bench()
    core.plan_apply(hst, [(lc.fn, 0, 20)])
    lc.configure(rate=1_000_000, mode=c.REPEAT, samples=1000, segments=8)
    lc.subscribe()
    lc.start()
    clock.t = 2.5                                                         # serials 0 and 1 done, 2 under way
    ep.capture_overflow(lc.fn)
    st = lc.status()
    assert (st.state, st.serial_done, st.dropped, st.error_name) == (c.STATE["error"], 2, True, "storage")
    assert st.write_pos == 2 * lc.config.bytes                            # the start of the dropped segment
    assert [s.serial for s in lc.segments()] == [0, 1]
    link = Link()
    link.take(ep)
    got = lc.events(link)
    assert [(e.kind, e.reason_name, e.error) for e in got][-1] == ("stopped", "error", 2)
    assert "segment" not in [e.kind for e in got[2:]]
    with pytest.raises(h.Failed, match="storage"):
        lc.wait()
    lc.start()                                                            # a new start clears the error
    assert lc.status().error is None


def test_a_bound_tracks_drop_stops_the_group():
    ep, hst, lc, an, grp, clock = group()
    lc.configure(rate=1_000_000, mode=c.REPEAT, samples=1000, segments=4)
    an.configure(rate=10_000, mode=c.REPEAT, samples=100, segments=4)
    grp.bind([lc, an])
    hst.subscribe(grp.fn)
    grp.start([lc, an])
    ep.capture_overflow(an.fn, 1)                                         # a DMA failure on the analog
    assert grp.status().state == c.STATE["error"] and lc.status().state == c.STATE["configured"]
    link = Link()
    link.take(ep)
    (e,) = grp.events(link)
    assert (e.kind, e.reason_name, e.error) == ("stopped", "error", 1)


# ---- oep-spec 59c5459 / dd5a886, capture §5: multirate ------------------------------------------------------------------

from oep_client import multirate as mr   # noqa: E402

A, S_, E = mr.ANY_ACTIVE, mr.SAMPLE, mr.EDGE_LATCH


def multirate_bench(roles=4):
    ep, hst, lc, clock = bench()
    core.plan_apply(hst, [(lc.fn, k, 20 + k) for k in range(roles)])
    return ep, hst, lc, clock


def counter_levels(role, first, n):
    return [((first + i) >> role) & 1 for i in range(n)]


def test_the_virtual_bench_declares_multirate_and_d_1_is_always_accepted():
    ep, hst, lc, _ = multirate_bench()
    decl = lc.multirate_declared()
    assert decl == mr.Declared(7, 2, 1024, False)
    assert decl.accepts_d(1) and not decl.accepts_d(0) and decl.accepts_d(3)
    assert mr.Declared(7, 2, 128, True).accepts_d(1) and not mr.Declared(7, 2, 128, True).accepts_d(3)   # d 1 always
    assert c.AnalogCapture(hst).multirate_declared() is None
    cfg = lc.configure(rate=1_000_000, samples=64, multirate=[mr.Multirate(1, S_, 1, 0), mr.Multirate(2, S_, 2, 1)])
    assert cfg.positions == [0, 1, 2] and cfg.block == 32                  # role 1's d 1 sample: a D = 1 channel


@pytest.mark.parametrize("mode", [c.ONE_SHOT, c.REPEAT])
def test_a_multirate_capture_decodes_to_the_waveform(mode):
    ep, hst, lc, clock = multirate_bench()
    specs = [mr.Multirate(0, A, 8, 1), mr.Multirate(2, E, 4, 1), mr.Multirate(3, S_, 4, 3)]
    extra = dict(segments=2) if mode == c.REPEAT else {}
    cfg = lc.configure(rate=1_000_000, mode=mode, samples=100, multirate=specs, **extra)
    assert cfg.block == 32 and cfg.samples == 128 and cfg.positions == [0]   # rounded up to L; role 1 is D = 1
    lc.start()
    if mode == c.REPEAT:
        clock.t = 1
        segs = lc.segments()
    else:
        segs = lc.wait()
    assert segs and all(s.samples == 128 for s in segs)
    for seg in segs:
        data = lc.read_segment(seg)
        assert len(data) == cfg.bytes == 4 * cfg.multirate_layout().block_bytes()
        got = lc.decode_multirate(data, seg.samples)
        first = seg.serial * 128
        assert got.d1 == [counter_levels(1, first, 128)]
        for s in specs:
            assert got.reduced[s.role] == s.values(counter_levels(s.role, first, 128))


def test_multirate_streaming_pushes_whole_blocks():
    ep, hst, lc, clock = multirate_bench()
    lc.configure(rate=1_000_000, mode=c.STREAMING, multirate=[mr.Multirate(3, A, 16, 0)])
    lc.subscribe()
    lc.start()
    clock.t = 1                                                          # 1000 base samples: 31 blocks of 32
    pushes = [c.unpack_push(f) for f in ep.pushes() if f[0] == m.ROLE_DATA]
    data = b"".join(p[3] for p in pushes)
    lay = lc.config.multirate_layout()
    assert len(data) == 31 * lay.block_bytes()
    got = lay.decode(data, 31 * 32)
    assert got.reduced[3] == mr.Multirate(3, A, 16, 0).values(counter_levels(3, 0, 31 * 32))


def test_multirate_refusals_on_the_virtual_bench():
    ep, hst, lc, _ = multirate_bench()
    base = m.tlv(c.MODE, bytes([1])) + m.tlv(c.RATE, struct.pack("<I", 1_000_000)) + m.tlv(c.SAMPLES, struct.pack("<I", 64))
    def ask(*specs, critical=True):
        return hst.request(lc.fn, lc.QUERY_OP, base + b"".join(m.tlv(mr.TAG, mr.TLV.pack(*x), critical=critical) for x in specs),
                           locked=False)
    for bad in [[(0, A, 0, 0)], [(0, S_, 4, 4)], [(0, E, 1, 1)], [(0, A, 4, 2)], [(1, S_, 2, 0), (1, S_, 4, 0)]]:
        with pytest.raises(h.Rejected) as e:
            ask(*bad)
        assert e.value.result.detail == m.MALFORMED, bad
    for bad, critical in [((0, 3, 4, 0), True), ((0, S_, 2000, 0), False)]:
        with pytest.raises(h.Unsupported) as e:
            ask(bad, critical=critical)
        assert e.value.result.payload == bytes([mr.TAG | (0x80 if critical else 0)])
    with pytest.raises(h.Unavailable) as e:
        ask((9, S_, 4, 0))                                               # role 9 not in the plan
    assert e.value.cause == "wrong_state"
    ep.captures[lc.fn].min_hz = ep.captures[lc.fn].max_hz = 1_000_000   # one rate only: a heavy one cannot keep it
    with pytest.raises(h.Unsupported) as e:
        ask(*[(k, E, 2, 1) for k in range(4)])
    assert e.value.result.payload == bytes([mr.TAG | 0x80])


def test_the_client_checks_multirate_before_sending():
    ep, hst, lc, _ = multirate_bench()
    n = len(ep.requests)
    for specs, words in [([mr.Multirate(0, A, 0, 0)], "d 0"), ([mr.Multirate(0, S_, 4, 4)], "phase"),
                         ([mr.Multirate(0, E, 1, 0)], "edge_latch"), ([mr.Multirate(1, S_, 2), mr.Multirate(1, S_, 4)], "twice"),
                         ([mr.Multirate(0, 3, 4, 0)], "policy 3"), ([mr.Multirate(0, S_, 2048, 0)], "d 2048")]:
        with pytest.raises(ValueError, match=words):
            lc.configure(rate=1_000_000, samples=64, multirate=specs)
    an = c.AnalogCapture(hst)
    with pytest.raises(ValueError, match="declares no multirate"):
        an.configure(rate=10_000, samples=64, multirate=[mr.Multirate(0, S_, 2, 0)])
    assert not [r for r in ep.requests[n:] if r.op in (c.LogicCapture.CONFIGURE, c.LogicCapture.QUERY_OP) and r.fn != 0]


def test_a_multirate_track_heavy_combination_gets_a_lower_rate():
    ep, hst, lc, _ = multirate_bench()
    light = lc.configure(rate=20_000_000, samples=64, query=True, multirate=[mr.Multirate(0, A, 4, 0)])
    heavy = lc.configure(rate=20_000_000, samples=64, query=True, multirate=[mr.Multirate(k, E, 2, 1) for k in range(4)])
    assert light.rate == 20_000_000 and heavy.rate < 20_000_000 and heavy.positions == [] and heavy.width == 1
