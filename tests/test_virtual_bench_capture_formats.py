"""Capture §1 data formats 1 (logic blocks) and 4 (test counter), §3.3 trigger_opts, §3.9 trigger records - through the
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


SAMPLE, ANY_HIGH, ANY_LOW, CHANGE_LATCH = cb.SAMPLE, cb.ANY_HIGH, cb.ANY_LOW, cb.CHANGE_LATCH


def want_values(role, kind, d, at, r, before):
    """§1.3's values for the virtual line `role` over samples at .. at + r - 1 (before: the level before, or None)."""
    out, last = [], before
    for lo in range(0, r, d):
        part = [level(role, at + i) for i in range(lo, min(lo + d, r))]
        if kind == SAMPLE:
            out.append(part[0])
        elif kind == ANY_HIGH:
            out.append(int(1 in part))
        elif kind == ANY_LOW:
            out.append(0 if 0 in part else 1)
        else:
            ch = 0
            for v in part:
                ch |= last is not None and v != last
                last = v
            out.append(part[-1] | ch << 1)
    return out


def check_blocks(blocks, reduced, samples, first, contiguous_before=None):
    for b in blocks:
        at = first + b.first
        for role, bits in b.raw.items():
            assert cb.bits_list(bits, b.samples) == [level(role, at + i) for i in range(b.samples)]
        for role, (kind, d) in reduced.items():
            before = level(role, at - 1) if (b.first or contiguous_before) else None
            assert b.values[role] == want_values(role, kind, d, at, b.samples, before)
            if kind == CHANGE_LATCH:
                assert b.starts[role] == (before if before is not None else level(role, at))
    assert sum(b.samples for b in blocks) == samples


def test_format_1_one_shot_raw_and_every_reduction():
    ep, hst, lc, clock = bench()
    roles = planned(hst, lc, 12)
    reduced = {2: (CHANGE_LATCH, 4), 8: (SAMPLE, 16), 9: (ANY_HIGH, 16), 11: (ANY_LOW, 32)}
    cfg = lc.configure(rate=1_000_000, samples=1000, data_format=LOGIC_BLOCKS, carriage=reduced, roles=roles,
                       block_max=256)
    assert (cfg.format, cfg.block, cfg.width, cfg.positions) == (LOGIC_BLOCKS, 256, 8, list(range(8)))
    lc.start()
    clock.t += 5
    (seg,) = lc.wait()
    data = lc.read_segment(seg)
    blocks = lc.blocks(data, seg.samples)
    assert [b.samples for b in blocks] == [256, 256, 256, 232]   # a partial last block
    check_blocks(blocks, reduced, seg.samples, 0)
    assert len(data) == cb.segment_bytes(seg.samples, 8, reduced, 256)   # fixed by the setting


def test_format_1_repeat_change_latch_follows_the_segment_before():
    ep, hst, lc, clock = bench()
    roles = planned(hst, lc, 10)
    reduced = {9: (CHANGE_LATCH, 8)}
    lc.configure(rate=1_000_000, mode=c.REPEAT, samples=200, segments=3, data_format=LOGIC_BLOCKS, carriage=reduced,
                 roles=roles)
    lc.start()
    clock.t += 5
    segs = lc.segments()
    assert len(segs) == 3 and not any(s.flags & 1 for s in segs)
    for k, s in enumerate(segs):
        check_blocks(lc.blocks(lc.read_segment(s), s.samples), reduced, s.samples, k * 200, contiguous_before=k > 0)


