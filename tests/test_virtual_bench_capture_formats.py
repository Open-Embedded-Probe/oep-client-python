"""Capture §1 data formats 1 (logic blocks) and 4 (test counter), §3.3 trigger_opts, §3.10 trigger records - through the
virtual bench and the client, end to end."""
import struct

import pytest

from oep_client import capture as c, capture_blocks as cb, core, host as h, message as m
from test_virtual_bench_capture import bench

LOGIC_BLOCKS, TEST_COUNTER = c.DATA_FORMAT["logic_blocks"], c.DATA_FORMAT["test_counter"]


def level(k, i):
    return (i >> k) & 1                                    # the virtual logic: channel k is bit k of the counter


def planned(hst, lc, n):
    core.plan_apply(hst, [(lc.fn, r, 30 + r) for r in range(n)])
    return list(range(n))


def check_blocks(blocks, events, samples, first, contiguous_before=None):
    for b in blocks:
        at = first + b.first
        for role, bits in b.raw.items():
            assert cb.bits_list(bits, b.samples) == [level(role, at + i) for i in range(b.samples)]
        for role, ch in b.events.items():
            before = level(role, at - 1) if (b.first or contiguous_before) else level(role, at)
            assert ch.start_level == before
            want, last = [], before
            for t in range(-(-b.samples // ch.d)):
                n = 0
                for i in range(t * ch.d, min((t + 1) * ch.d, b.samples)):
                    v = level(role, at + i)
                    n += v != last
                    last = v
                if n:
                    want.append((t, last, n))
            assert list(zip(ch.ticks, ch.levels, ch.counts)) == want
    assert sum(b.samples for b in blocks) == samples
    assert {r for b in blocks for r in list(b.raw) + list(b.events)} >= set(events)


def test_format_1_one_shot_raw_events_and_a_required_fallback():
    ep, hst, lc, clock = bench()
    roles = planned(hst, lc, 12)
    events = {2: 4, 8: 16, 9: 16, 11: 32}                  # role 2 changes every 4 samples: past L/8, so fallback
    cfg = lc.configure(rate=1_000_000, samples=1000, data_format=LOGIC_BLOCKS, events=events, roles=roles, block_max=256)
    assert (cfg.format, cfg.block, cfg.width, cfg.positions) == (LOGIC_BLOCKS, 256, 8, list(range(8)))
    lc.start()
    clock.t += 5
    (seg,) = lc.wait()
    blocks = lc.blocks(lc.read_segment(seg), seg.samples)
    assert [b.samples for b in blocks] == [256, 256, 256, 232]   # a partial last block
    assert all(b.fallback == {2} for b in blocks)
    check_blocks(blocks, events, seg.samples, 0)
    assert len(lc.read_segment(seg)) <= 4 * cb.worst_bytes(8, list(events.values()), 256)


def test_format_1_repeat_start_level_follows_the_segment_before():
    ep, hst, lc, clock = bench()
    roles = planned(hst, lc, 10)
    events = {9: 8}
    lc.configure(rate=1_000_000, mode=c.REPEAT, samples=200, segments=3, data_format=LOGIC_BLOCKS, events=events,
                 roles=roles)
    lc.start()
    clock.t += 5
    segs = lc.segments()
    assert len(segs) == 3 and not any(s.flags & 1 for s in segs)
    for k, s in enumerate(segs):
        check_blocks(lc.blocks(lc.read_segment(s), s.samples), events, s.samples, k * 200, contiguous_before=k > 0)


def test_format_1_contract():
    ep, hst, lc, clock = bench()
    roles = planned(hst, lc, 4)
    with pytest.raises(h.Rejected) as e:                    # a raw carriage with d > 1: malformed (§3.3)
        lc._call(lc.CONFIGURE, m.tlv(c.MODE, bytes([c.ONE_SHOT])) + m.tlv(c.RATE, struct.pack("<I", 1000))
                 + m.tlv(c.SAMPLES, struct.pack("<I", 64)) + m.tlv(c.FORMAT | c.CRITICAL, bytes([LOGIC_BLOCKS]))
                 + m.tlv(c.CARRIAGE | c.CRITICAL, struct.pack("<BBI", 0, c.LOGIC_CARRIAGE["raw"], 4)))
    assert e.value.result.detail == m.MALFORMED
    with pytest.raises(h.Unsupported):                      # L can't be a multiple of 8 and 3 within 16
        lc.configure(rate=1000, samples=64, data_format=LOGIC_BLOCKS, events={3: 3}, roles=roles, block_max=16)
    with pytest.raises(h.Unsupported):                      # a critical TLV it does not have (threshold, §3.8)
        lc._call(lc.CONFIGURE, m.tlv(c.MODE, bytes([c.ONE_SHOT])) + m.tlv(c.RATE, struct.pack("<I", 1000))
                 + m.tlv(c.SAMPLES, struct.pack("<I", 64))
                 + m.tlv(c._CAP.tlv["configure"]["threshold"] | c.CRITICAL, struct.pack("<BiI", 0, 1650, 0)))


@pytest.mark.parametrize("mode", [c.ONE_SHOT, c.STREAMING])
def test_format_4_counts_the_stream_positions(mode):
    ep, hst, lc, clock = bench()
    planned(hst, lc, 3)
    cfg = lc.configure(rate=1_000_000, mode=mode, samples=1000 if mode == c.ONE_SHOT else None, data_format=TEST_COUNTER)
    assert cfg.format == TEST_COUNTER and cfg.width == 4
    if mode == c.STREAMING:
        lc.subscribe()
    lc.start()
    clock.t += 3
    if mode == c.ONE_SHOT:
        (seg,) = lc.wait()
        data = lc.read_segment(seg)
        assert len(data) == 500
        assert data == bytes(k & 0xFF for k in range(500))
    else:
        got = b""
        for f in ep.pushes():
            if f[0] == m.ROLE_DATA:
                _, _, position, chunk, _ = c.unpack_push(f)
                assert chunk == bytes((position + j) & 0xFF for j in range(len(chunk)))
                got += chunk
        assert len(got) >= 1000                             # 3 ms of 1 MHz at 4 bits a sample, as format 0 sends


def test_arm_1_waits_for_the_pretrigger_and_arm_0_does_not():
    for arm, index in ((1, 10), (0, 1)):
        ep, hst, lc, clock = bench()
        planned(hst, lc, 2)
        lc.configure(rate=1000, samples=32, trigger=(c.EDGE, 0, 0), pretrigger=10,
                     trigger_opts=c.TriggerOpts(arm=arm))
        lc.start()
        clock.t += 100
        (seg,) = lc.wait()
        assert seg.trigger_index == index                    # channel 0 rises at samples 1, 3, ...: arm 1 from 10 on


def test_level_when_0_needs_a_false_sample_and_when_1_does_not():
    for when, index in ((0, 2), (1, 0)):
        ep, hst, lc, clock = bench()
        planned(hst, lc, 2)
        ep.captures[lc.fn].value = lambda k, i: int(i < 3 or i > 6)   # high, low from 3, high again from 7
        lc.configure(rate=1000, samples=16, trigger=(c.LEVEL, 0, 1), pretrigger=2,
                     trigger_opts=c.TriggerOpts(when=when))
        lc.start()
        clock.t += 100
        (seg,) = lc.wait()
        assert seg.start_ns // 1_000_000 - ep.captures[lc.fn].t0_ns // 1_000_000 + seg.trigger_index == (7 if not when else 2)
        assert seg.trigger_index == 2


def test_rearm_repeat_each_segment_its_own_trigger_and_records():
    ep, hst, lc, clock = bench()
    planned(hst, lc, 6)
    lc.configure(rate=1000, mode=c.REPEAT, samples=8, segments=4, trigger=(c.EDGE, 5, 0), pretrigger=2,
                 trigger_opts=c.TriggerOpts(rearm=1))
    lc.subscribe()
    lc.start()
    clock.t += 400
    segs = lc.segments()
    assert len(segs) == 4 and all(s.trigger_index == 2 for s in segs)
    starts = [round((s.start_ns - segs[0].start_ns) / 1_000_000) for s in segs]
    assert starts == [0, 64, 128, 192]                       # channel 5 rises every 64 samples
    assert all(s.flags & 1 for s in segs[1:])                # not straight after the one before
    assert all(s.flags & c._CAP.enum["segment_flag"]["rearm"] for s in segs)    # each began at its own trigger
    assert not lc.status().flags & c.STATUS_FLAGS["dropped"]                     # a rearm wait is not a drop
    records = lc.triggers()
    assert [r.serial for r in records] == [0, 1, 2, 3] and [r.sample for r in records] == [32, 96, 160, 224]
    triggered = [c.unpack_event(f) for f in c.take_events(hst.link, lc.fn)] if hasattr(hst, "link") else None
    if triggered is not None:
        assert [e.record for e in triggered if e.kind == "triggered"] == [0, 1, 2, 3]


def test_rearm_repeat_status_says_it_waits_between_segments():
    ep, hst, lc, clock = bench()
    planned(hst, lc, 6)
    lc.configure(rate=1000, mode=c.REPEAT, samples=8, segments=4, trigger=(c.EDGE, 5, 0), pretrigger=2,
                 trigger_opts=c.TriggerOpts(rearm=1))
    lc.start()
    clock.t += 50                                            # the first segment (30 - 38) done, the next edge at 96
    st = lc.status()
    assert st.state == c.STATE["capturing"] and st.flags & c.STATUS_FLAGS["rearm_waiting"]
    lc.force()                                               # force works in that wait too (§2.1)
    clock.t += 20
    assert len(lc.segments()) == 2 and lc.status().flags & c.STATUS_FLAGS["rearm_waiting"]   # and waits again


def test_rearm_streaming_records_every_event_without_cutting():
    ep, hst, lc, clock = bench()
    planned(hst, lc, 6)
    lc.configure(rate=1000, mode=c.STREAMING, trigger=(c.EDGE, 4, 0), trigger_opts=c.TriggerOpts(rearm=1))
    lc.subscribe()
    lc.start()
    clock.t += 200
    events = [c.unpack_event(f) for f in ep.pushes() if f[0] == m.ROLE_EVENT]
    trig = [e for e in events if e.kind == "triggered"]
    assert [e.sample for e in trig] == [16, 48, 80, 112, 144, 176]
    assert [e.record for e in trig] == list(range(6))
    assert [r.sample for r in lc.triggers(3)] == [112, 144, 176]
    with pytest.raises(h.Rejected):
        lc.triggers(0, generation=lc.generation + 1)


def test_trigger_opts_beyond_the_declared_are_refused():
    ep, hst, lc, clock = bench()
    planned(hst, lc, 2)
    with pytest.raises(h.Unsupported):                       # hysteresis is an analog's
        lc.configure(rate=1000, samples=32, trigger=(c.EDGE, 0, 0), trigger_opts=c.TriggerOpts(hysteresis=5))


def streamed_segments(ep, segs):
    """position -> (bytes, samples) of each streamed segment, from the data frames' segment_info (§3.4)."""
    for f in ep.pushes():
        if f[0] != m.ROLE_DATA:
            continue
        _, _, position, chunk, _ = c.unpack_push(f)
        info = c.push_segment(f)
        data, _ = segs.get(info.position, (b"", 0))
        assert info.position + len(data) == position              # frames of a segment in order, never across one
        segs[info.position] = (data + chunk, info.samples)


def test_format_1_streaming_blocks_per_segment_and_a_short_end():
    ep, hst, lc, clock = bench()
    roles = planned(hst, lc, 10)
    events = {8: 16, 9: 16}
    cfg = lc.configure(rate=100_000, mode=c.STREAMING, data_format=LOGIC_BLOCKS, events=events, roles=roles,
                       block_max=512)
    assert cfg.block == 512
    lc.subscribe()
    lc.start()
    got = {}
    for _ in range(10):
        clock.t += 7
        streamed_segments(ep, got)
    clock.t += 3
    lc.stop()
    streamed_segments(ep, got)
    first = 0
    for k, position in enumerate(sorted(got)):
        data, samples = got[position]
        blocks = lc.blocks(data, samples)
        check_blocks(blocks, events, samples, first, contiguous_before=k > 0)
        first += samples
    assert first == 7300                                     # 73 ms at 100 kHz: all of it, the last block short
    st = lc.status()
    assert st.write_pos == sum(len(d) for d, _ in got.values()) and st.serial_done == len(got)


def test_format_1_streaming_drops_a_segment_whole_while_unsubscribed():
    ep, hst, lc, clock = bench()
    roles = planned(hst, lc, 10)
    events = {9: 16}
    lc.configure(rate=100_000, mode=c.STREAMING, data_format=LOGIC_BLOCKS, events=events, roles=roles, block_max=512)
    lc.subscribe()
    lc.start()
    got = {}
    clock.t += 15
    streamed_segments(ep, got)
    lc.unsubscribe()
    clock.t += 3                                             # inside a segment: it goes whole, the next ones too
    ep.pushes()
    lc.subscribe()
    for _ in range(6):
        clock.t += 7
        streamed_segments(ep, got)
    lc.stop()
    streamed_segments(ep, got)
    complete = 0
    for position in sorted(got):
        data, samples = got[position]
        lc.blocks(data, samples)                             # every segment received is whole from its start
        complete += 1
    assert complete >= 2 and lc.status().flags & c.STATUS_FLAGS["dropped"]


def test_logic_waves_quiet_and_uart_lines_carry_few_events():
    """--logic-wave: quiet and UART-like lines (§1.3's typical case) - their events stay far below the worst case."""
    from oep_client import virtual_bench_capture as vbc
    ep, hst, lc, clock = bench()
    roles = planned(hst, lc, 16)
    cap = ep.captures[lc.fn]
    for r in range(4, 16):
        cap.waves[r] = vbc.logic_wave("uart:115200" if r % 2 else "quiet")
    events = {r: 64 for r in range(4, 16)}
    lc.configure(rate=20_000_000, samples=65536, data_format=LOGIC_BLOCKS, events=events, roles=roles)
    lc.start()
    clock.t += 10
    (seg,) = lc.wait()
    data = lc.read_segment(seg)
    blocks = lc.blocks(data, seg.samples)
    assert not any(b.fallback for b in blocks)
    worst = cb.worst_bytes(4, list(events.values()), lc.config.block) * len(blocks)
    assert len(data) < 0.45 * worst                            # 4 raw lines dominate: 2048 B of a 5128 B worst block
    uart = blocks[0].events[5]
    assert cb.COUNT_SATURATED not in list(uart.counts)         # a 115200 bit is 173 samples: one change a tick at most
    assert vbc.logic_wave("uart:115200:A")(0, 20_000_000) == 0  # the start bit


def test_streaming_answer_says_its_stream_buffer():
    """§3.3 stream_buffer (mode 3 only): the host's tolerable read delay = stream_buffer / the data rate."""
    from oep_client import virtual_bench_capture as vbc
    ep, hst, lc, clock = bench()
    planned(hst, lc, 8)
    cfg = lc.configure(rate=10_000_000, mode=c.STREAMING, query=True)
    assert cfg.stream_buffer == vbc.STREAM_BUFFER_BYTES
    assert cfg.tolerable_delay_s() == pytest.approx(vbc.STREAM_BUFFER_BYTES / 10e6)
    assert lc.configure(rate=10_000_000, samples=100).stream_buffer is None    # one-shot: not said


def test_format_0_streaming_makes_a_tick_at_most_and_stop_answers():
    """Streaming format 0: a tick makes at most BLOCKS_A_TICK samples, so a bench far behind a fast rate still answers
    (the stream runs late) - catching up on all the time passed at once answered stop after 12.8 s (wireskein: 1 MHz x
    2 lines)."""
    from oep_client import virtual_bench_capture as vbc
    ep, hst, lc, clock = bench()
    planned(hst, lc, 2)
    lc.configure(rate=1_000_000, mode=c.STREAMING)
    lc.subscribe(16384, 20)
    lc.start()
    cap = ep.captures[lc.fn]
    clock.t += 2_000                                           # 2 s (within the lease): two million samples due
    cap.tick(clock.t)
    assert cap.produced == vbc.BLOCKS_A_TICK
    lc.stop()
    assert cap.produced <= 4 * vbc.BLOCKS_A_TICK < 2_000_000   # a tick or a few (the requests tick too), not 2 s
    assert lc.status().state == c.STATE["configured"]