def test_format_1_contract():
    ep, hst, lc, clock = bench()
    roles = planned(hst, lc, 4)
    head = (m.tlv(c.MODE, bytes([c.ONE_SHOT])) + m.tlv(c.RATE, struct.pack("<I", 1000))
            + m.tlv(c.SAMPLES, struct.pack("<I", 64)) + m.tlv(c.FORMAT | c.CRITICAL, bytes([LOGIC_BLOCKS])))
    for kind, d in ((c.LOGIC_CARRIAGE["raw"], 4), (SAMPLE, 1)):   # raw with d > 1, a reduction with d 1: malformed
        with pytest.raises(h.Rejected) as e:
            lc._call(lc.CONFIGURE, head + m.tlv(c.CARRIAGE | c.CRITICAL, struct.pack("<BBI", 0, kind, d)))
        assert e.value.result.detail == m.MALFORMED
    with pytest.raises(h.Unsupported):                      # L can't be a multiple of 8 and 3 within 16
        lc.configure(rate=1000, samples=64, data_format=LOGIC_BLOCKS, carriage={3: (SAMPLE, 3)}, roles=roles,
                     block_max=16)
    with pytest.raises(h.Unsupported):                      # a critical TLV it does not know (core §2.3)
        lc._call(lc.CONFIGURE, m.tlv(c.MODE, bytes([c.ONE_SHOT])) + m.tlv(c.RATE, struct.pack("<I", 1000))
                 + m.tlv(c.SAMPLES, struct.pack("<I", 64))
                 + m.tlv((max(c._CAP.tlv["configure"].values()) + 1) | c.CRITICAL, bytes(1)))


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
    events = {8: (SAMPLE, 16), 9: (CHANGE_LATCH, 16)}
    cfg = lc.configure(rate=100_000, mode=c.STREAMING, data_format=LOGIC_BLOCKS, carriage=events, roles=roles,
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
    events = {9: (ANY_LOW, 16)}
    lc.configure(rate=100_000, mode=c.STREAMING, data_format=LOGIC_BLOCKS, carriage=events, roles=roles, block_max=512)
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


def test_logic_waves_quiet_and_uart_lines_reduced():
    """--logic-wave: quiet and UART-like lines change_latched at 64: the size is the setting's whatever the lines do; a
    quiet line never changes, a UART line's start bits show as changes."""
    from oep_client import virtual_bench_capture as vbc
    ep, hst, lc, clock = bench()
    roles = planned(hst, lc, 16)
    cap = ep.captures[lc.fn]
    for r in range(4, 16):
        cap.waves[r] = vbc.logic_wave("uart:115200" if r % 2 else "quiet")
    reduced = {r: (CHANGE_LATCH, 64) for r in range(4, 16)}
    lc.configure(rate=20_000_000, samples=65536, data_format=LOGIC_BLOCKS, carriage=reduced, roles=roles)
    lc.start()
    clock.t += 10
    (seg,) = lc.wait()
    data = lc.read_segment(seg)
    assert len(data) == cb.segment_bytes(seg.samples, 4, reduced, lc.config.block)
    cols = lc.columns(data, seg.samples)
    quiet = int.from_bytes(cols[4].data, "little")
    assert all(not (quiet >> (2 * k + 1)) & 1 for k in range(cols[4].n))   # never a change
    uart = int.from_bytes(cols[5].data, "little")
    assert any((uart >> (2 * k + 1)) & 1 for k in range(cols[5].n))       # some
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


def test_format_0_streaming_catches_up_over_ticks_and_stop_makes_every_sample():
    """Streaming format 0: a tick makes a bounded number of samples (a_tick), so a bench behind a fast rate still
    answers and catches up over ticks; stop makes every sample up to the stop (a probe sends what its stream buffer
    holds) - before, the stream ran late and ended before the stop (wireskein: a section past its end on a busy host)."""
    from oep_client import virtual_bench_capture as vbc
    ep, hst, lc, clock = bench()
    planned(hst, lc, 2)
    lc.configure(rate=1_000_000, mode=c.STREAMING)
    lc.subscribe(16384, 20)
    lc.start()
    cap = ep.captures[lc.fn]
    clock.t += 2_000                                           # 2 s (within the lease): two million samples due
    cap.tick(clock.t)
    assert cap.produced == cap.a_tick() < 2_000_000
    lc.stop()
    assert cap.produced == 2_000_000                           # all of them, at the stop
    assert lc.status().state == c.STATE["configured"]


def test_streaming_behind_more_than_its_buffer_stops_in_error(monkeypatch):
    """Behind by more than its stream buffer holds, the track stops in error as a probe whose buffer overflowed: never a
    stream that silently ran late."""
    from oep_client import virtual_bench_capture as vbc
    monkeypatch.setattr(vbc, "STREAM_BUFFER_BYTES", 1 << 12)  # 16384 samples of 2 lines: 16 ms at 1 MS/s
    ep, hst, lc, clock = bench()
    planned(hst, lc, 2)
    lc.configure(rate=1_000_000, mode=c.STREAMING)
    lc.subscribe(16384, 20)
    lc.start()
    cap = ep.captures[lc.fn]
    assert cap.held_most() == 16384
    clock.t += 30                                              # more than the buffer's time, in one go
    cap.tick(clock.t)
    st = lc.status()
    assert st.state == c.STATE["error"]


def test_format_1_with_every_channel_reduced_has_w_and_c_0():
    ep, hst, lc, clock = bench()
    roles = planned(hst, lc, 3)
    events = {r: (SAMPLE, 8) for r in roles}
    cfg = lc.configure(rate=1000, samples=64, data_format=LOGIC_BLOCKS, carriage=events, roles=roles, block_max=64)
    assert (cfg.width, cfg.positions) == (0, [])             # §1.1's w range's exception: no raw part
    lc.start()
    clock.t += 100
    (seg,) = lc.wait()
    check_blocks(lc.blocks(lc.read_segment(seg), seg.samples), events, seg.samples, 0)


def test_trigger_records_keep_the_newest_and_an_old_serial_reads_from_the_oldest_kept():
    ep, hst, lc, clock = bench()
    planned(hst, lc, 3)
    lc.configure(rate=1000, mode=c.STREAMING, trigger=(c.EDGE, 2, 0), trigger_opts=c.TriggerOpts(rearm=1))
    lc.subscribe()
    lc.start()
    clock.t += 200                                           # channel 2 rises every 8 samples: 25 events
    ep.pushes()
    kept = lc.triggers(0)
    serials = [r.serial for r in kept]
    assert len(serials) == 16 and serials == list(range(serials[0], serials[0] + 16)) and serials[0] > 0
    assert [r.serial for r in lc.triggers(serials[0] + 3)] == serials[3:]


def test_when_1_counts_at_every_arming_of_a_rearming_repeat():
    ep, hst, lc, clock = bench()
    planned(hst, lc, 6)
    lc.configure(rate=1000, mode=c.REPEAT, samples=8, segments=3, trigger=(c.LEVEL, 5, 1),
                 trigger_opts=c.TriggerOpts(rearm=1, when=1))
    lc.start()
    clock.t += 200
    segs = lc.segments()
    # channel 5 is high for samples 32-63, 96-127, ...: when 1 takes the level true at the arming, so the second
    # segment begins at once after the first (40), still in the high half
    assert [round((s.start_ns - segs[0].start_ns) / 1_000_000) for s in segs] == [0, 8, 16]
