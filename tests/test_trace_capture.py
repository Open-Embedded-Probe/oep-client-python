"""Rule-by-rule trace of oep-spec a09fa45 interfaces/oep-if-capture.ja.md (docs/trace/capture.json): the names (capture
§0), the data layouts (§1), modes and segments (§2), the operations, configure, notifications, describe and calibration
(§3) and oep.fixture.capture-group (§4), checked on the virtual bench and the client where no other test pins a rule
down."""

import copy
import dataclasses
import struct
from fractions import Fraction

import pytest

from oep_client import capture as c, core, endpoint, host as h, message as m, registry as reg, virtual_bench
from oep_client import virtual_bench_capture as vbc
from test_virtual_bench_capture import Clock, _configure_body, counter_ok

LOGIC, ANALOG, GROUP = reg.FIXTURE_LOGIC, reg.FIXTURE_ANALOG, reg.FIXTURE_CAPTURE_GROUP
DESCRIBE = LOGIC.tlv["describe"]
ANSWER = ANALOG.tlv["configure_answer"]
ST = c.STATE
GAP, SHORT, SLIPPED = (LOGIC.enum["segment_flag"][k] for k in ("gap", "short", "slipped"))
DROPPED = LOGIC.enum["status_flag"]["dropped"]


# ---- helpers --------------------------------------------------------------------------------------------------------

def bench(profile=virtual_bench.p4_x035):
    clock = Clock()
    ep = endpoint.Endpoint(profile() if callable(profile) else profile, clock)
    hst = h.Host(lambda b: ep.handle(b, 0))
    hst.open(3000)
    return ep, hst, clock


def track(hst, analog=False, roles=(0, 1)):
    """A planned logic (channels 30..) or analog (channels 16..) track with these roles."""
    t = c.AnalogCapture(hst) if analog else c.LogicCapture(hst)
    base = 16 if analog else 30
    core.plan_apply(hst, [(t.fn, r, base + i) for i, r in enumerate(roles)])
    return t


def rate(analog):
    return 10_000 if analog else 1_000_000


def ask(hst, fn, op, body=b"", locked=True) -> m.Result:
    """The probe's answer, a rejection included."""
    try:
        return hst.request(fn, op, body, locked=locked)
    except h.Rejected as e:
        return e.result


def cause(r: m.Result) -> int:
    assert r.resolution == m.REJECTED and r.detail == m.UNAVAILABLE, r.describe()
    return dict(m.split_tlvs(r.payload))[reg.CORE.tlv["unavailable_payload"]["cause"]][0]


def unsupported_tag(r: m.Result) -> int:
    assert r.resolution == m.REJECTED and r.detail == m.UNSUPPORTED, r.describe()
    return r.payload[0]


def malformed(r: m.Result) -> bool:
    return r.resolution == m.REJECTED and r.detail == m.MALFORMED


def describe(hst, fn) -> list[tuple[int, bytes]]:
    r = hst.request(0, m.OP_DESCRIBE, struct.pack("<HH", fn, 0), locked=False)
    assert r.succeeded and r.payload[0] == 0
    return m.split_tlvs(r.payload[1:])


def snap(ep, fn):
    return copy.deepcopy((ep.captures[fn], ep.plan, ep.gpio_modes))


def with_inner(profile=virtual_bench.p4_x035, name="oep.fixture.logic", **inner):
    """The profile with fn `name`'s own (undeclared) choices replaced (VIRTUAL BENCH ONLY: a probe of another shape)."""
    p = profile()
    return virtual_bench._again(p, [dataclasses.replace(o, inner=tuple({**dict(o.inner), **inner}.items()))
                                    if o.name == name else o for o in p.offered])


def events(ep, fn):
    return [c.unpack_event(f, group=fn == ep.fns.get("oep.fixture.capture-group"))
            for f in ep.pushes() if f[0] == m.ROLE_EVENT and struct.unpack_from("<H", f, 1)[0] == fn]


CFG = reg.PROBE_CONFIG


def output_idle(hst, ep, ch, mode="output_high"):
    """probe.config set: channel ch's idle is an output (core §8)."""
    item = m.tlv(CFG.tlv["item"]["idle"], struct.pack("<HB", ch, CFG.enum["idle_mode"][mode]))
    assert hst.request(ep.fns[CFG.name], CFG.op["set"], bytes([1]) + item).succeeded
    assert ep.pin_state(ch) == f"idle {mode.replace('_', '-')}"


def logic_of(width, positions):
    lc = c.LogicCapture.__new__(c.LogicCapture)
    lc.config = c.Config(rate=Fraction(1), width=width, positions=list(positions))
    return lc


def analog_of(slot, offset, bits, order, zero=None, scale=None):
    an = c.AnalogCapture.__new__(c.AnalogCapture)
    an.config = c.Config(rate=Fraction(1), slot=slot, offset=offset, bits=bits, order=list(order),
                         zero=zero or {k: 0 for k in order}, scale_nv=scale or {k: 1_000_000 for k in order})
    return an


# ---- §0 the interfaces ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["oep.fixture.logic", "oep.fixture.analog", "oep.fixture.capture-group"])
def test_names_are_revision_1_and_offered_as_such(name):
    """capture §0: the three interfaces are revision 1; the bench lists each under its name at revision 1. logic and
    analog have the same op numbers and shapes (analog adds calibration only)."""
    assert reg.INTERFACES[name].revision == 1
    ep, hst, _ = bench()
    fn = ep.fns[name]
    assert ep.names[fn] == name and all(o.revision == 1 for o in virtual_bench.p4_x035().offered if o.name == name)
    assert {k: v for k, v in ANALOG.op.items() if k != "calibration"} == LOGIC.op
    assert c.CaptureGroup(hst).fn == ep.fns["oep.fixture.capture-group"]


# ---- §1.1 the logic layout ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("channels, widths, w", [(1, (1, 2, 4, 8, 16), 1), (3, (1, 2, 4, 8, 16), 4),
                                                 (3, (3,), 3), (2, (8,), 8), (9, (16,), 16), (5, (128,), 128)])
def test_logic_layout_w_c_pos_and_n_are_the_probes(channels, widths, w):
    """capture §1.1: configure answers w (1-128, the probe's choice, may exceed the channel count), C = the planned
    logic roles, pos[k] distinct and below w in role order, N >= 1 (actual_samples)."""
    ep, hst, clock = bench(with_inner(widths=widths))
    lc = track(hst, roles=range(channels))
    cfg = lc.configure(rate=1_000_000, samples=24)
    assert cfg.width == w and 1 <= w <= 128 and w >= channels
    assert cfg.positions == list(range(channels)) and len(set(cfg.positions)) == channels
    assert all(0 <= p < w for p in cfg.positions) and cfg.samples == 24
    layout = dict(m.split_tlvs(hst.request(lc.fn, lc.QUERY_OP, _configure_body(samples=24), locked=False).payload))
    assert layout[ANSWER["layout"]][1] == channels                  # C
    assert malformed(ask(hst, lc.fn, lc.CONFIGURE, _configure_body(samples=0)))   # N 0: not a value (§3.3)


@pytest.mark.parametrize("w", [1, 2, 3, 4, 5, 7, 8, 9, 12, 16, 24, 127, 128])
def test_logic_sample_i_is_bits_i_w_and_channel_k_bit_pos_k(w):
    """capture §1.1 rules 1, 2 and 4: sample i is bits i*w .. i*w+w-1 of the segment (bit j = bit j mod 8 of byte j/8,
    LSB first), channel k is bit i*w + pos[k], the segment is ceil(N*w/8) bytes - the bench packs, the client reads."""
    n = 11
    values = [(0x5A5A5A5A5A5A5A5A5A5A5A5A5A5A5A5A * (i + 1) + i) % (1 << w) for i in range(n)]
    data = vbc.pack_samples(values, w)
    assert len(data) == -(-n * w // 8)
    bits = int.from_bytes(data, "little")
    assert [(bits >> (i * w)) & ((1 << w) - 1) for i in range(n)] == values
    positions = sorted({0, w - 1, w // 2})
    lc = logic_of(w, positions)
    for k, p in enumerate(positions):
        assert lc.channel(data, k, n) == [(v >> p) & 1 for v in values]
    assert c.Config(width=w, positions=positions, samples=n).bytes == -(-n * w // 8)


def test_logic_bits_no_channel_hits_are_ignored():
    """capture §1.1 rule 3 and 4: bits on no pos[k] (and the last byte's bits past N*w) are undefined - the client reads
    the same channels whatever they hold."""
    lc = logic_of(4, [0, 1, 2])
    clean, dirty = bytes([0b0101_0011, 0b0000_0110]), bytes([0b1101_1011, 0b1111_1110])   # bit 3 / 7 and the pad set
    assert [lc.channel(clean, k, 3) for k in range(3)] == [lc.channel(dirty, k, 3) for k in range(3)]


def test_logic_whole_byte_samples_are_little_endian_integers():
    """capture §1.1 rule 5: with w a multiple of 8 a sample is the w/8-byte little-endian integer."""
    values = [0x1FF, 0x0A5, 0x100]
    data = vbc.pack_samples(values, 16)
    assert data == b"".join(v.to_bytes(2, "little") for v in values)
    lc = logic_of(16, range(9))
    assert [sum(lc.channel(data, k, 3)[i] << k for k in range(9)) for i in range(3)] == values


@pytest.mark.parametrize("w, pos, raw, want", [
    (1, [0], bytes([0b10110001]), [[1, 0, 0, 0, 1, 1, 0, 1]]),                       # samples 0-7 are bits 0-7
    (3, [0, 1, 2], bytes([0b10_011_101, 0b1_110_100_0, 0b000_111_01]),                 # 8 samples in 3 bytes
     [[1, 1, 0, 0, 0, 1, 1, 0], [0, 1, 1, 0, 1, 1, 1, 0], [1, 0, 0, 1, 1, 0, 1, 0]]),
    (8, [0], bytes([0xFE, 0x01]), [[0, 1]]),                                           # a byte a sample, 1-7 undefined
    (8, [5], bytes([0xDF, 0x20]), [[0, 1]]),                                           # only bit 5 means anything
    (4, [0, 1, 2], bytes([0b1110_1001]), [[1, 0], [0, 1], [0, 1]]),                    # bits 3 and 7 undefined
    (8, [0, 1, 2], bytes([0b11111_101, 0b00000_010]), [[1, 0], [0, 1], [1, 0]]),       # bits 3-7 undefined
    (16, list(range(9)), bytes([0xFF, 0xFF, 0x00, 0x01]), [[1, 0]] * 8 + [[1, 1]]),     # 2 bytes LE, 9-15 undefined
], ids=["w1-1ch", "w3-3ch", "w8-1ch", "w8-bit5", "w4-3ch", "w8-3ch", "w16-9ch"])
def test_logic_layout_examples(w, pos, raw, want):
    """capture §1.1's examples: each configuration reads as its row says (the undefined bits set where they are)."""
    lc = logic_of(w, pos)
    n = len(want[0])
    assert [lc.channel(raw, k, n) for k in range(len(pos))] == want
    assert c.Config(width=w, positions=pos, samples=n).bytes == len(raw)


def test_logic_three_bit_sample_2_straddles_bytes_0_and_1():
    """capture §1.1's w 3 example: sample i is bits 3i .. 3i+2; sample 2 is byte 0 bits 6-7 and byte 1 bit 0."""
    lc = logic_of(3, [0, 1, 2])
    for raw, value in ((bytes([0b0100_0000, 0b0000_0000, 0]), 0b001), (bytes([0b1000_0000, 0, 0]), 0b010),
                       (bytes([0, 0b0000_0001, 0]), 0b100)):
        assert [lc.channel(raw, k, 8)[2] for k in range(3)] == [(value >> k) & 1 for k in range(3)]
        assert all(lc.channel(raw, k, 8)[i] == 0 for k in range(3) for i in range(8) if i != 2)


def test_logic_three_bit_samples_through_the_bench():
    """capture §1.1 (w 3, 3 channels): a probe that packs 3 bits a sample - the bench's counter read back by the client,
    8 samples in 3 bytes."""
    ep, hst, clock = bench(with_inner(widths=(3,)))
    lc = track(hst, roles=(0, 1, 2))
    lc.configure(rate=1_000_000, samples=64)
    lc.start()
    clock.t += 1
    (seg,) = lc.wait()
    data = lc.read_segment(seg)
    assert len(data) == 24
    counter_ok(lc, data, 64)


# ---- §1.2 the analog layout -----------------------------------------------------------------------------------------

def test_analog_layout_s_o_b_c_order_n():
    """capture §1.2: s is 8/16/32, 0 <= o, 1 <= b <= 31, o + b <= s; C = the planned analog roles, order holds each of
    them once; N >= 1."""
    ep, hst, clock = bench()
    an = track(hst, analog=True, roles=(0, 2, 3))
    cfg = an.configure(rate=10_000, samples=5)
    assert cfg.slot in (8, 16, 32) and cfg.offset >= 0 and 1 <= cfg.bits <= 31 and cfg.offset + cfg.bits <= cfg.slot
    assert sorted(cfg.order) == [0, 2, 3] and len(cfg.order) == 3 and cfg.samples == 5
    assert malformed(ask(hst, an.fn, an.CONFIGURE, _configure_body(rate=10_000, samples=0)))


@pytest.mark.parametrize("slot, offset, bits", [(8, 0, 8), (16, 0, 12), (16, 4, 12), (32, 0, 12), (32, 1, 31)])
def test_analog_slots_frames_and_value_bits(slot, offset, bits):
    """capture §1.2 rules 1-3: a slot is an s/8-byte little-endian integer whose bits o .. o+b-1 are the value (other
    bits undefined, ignored); sample i is slots i*C .. i*C+C-1, slot m is channel order[m]; N*C*s/8 bytes."""
    order = [3, 1]
    values = {3: [5, (1 << bits) - 1, 0], 1: [1, 2, (1 << bits) - 2]}
    junk = ((1 << slot) - 1) ^ (((1 << bits) - 1) << offset)          # every bit outside the value set
    data = b"".join((((values[k][i] << offset) | junk) & ((1 << slot) - 1)).to_bytes(slot // 8, "little")
                    for i in range(3) for k in order)
    an = analog_of(slot, offset, bits, order)
    assert an.values(data, 3, 3) == values[3] and an.values(data, 1, 3) == values[1]
    assert len(data) == 3 * 2 * slot // 8 == c.Config(slot=slot, order=order, samples=3).bytes


def test_analog_voltage_skew_and_clipping():
    """capture §1.2 rules 4-6: voltage = (value - zero) x scale_nv at the input pin; channel k's time is the sample's
    plus skew_ns[k]; 0 and 2^b - 1 are clipped ends (with a negative scale, 0 is the high end)."""
    an = analog_of(16, 0, 12, [0, 1], zero={0: 100, 1: 0}, scale={0: 2_000_000, 1: -1_000_000})
    assert an.millivolts(0, 600) == 1000 and an.millivolts(1, 500) == -500
    assert an.clipped(0, 0) == an.CLIP_LOW and an.clipped(0, 4095) == an.CLIP_HIGH and an.millivolts(0, 0) is None
    assert an.clipped(1, 0) == an.CLIP_HIGH and an.clipped(1, 4095) == an.CLIP_LOW
    assert an.ends_millivolts(0) == (-200, (4095 - 100) * 2)
    ep, hst, clock = bench()
    an = track(hst, analog=True, roles=(0, 1))
    cfg = an.configure(rate=10_000, samples=8)
    assert cfg.skew_ns[0] == 0 and cfg.skew_ns[1] == int(Fraction(10**9) / (cfg.rate * 2))   # one ADC in turn
    an.start()
    clock.t += 5
    (seg,) = an.wait()
    data = an.read_segment(seg)
    assert an.values(data, 0, 8) == [vbc.analog_value(0, i) for i in range(8)]   # the raw values, as sent


@pytest.mark.parametrize("slot, offset, bits, record, value", [
    (32, 0, 12, 0x0001_3ABC, 0xABC),        # DMA record: bits 13-16 (channel number) undefined
    (16, 0, 12, 0x3ABC, 0xABC),             # 2-byte record: top 4 bits undefined
    (16, 0, 12, 0x0ABC, 0xABC),             # FIFO 16 bits
    (8, 0, 8, 0xA5, 0xA5),                  # an 8-bit ADC
])
def test_analog_layout_examples(slot, offset, bits, record, value):
    """capture §1.2's examples: the value is bits o .. o+b-1 of the record, whatever the rest holds."""
    an = analog_of(slot, offset, bits, [0])
    assert an.values(record.to_bytes(slot // 8, "little"), 0, 1) == [value]


def test_logic_plan_on_an_output_idle_channel_listens_without_driving():
    """capture §1.2: a logic plan takes a channel whose idle is an output and observes it; the drive does not change
    (the pin state is the idle's before and after the plan and the capture)."""
    ep, hst, clock = bench()
    output_idle(hst, ep, 30)
    before = ep.pin_state(30)
    lc = c.LogicCapture(hst)
    core.plan_apply(hst, [(lc.fn, 0, 30)])
    lc.configure(rate=1_000_000, samples=8)
    lc.start()
    clock.t += 1
    lc.wait()
    assert ep.pin_state(30) == before


def test_logic_capture_never_changes_a_driven_pin():
    """capture §1.2 (core §8): taking a plan changes nothing electrical; a logic capture only listens - a gpio output it
    shares a pin with keeps its drive through configure, start, completion and stop."""
    ep, hst, clock = bench()
    gpio = ep.fns["oep.fixture.gpio"]
    core.plan_apply(hst, [(gpio, 1, 30)])
    hst.call(gpio, reg.FIXTURE_GPIO.op["set"], b"\x01" + struct.pack("<HB", 30, 4))
    driven = ep.pin_state(30)
    lc = c.LogicCapture(hst)
    core.plan_apply(hst, [(lc.fn, 0, 30)])
    assert ep.pin_state(30) == driven
    lc.configure(rate=1_000_000, samples=8)
    lc.start()
    assert ep.pin_state(30) == driven
    clock.t += 1
    lc.wait()
    lc.stop()
    assert ep.pin_state(30) == driven


def test_analog_pad_leaves_idle_only_while_it_acquires():
    """capture §1.2: an analog plan and configure leave the pad idle; start takes it to the analog function; stop,
    completion, state 5 (no free segment) and an error give it back to idle with the plan kept; leaving state 5 takes it
    again."""
    ep, hst, clock = bench()
    an = track(hst, analog=True, roles=(0,))
    idle = ep.pin_state(16)
    an.configure(rate=1000, samples=8)
    assert ep.pin_state(16) == idle
    an.start()
    assert ep.pin_state(16) == "analog"
    clock.t = 20                                                    # complete (state 4)
    assert an.status().state == ST["done"] and ep.pin_state(16) == idle
    an.start()
    assert ep.pin_state(16) == "analog"
    an.stop()                                                       # stop (state 1)
    assert ep.pin_state(16) == idle and (an.fn, 0, 16) in ep.plan
    an.configure(rate=1000, mode=c.REPEAT, samples=8, segments=1)
    an.start()
    clock.t = 40                                                    # the one segment is full: state 5
    assert an.status().state == ST["paused"] and ep.pin_state(16) == idle
    an.release(0)                                                   # room again: acquiring
    assert an.status().state == ST["capturing"] and ep.pin_state(16) == "analog"
    ep.capture_overflow(an.fn)                                      # an error (state 6)
    assert an.status().state == ST["error"] and ep.pin_state(16) == idle and (an.fn, 0, 16) in ep.plan


def test_analog_plan_on_an_output_idle_channel_is_unavailable_cause_5():
    """capture §1.2: an analog plan on a channel whose idle is an output (mode 3 / 4) is rejected unavailable cause 5;
    a logic plan there is taken (it listens)."""
    ep, hst, clock = bench()
    output_idle(hst, ep, 16, "output_low")
    output_idle(hst, ep, 17, "output_high")
    for ch in (16, 17):
        r = ask(hst, ep.fns["oep.probe.plan"], reg.PROBE_PLAN.op["plan_apply"],
                m.tlv(reg.PROBE_PLAN.tlv["plan_apply"]["role_assignment"],
                      struct.pack("<HBH", ep.fns["oep.fixture.analog"], 0, ch), critical=True))
        assert cause(r) == reg.CORE.enum["unavailable_cause"]["held_by_settings"]
    core.plan_apply(hst, [(ep.fns["oep.fixture.logic"], 0, 16)])


def test_analog_plan_refuses_a_pin_without_an_adc_channel():
    """capture §3.1: a pin with no ADC channel is refused in an analog plan (the bench's ADC is GPIO 16-23)."""
    ep, hst, clock = bench()
    an = c.AnalogCapture(hst)
    with pytest.raises(h.Rejected):
        core.plan_apply(hst, [(an.fn, 0, 30)])
    assert not ep.plan
    core.plan_apply(hst, [(an.fn, 0, 23)])


# ---- §2.1 modes -----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("analog", [False, True])
def test_one_shot_takes_samples_stops_in_4_and_the_next_start_clears(analog):
    """capture §2.1 / §2.2 one-shot: after start it takes `samples` samples and stops (state 4); its one segment is
    serial 0; the host reads at its own pace; the next start clears it (a new generation)."""
    ep, hst, clock = bench()
    t = track(hst, analog)
    t.configure(rate=rate(analog), samples=16)
    t.start()
    clock.t = 50
    st = t.status()
    assert st.state == ST["done"] and st.serial_done == 1
    (seg,) = t.segments()
    assert (seg.serial, seg.position, seg.samples, seg.generation) == (0, 0, 16, 1)
    clock.t = 500                                                  # still there later
    assert len(t.read_segment(seg)) == t.config.segment_bytes(16) and t.status().state == ST["done"]
    t.start()
    assert t.generation == 2 and t.segments() == [] and t.status().write_pos == 0
    assert cause(ask(hst, t.fn, t.READ, struct.pack("<IQH", 1, 0, 8), locked=False)) == 6


def test_repeat_follows_on_without_gaps_into_free_segments_only():
    """capture §2.1 / §2.2 repeat: one-shots one after another with no gap at the boundaries, into free segments only;
    with none free it stops (state 5) and release lets it go on; the first segment after that has flags bit0."""
    ep, hst, clock = bench()
    lc = track(hst)
    lc.configure(rate=1_000_000, mode=c.REPEAT, samples=1000, segments=2)
    lc.start()
    clock.t = 5
    assert lc.status().state == ST["paused"]
    a, b = lc.segments()
    assert b.position == a.position + lc.config.segment_bytes(a.samples) and not b.flags & GAP
    assert b.start_ns == a.start_ns + 1_000_000
    lc.release(a.serial)
    assert lc.status().state == ST["capturing"]
    clock.t = 6
    c3 = lc.segments(2)[0]
    assert c3.flags & GAP and c3.position >= b.position + lc.config.segment_bytes(b.samples)


def test_streaming_sends_while_the_lock_holder_subscribes():
    """capture §2.1 streaming: the bytes follow on with no gap and go out as data notifications while subscribed;
    no segment events (rule 3), stopped is sent."""
    ep, hst, clock = bench()
    lc = track(hst)
    lc.configure(rate=1000, mode=c.STREAMING)
    lc.subscribe()
    lc.start()
    got = []
    for t in (8, 16, 24):
        clock.t = t
        got += [f for f in ep.pushes()]
    data = [c.unpack_push(f) for f in got if f[0] == m.ROLE_DATA]
    assert [p[2] for p in data] == [0, 2, 4] and all(len(p[3]) == 2 for p in data)
    assert not [f for f in got if f[0] == m.ROLE_EVENT]
    lc.stop()
    assert [e.kind for e in events(ep, lc.fn)] == ["stopped"]


@pytest.mark.parametrize("analog", [False, True])
def test_no_sample_is_taken_before_start(analog):
    """capture §2.1: the probe takes samples only after start - a configured track waiting a long time before start
    still has its first sample at start (start_ns), and a trigger that holds at once keeps no pretrigger before it."""
    ep, hst, clock = bench()
    t = track(hst, analog)
    trig = (c.CROSS_UP, 1, 2100) if analog else (c.LEVEL, 0, 0)    # the sine's sample 1 / bit 0 low at sample 0
    first = 1 if analog else 0
    t.configure(rate=1000, samples=16, trigger=trig, pretrigger=8)
    clock.t = 100
    t.start()
    clock.t = 200
    (seg,) = t.segments()
    assert seg.trigger_index == first and seg.samples == 8 + first and seg.flags & SHORT
    assert seg.start_ns >= 100_000_000


def test_streaming_without_room_drops_and_the_position_jumps():
    """capture §2.1 rule 1 / §3.2: with no room to send (here: the subscription gone while it captures) the probe goes
    on capturing and drops what it cannot send - the next data frame's position jumps and status flags bit0 says data
    was dropped."""
    ep, hst, clock = bench()
    lc = track(hst)
    lc.configure(rate=1000, mode=c.STREAMING)
    cap = ep.captures[lc.fn]
    cap.samples = 8                                                 # small segments (the probe's own split, §2.2)
    seg = cap.segment_bytes()
    lc.subscribe()
    lc.start()
    clock.t = 8
    first = [c.unpack_push(f) for f in ep.pushes() if f[0] == m.ROLE_DATA]
    end = first[-1][2] + len(first[-1][3])
    lc.unsubscribe()
    clock.t = 40
    assert ep.pushes() == []
    assert lc.status().state == ST["capturing"]                     # it goes on capturing
    lc.subscribe()                                                  # inside a segment begun with no room ...
    clock.t = 43
    assert not [f for f in ep.pushes() if f[0] == m.ROLE_DATA]      # ... that segment goes whole (rule 1)
    clock.t = 60
    later = [c.unpack_push(f) for f in ep.pushes() if f[0] == m.ROLE_DATA]
    assert later and later[0][2] > end and later[0][2] % seg == 0   # the position jumped, to a segment's start
    assert lc.status().flags & DROPPED


def test_after_stop_the_rest_goes_without_waiting_for_the_batching():
    """capture §2.1 rule 2: after stop the bytes taken before it and not sent yet are sent, without waiting for
    min_bytes or max_delay_ms; write_pos after stop is where the received data ends."""
    ep, hst, clock = bench()
    lc = track(hst)
    lc.configure(rate=1000, mode=c.STREAMING)
    lc.subscribe(min_bytes=60000, max_delay_ms=60000)
    lc.start()
    clock.t = 40
    assert [f for f in ep.pushes() if f[0] == m.ROLE_DATA] == []    # held by the batching
    lc.stop()
    data = [c.unpack_push(f) for f in ep.pushes() if f[0] == m.ROLE_DATA]
    assert sum(len(p[3]) for p in data) == 10 and data[-1][2] + len(data[-1][3]) == lc.status().write_pos
    spans = [(p[4], p[2], p[2] + len(p[3])) for p in data]
    assert c.coverage(spans, lc.generation, lc.status().write_pos) == (True, True)
    assert c.coverage(spans[1:], lc.generation, lc.status().write_pos) in ((True, False), (False, False))


def test_streaming_read_of_pushed_data_points_at_write_pos_with_gap():
    """capture §2.1 rule 4 / §3.2: data sent by notification may not be readable; with nothing readable after the
    asked position, read answers position = write_pos, gap 1, len 0."""
    ep, hst, clock = bench()
    lc = track(hst)
    lc.configure(rate=1000, mode=c.STREAMING)
    lc.subscribe()
    lc.start()
    clock.t = 16
    ep.pushes()
    r = hst.request(lc.fn, lc.READ, struct.pack("<IQH", lc.generation, 0, 8), locked=False).payload
    position, flags, n = struct.unpack_from("<QBH", r)
    assert (position, flags & 2, n) == (lc.status().write_pos, 2, 0)


# ---- §2.2 segments --------------------------------------------------------------------------------------------------

def test_streaming_segments_are_the_probes_and_every_frame_says_which():
    """capture §2.2 streaming / §3.4: segments are the probe's own pieces; each data frame stays in one segment and
    carries its segment_info (the segment's position, the samples taken so far, its start_ns and uncertainty) and the
    generation."""
    ep, hst, clock = bench()
    lc = track(hst)
    lc.configure(rate=1000, mode=c.STREAMING)
    ep.captures[lc.fn].samples = 16                                 # the probe's own piece: 16 samples, 4 bytes
    lc.subscribe()
    clock.t = 3
    lc.start()
    frames = []
    for t in (9, 13, 25, 40):
        clock.t = t
        frames += [f for f in ep.pushes() if f[0] == m.ROLE_DATA]
    for f in frames:
        fn, _, position, data, generation = c.unpack_push(f)
        info = c.push_segment(f)
        assert generation == lc.generation and info.position <= position
        assert position + len(data) <= info.position + 4             # within its segment
        assert info.start_ns == 3_000_000 + (info.position // 4) * 16_000_000 and info.start_uncertainty_ns == 50
        assert 1 <= info.samples <= 16 and info.samples * 2 >= (position + len(data) - info.position) * 8 // 1
    assert {c.push_segment(f).position for f in frames} == {0, 4, 8}


def test_segment_serials_wrap_and_compare_by_core_2_6():
    """capture §2.2 serial: from 0 at start, a u32 that wraps; release compares by core §2.6 (inclusive), so serials
    0xFFFFFFFE, 0xFFFFFFFF, 0 follow on and release(0) frees all three."""
    ep, hst, clock = bench()
    lc = track(hst)
    lc.configure(rate=1000, mode=c.REPEAT, samples=8, segments=4)
    lc.start()
    assert lc.status().serial_done == 0
    ep.captures[lc.fn].serial_done = 0xFFFFFFFE                     # TEST SETUP: as if 2^32 - 2 segments had passed
    clock.t = 24
    segs = lc.segments(0xFFFFFFFE)
    assert [s.serial for s in segs] == [0xFFFFFFFE, 0xFFFFFFFF, 0] and lc.status().serial_done == 1
    assert [s.position for s in segs] == [0, 2, 4]                  # position: u64, never wraps, from start
    lc.release(0)
    assert lc.segments(0xFFFFFFFE) == []


def test_segment_fields_position_samples_times_trigger_flags_generation():
    """capture §2.2: position is the segment's first byte (the read space); samples (short at stop, flags bit1);
    start_ns is the first sample's time on the probe's clock with its uncertainty; trigger_index is the trigger's sample
    or 0xFFFFFFFF; generation is the segment's start."""
    ep, hst, clock = bench()
    lc = track(hst)
    clock.t = 2
    lc.configure(rate=1000, mode=c.REPEAT, samples=8, segments=4, trigger=(c.EDGE, 0, 0), pretrigger=2)
    lc.start()
    clock.t = 20
    first, second = lc.segments()[:2]
    assert first.trigger_index == 1 and second.trigger_index is None and first.flags & SHORT
    raw = hst.request(lc.fn, lc.SEGMENTS, struct.pack("<I", second.serial), locked=False).payload
    assert struct.unpack_from("<I", raw, 2 + 28)[0] == 0xFFFFFFFF   # trigger_index of a segment without the trigger
    assert first.start_ns == 2_000_000 and second.start_ns == first.start_ns + first.samples * 1_000_000
    assert first.start_uncertainty_ns == 50 and {first.generation, second.generation} == {1}
    assert lc.read(second.position, 2) == lc.read_segment(second)
    lc.stop()
    last = lc.segments()[-1]
    assert last.flags & SHORT and last.samples < 8


def test_repeat_segments_follow_on_unless_flags_bit0():
    """capture §2.2: inside a segment the samples run on; in repeat a segment follows its predecessor directly unless
    flags bit0 says there was a gap (the counter goes on sample by sample across segments)."""
    ep, hst, clock = bench()
    lc = track(hst)
    lc.configure(rate=1000, mode=c.REPEAT, samples=8, segments=3)
    lc.start()
    clock.t = 24
    segs = lc.segments()
    assert not any(s.flags & GAP for s in segs)
    counter_ok(lc, b"".join(lc.read_segment(s) for s in segs), 24)
    for a, b in zip(segs, segs[1:]):
        assert b.position == a.position + 2 and b.start_ns == a.start_ns + 8_000_000


@pytest.mark.parametrize("analog", [False, True])
@pytest.mark.parametrize("error", [1, 2])
def test_a_drop_inside_a_segment_is_an_error_not_a_gap(analog, error):
    """capture §2.2: a sample lost for another reason than room (overflow: error 2, a DMA / peripheral failure: 1) is a
    drop inside its segment: that segment is no data (not in segments, no segment event), the track stops in state 6
    with stopped reason 3, status flags bit0, write_pos at that segment's start and read returns nothing from there."""
    ep, hst, clock = bench()
    t = track(hst, analog)
    t.configure(rate=1000, mode=c.REPEAT, samples=8, segments=8)
    t.subscribe()
    t.start()
    clock.t = 20                                                    # two whole segments and a part
    done = t.segments()
    events(ep, t.fn)
    ep.capture_overflow(t.fn, error)
    st = t.status()
    assert (st.state, st.error, st.flags & DROPPED) == (ST["error"], error, DROPPED)
    assert t.segments() == done and st.write_pos == done[-1].position + t.config.segment_bytes(8)
    assert [(e.kind, e.reason, e.error) for e in events(ep, t.fn)] == [("stopped", 3, error)]
    r = hst.request(t.fn, t.READ, struct.pack("<IQH", t.generation, st.write_pos, 64), locked=False).payload
    assert struct.unpack_from("<QBH", r) == (st.write_pos, 0, 0)


def test_the_pretrigger_ring_is_not_public_while_waiting():
    """capture §2.2: while it waits for the trigger, write_pos is 0 and neither read nor notifications return the
    pretrigger ring."""
    ep, hst, clock = bench()
    lc = track(hst)
    ep.captures[lc.fn].value = lambda role, i: 0                    # the edge never comes
    lc.configure(rate=1000, mode=c.STREAMING, trigger=(c.EDGE, 0, 0), pretrigger=50)
    lc.subscribe()
    lc.start()
    clock.t = 100
    assert ep.pushes() == [] and lc.status().write_pos == 0 and lc.status().state == ST["waiting"]
    r = hst.request(lc.fn, lc.READ, struct.pack("<IQH", lc.generation, 0, 64), locked=False).payload
    assert struct.unpack_from("<QBH", r) == (0, 0, 0)


def test_unfinished_tail_bytes_are_not_returned_before_the_segment_ends():
    """capture §2.2: whole bytes may go out before the segment ends; with a w that is not a multiple of 8 the last byte
    a later sample still changes is not sent - every frame ends on a whole byte of whole samples."""
    ep, hst, clock = bench(with_inner(widths=(3,)))
    lc = track(hst, roles=(0, 1, 2))
    lc.configure(rate=1000, mode=c.STREAMING)
    lc.subscribe()
    lc.start()
    sent = 0
    for t in range(1, 30, 3):
        clock.t = t
        for f in ep.pushes():
            if f[0] == m.ROLE_DATA:
                _, _, position, data, _ = c.unpack_push(f)
                assert position == sent
                sent += len(data)
                assert sent % 3 == 0                                    # 3 bytes = 8 whole samples of 3 bits
        assert sent * 8 // 3 <= t + 1                                   # only samples already taken
    assert sent


def test_host_drops_the_data_past_write_pos_after_an_error():
    """capture §2.2 / §2.1 rule 2: after an error stop the host drops what it received at or past write_pos; segments
    already out are not taken back (the client's Received.drop_from)."""
    got = c.Received(data=bytearray(b"abcdefgh"), start=100)
    got.drop_from(105)
    assert bytes(got.data) == b"abcde" and got.dropped_after_error == 3
    got = c.Received(data=bytearray(b"abcdef"), start=0, gaps=[(2, 4)])     # bytes 0-1, then 6-9
    got.drop_from(8)
    assert bytes(got.data) == b"abcd"


def test_segment_records_are_a_small_ring_the_probe_sizes():
    """capture §2.2 / §3.2: the probe keeps segment records in a ring of its own size; segments pages them (common §1.3:
    from_serial included, nothing from serial_done, a serial not kept starts at the oldest kept)."""
    ep, hst, clock = bench(with_inner(ring=40))
    lc = track(hst)
    lc.configure(rate=1000, mode=c.REPEAT, samples=8, segments=40)
    lc.start()
    clock.t = 320
    page, more = lc.segments_page(0)
    assert more and 0 < len(page) <= (1024 - 5 - 2) // 37 and page[0].serial == 0
    assert [s.serial for s in lc.segments(0)] == list(range(40))
    lc.release(9)
    assert lc.segments(3)[0].serial == 10                           # not kept: from the oldest kept
    assert lc.segments_page(lc.status().serial_done) == ([], False)


# ---- §3.1 roles -----------------------------------------------------------------------------------------------------

def test_role_k_is_channel_k():
    """capture §3.1: role k is channel k - the logic pos array is in role order; the analog's order and every
    per-channel TLV carry the planned role numbers themselves, which need not be contiguous."""
    ep, hst, clock = bench()
    lc = c.LogicCapture(hst)
    core.plan_apply(hst, [(lc.fn, 2, 32), (lc.fn, 0, 30), (lc.fn, 1, 31)])
    assert lc.configure(rate=1000, samples=8).positions == [0, 1, 2]
    an = track(hst, analog=True, roles=(1, 3))
    cfg = an.configure(rate=1000, samples=8)
    assert sorted(cfg.order) == [1, 3] and set(cfg.scale_nv) == {1, 3} and set(cfg.frontend) == {1, 3}


# ---- §3.2 the operations --------------------------------------------------------------------------------------------

def running(analog=False, mode=c.REPEAT):
    """A track with a plan, configured (repeat, 8 samples, 4 segments) and started, two segments done."""
    ep, hst, clock = bench()
    t = track(hst, analog)
    t.configure(rate=1000, mode=mode, samples=8 if mode != c.STREAMING else None,
                segments=4 if mode == c.REPEAT else None)
    if mode == c.STREAMING:
        t.subscribe()
    t.start()
    clock.t = 17
    return ep, hst, clock, t


OPS = {  # name: (lock needed, a body)
    "configure": (True, _configure_body(rate=1000, samples=8)),
    "start": (True, b""), "stop": (True, b""), "force": (True, b""),
    "status": (False, b""), "read": (False, None), "segments": (False, struct.pack("<I", 0)),
    "release": (True, None), "query": (False, _configure_body(rate=1000, samples=8)),
    "calibration": (False, b""), "subscribe": (True, struct.pack("<HI", 0, 0)), "unsubscribe": (True, b""),
}


@pytest.mark.parametrize("analog", [False, True])
@pytest.mark.parametrize("name", list(OPS))
def test_operation_rows_numbers_locks_and_answers(analog, name):
    """capture §3.2's table: each op's number, whether it needs the lock (session 0 -> session_required) and its
    answer's form; calibration is analog's only (logic: unknown_operation); describe's ops lists them."""
    spec = ANALOG if analog else LOGIC
    if name == "calibration" and not analog:
        assert "calibration" not in LOGIC.op
        ep, hst, clock, t = running(analog)
        assert ask(hst, t.fn, 0x19, b"", locked=False).detail == m.UNKNOWN_OPERATION
        assert 0x19 not in core.ops(hst, t.fn)
        return
    number = {"configure": 0x10, "start": 0x11, "stop": 0x12, "force": 0x13, "status": 0x14, "read": 0x15,
              "segments": 0x16, "release": 0x17, "query": 0x18, "calibration": 0x19, "subscribe": 0x01,
              "unsubscribe": 0x02}[name]
    assert spec.op[name] == number
    ep, hst, clock, t = running(analog)
    assert number in core.ops(hst, t.fn)
    locked, body = OPS[name]
    if body is None:
        body = struct.pack("<IQH", t.generation, 0, 64) if name == "read" else struct.pack("<II", t.generation, 0)
    if name in ("start", "configure"):
        t.stop()
    if locked:
        r = ask(hst, t.fn, number, body, locked=False)
        assert r.resolution == m.REJECTED and r.detail == m.SESSION_REQUIRED
    r = ask(hst, t.fn, number, body, locked=locked)
    assert r.succeeded, r.describe()
    p = r.payload
    if name in ("stop", "force", "release", "subscribe", "unsubscribe"):
        assert p == b""
    elif name == "start":
        assert len(p) == 4 and struct.unpack("<I", p)[0] == t.generation + 1
    elif name == "status":
        state, done, pos, flags, generation = struct.unpack_from("<BIQBI", p)
        assert len(p) == 18 and generation == t.generation
    elif name == "read":
        position, flags, n = struct.unpack_from("<QBH", p)
        assert position == 0 and len(p) >= 11 + n and n > 0
    elif name == "segments":
        more, count = p[0], p[1]
        assert len(p) >= 2 + 37 * count and count == 2
    elif name in ("configure", "query"):
        assert {ANSWER["actual_rate"], ANSWER["layout"]} <= {tag for tag, _ in m.split_tlvs(p)}
    elif name == "calibration":
        assert m.split_tlvs(p)


@pytest.mark.parametrize("analog", [False, True])
def test_states_0_to_6_and_status_fields(analog):
    """capture §3.2: states 0 unconfigured, 1 configured, 2 waiting, 3 capturing, 4 done, 5 paused, 6 error; serial_done
    is the next serial to finish (segments finished mod 2^32) and write_pos the next byte's position, dropped bytes
    included."""
    assert LOGIC.enum["state"] == {"unconfigured": 0, "configured": 1, "waiting": 2, "capturing": 3, "done": 4,
                                   "paused": 5, "error": 6} == ANALOG.enum["state"]
    ep, hst, clock = bench()
    t = track(hst, analog)
    assert t.status().state == 0 and t.status().generation == 0
    t.configure(rate=1000, mode=c.REPEAT, samples=8, segments=2)
    assert t.status().state == 1
    t.start()
    clock.t = 9
    st = t.status()
    assert (st.state, st.serial_done, st.write_pos) == (3, 1, t.config.segment_bytes(8))
    clock.t = 30
    assert t.status().state == 5
    ep.capture_overflow(t.fn)
    assert t.status().state == 6
    t.configure(rate=1000, samples=8)
    t.start()
    clock.t = 60
    assert t.status().state == 4
    ep.captures[t.fn].value = lambda role, i: 0
    t.configure(rate=1000, samples=8, trigger=(c.CROSS_UP, 0, 1) if analog else (c.EDGE, 0, 0))
    t.start()
    assert t.status().state == 2


def test_write_pos_counts_dropped_streaming_bytes():
    """capture §3.2: write_pos includes what was dropped - it is the next byte's position."""
    ep, hst, clock = bench()
    lc = track(hst)
    lc.configure(rate=1000, mode=c.STREAMING)
    lc.subscribe()
    lc.start()
    lc.unsubscribe()
    clock.t = 40
    ep.pushes()
    assert lc.status().write_pos == 10                             # 40 samples x 2 bits, none sent


def test_status_flags_reset_at_start_and_error_tlv_in_state_6():
    """capture §3.2: status flags bit0 dropped, bit1 slipped (as segment flags bit2), other bits 0; reset at start, the
    run's total; state 6 answers TLV 0x01 error."""
    ep, hst, clock = bench()
    ep.capture_slipped = True
    lc = track(hst)
    lc.configure(rate=1000, mode=c.REPEAT, samples=8, segments=4)
    lc.start()
    clock.t = 9
    assert lc.status().flags == 2 and lc.segments()[0].flags & SLIPPED
    ep.capture_overflow(lc.fn, 3)
    st = lc.status()
    assert st.flags == 3 and st.error == 3
    raw = hst.request(lc.fn, lc.STATUS, locked=False).payload
    assert dict(m.split_tlvs(raw[18:])) == {LOGIC.tlv["status_answer"]["error"]: b"\x03"}
    ep.capture_slipped = False
    lc.configure(rate=1000, mode=c.REPEAT, samples=8, segments=4)
    lc.start()
    st = lc.status()
    assert st.flags == 0 and st.error is None and len(hst.request(lc.fn, lc.STATUS, locked=False).payload) == 18


@pytest.mark.parametrize("mode", [c.ONE_SHOT, c.REPEAT, c.STREAMING])
def test_generation_on_read_and_release_and_zero_before_start(mode):
    """capture §3.2: read and release carry the current generation, another is unavailable cause 6 (release checks it
    before its mode's no-op); before the first start the generation is 0."""
    ep, hst, clock = bench()
    t = track(hst)
    t.configure(rate=1000, mode=mode, samples=None if mode == c.STREAMING else 8)
    assert t.status().generation == 0
    assert hst.request(t.fn, t.READ, struct.pack("<IQH", 0, 0, 8), locked=False).succeeded
    if mode == c.STREAMING:
        t.subscribe()
    t.start()
    clock.t = 9
    for g in (0, 2, 0xFFFFFFFF):
        assert cause(ask(hst, t.fn, t.READ, struct.pack("<IQH", g, 0, 8), locked=False)) == 6
        assert cause(ask(hst, t.fn, t.RELEASE, struct.pack("<II", g, 0))) == 6
    assert ask(hst, t.fn, t.RELEASE, struct.pack("<II", 1, 0)).succeeded


def test_read_positions_gaps_and_max_0():
    """capture §3.2: a read position re-used goes to the next readable one with gap; none left -> write_pos, gap 1,
    len 0; at or past write_pos -> write_pos, gap 0, len 0; max 0 is an empty success by the same position rules."""
    ep, hst, clock = bench()
    lc = track(hst)
    lc.configure(rate=1000, mode=c.REPEAT, samples=8, segments=4)
    lc.start()
    clock.t = 24
    lc.release(0)
    rd = lambda pos, most: struct.unpack_from("<QBH", hst.request(lc.fn, lc.READ, struct.pack(   # noqa: E731
        "<IQH", lc.generation, pos, most), locked=False).payload)
    wp = lc.status().write_pos
    assert rd(0, 64) == (2, 0x02, wp - 2)                           # released: from the next kept, gap
    assert rd(0, 0)[0] == 2 and rd(0, 0)[1] & 0x02 and rd(0, 0)[2] == 0
    assert rd(wp, 8) == (wp, 0, 0) and rd(wp + 100, 8) == (wp, 0, 0) and rd(wp + 100, 0) == (wp, 0, 0)
    assert rd(2, 1) == (2, 0x01, 1)                                 # more
    lc.release(lc.status().serial_done - 1)
    assert rd(0, 8) == (wp, 0x02, 0)                                # nothing readable left: write_pos with gap


def test_release_frees_finished_segments_up_to_serial_inclusive():
    """capture §3.2: release is repeat's only (one-shot / streaming: success, nothing done); it frees the finished
    segments at or before serial (core §2.6) and never one not finished; from state 5 the capture goes on by itself
    (state 3) with flags bit0 on the next segment, and no stopped is sent for the full ring."""
    ep, hst, clock = bench()
    lc = track(hst)
    lc.configure(rate=1000, mode=c.REPEAT, samples=8, segments=2)
    lc.subscribe()
    lc.start()
    clock.t = 30
    assert lc.status().state == ST["paused"]
    assert [e.kind for e in events(ep, lc.fn)] == ["segment", "segment"]
    lc.release(5)                                                   # past serial_done: only the finished ones go
    assert lc.status().state == ST["capturing"] and lc.segments() == []
    clock.t = 31
    lc.release(0xFFFFFFFF)                                          # before serial 0 (core §2.6): nothing freed
    clock.t = 40
    (seg,) = lc.segments()[:1]
    assert seg.serial == 2 and seg.flags & GAP
    lc.stop()
    lc.configure(rate=1000, samples=8)
    lc.start()
    clock.t = 50
    lc.status()
    before = snap(ep, lc.fn)
    lc.release(0)
    assert snap(ep, lc.fn) == before


def test_read_returns_at_most_max_and_within_max_frame():
    """capture §3.2: read returns what the probe decides - no more than max, and an answer within max_frame."""
    ep, hst, clock = bench(virtual_bench.esp32_v003)
    lc = c.LogicCapture(hst)
    core.plan_apply(hst, [(lc.fn, 0, 4)])
    lc.configure(rate=1_000_000, samples=4000)
    lc.start()
    clock.t = 10
    lc.wait()
    for most in (1, 100, 0xFFFF):
        r = hst.request(lc.fn, lc.READ, struct.pack("<IQH", lc.generation, 0, most), locked=False)
        n = struct.unpack_from("<QBH", r.payload)[2]
        assert n <= most and 5 + len(r.payload) <= 512
    assert lc.read(0, 4000) == bytes(ep.captures[lc.fn].data[:4000])


# the state table (§3.2): row by row

def table(analog, state):
    """A track in `state` (0-6)."""
    ep, hst, clock = bench()
    t = track(hst, analog)
    if state == 0:
        return ep, hst, clock, t
    if state == 2:
        ep.captures[t.fn].value = lambda role, i: 0
        t.configure(rate=1000, samples=8, trigger=(c.CROSS_UP, 0, 1) if analog else (c.EDGE, 0, 0))
    else:
        t.configure(rate=1000, mode=c.REPEAT if state in (3, 5, 6) else c.ONE_SHOT, samples=8,
                    segments=(1 if state == 5 else 4) if state in (3, 5, 6) else None)
    if state >= 2:
        t.subscribe()
        t.start()
        clock.t = {3: 9, 4: 20, 5: 20, 6: 9}.get(state, 0)
    if state == 6:
        ep.capture_overflow(t.fn)
    assert t.status().state == state
    events(ep, t.fn)
    return ep, hst, clock, t


def body(analog):
    return _configure_body(rate=1000, samples=8)


@pytest.mark.parametrize("analog", [False, True])
@pytest.mark.parametrize("state, configure, start", [
    (0, 1, "cause6"), (1, 1, "new"), (2, "cause6", "cause6"), (3, "cause6", "cause6"), (4, 1, "new"),
    (5, "cause6", "cause6"), (6, 1, "new")])
def test_state_table_configure_and_start(analog, state, configure, start):
    """capture §3.2's state table, the configure and start columns: configure -> 1 in states 0, 1, 4, 6 and unavailable
    cause 6 in 2, 3, 5 (nothing changed); start -> 2 (a trigger) / 3 with a new generation and the segments gone in
    1, 4, 6, cause 6 elsewhere (and in 0: without a plan configure / query are cause 6 too)."""
    ep, hst, clock, t = table(analog, state)
    before = snap(ep, t.fn)
    r = ask(hst, t.fn, t.CONFIGURE, body(analog))
    if configure == 1:
        assert r.succeeded and t.status().state == 1
        ep, hst, clock, t = table(analog, state)
    else:
        assert cause(r) == 6 and snap(ep, t.fn) == before
    before, generation = snap(ep, t.fn), t.status().generation
    r = ask(hst, t.fn, t.START)
    if start == "new":
        assert r.succeeded and t.status().generation == generation + 1 and t.segments() == []
        assert t.status().state in (2, 3, 4)
    else:
        assert cause(r) == 6 and snap(ep, t.fn) == before
    if state == 0:
        ep2, hst2, _ = bench()
        t2 = c.AnalogCapture(hst2) if analog else c.LogicCapture(hst2)
        for op in (t2.CONFIGURE, t2.QUERY_OP):
            assert cause(ask(hst2, t2.fn, op, body(analog), locked=op == t2.CONFIGURE)) == 6


@pytest.mark.parametrize("analog", [False, True])
@pytest.mark.parametrize("state", range(7))
def test_state_table_stop_force_release(analog, state):
    """capture §3.2's state table, the stop, force and release columns: stop -> 1 from 2 (stopped 1), 3 (stopped 1,
    a short segment with flags bit1), 5 and 6; force -> 3 from 2 only; release gives room in 3 and from 5 goes on (3,
    flags bit0); "—" is a success that changes nothing."""
    ep, hst, clock, t = table(analog, state)
    before = snap(ep, t.fn)
    assert ask(hst, t.fn, t.FORCE).succeeded
    if state == 2:
        assert t.status().state == 3 and [e.kind for e in events(ep, t.fn)] == ["triggered"]
        ep, hst, clock, t = table(analog, state)
    else:
        assert snap(ep, t.fn) == before
    if t.status().generation:
        assert ask(hst, t.fn, t.RELEASE, struct.pack("<II", t.status().generation, 0)).succeeded
        if state == 5:
            assert t.status().state == 3
            clock.t = 40
            assert any(s.flags & GAP for s in t.segments())
        ep, hst, clock, t = table(analog, state)
    before = snap(ep, t.fn)
    clock.t += 3 if state == 3 else 0
    assert ask(hst, t.fn, t.STOP).succeeded
    got = events(ep, t.fn)
    if state in (2, 3, 5, 6):
        assert t.status().state == 1
        if state in (2, 3):
            assert got[-1].kind == "stopped" and got[-1].reason == 1
        if state == 3:
            assert t.segments()[-1].flags & SHORT
    else:
        assert t.status().state == state


@pytest.mark.parametrize("analog", [False, True])
def test_state_table_automatic_transitions(analog):
    """capture §3.2's state table, the last column: 2 -> 3 on the trigger (triggered), 2 -> 6 on an error (stopped 3),
    3 -> 4 on completion (stopped 0), 3 -> 5 for want of a free segment, 3 -> 6 on an error (stopped 3)."""
    ep, hst, clock = bench()
    t = track(hst, analog)
    t.subscribe()
    t.configure(rate=1000, samples=8, trigger=(c.CROSS_UP, 1, 2100) if analog else (c.EDGE, 0, 0))
    t.start()
    clock.t = 3
    assert [e.kind for e in events(ep, t.fn)][:1] == ["triggered"]
    clock.t = 30
    assert t.status().state == 4
    assert [(e.kind, e.reason) for e in events(ep, t.fn)] == [("segment", None), ("stopped", 0)]
    ep.captures[t.fn].value = lambda role, i: 0
    t.start()
    ep.capture_overflow(t.fn)
    assert t.status().state == 6 and [(e.kind, e.reason) for e in events(ep, t.fn)] == [("stopped", 3)]
    t.configure(rate=1000, mode=c.REPEAT, samples=8, segments=1)
    t.start()
    clock.t = 50
    assert t.status().state == 5 and [e.kind for e in events(ep, t.fn)] == ["segment"]
    t.release(0)
    ep.capture_overflow(t.fn)
    assert t.status().state == 6 and [(e.kind, e.reason) for e in events(ep, t.fn)] == [("stopped", 3)]


# ---- §3.3 configure -------------------------------------------------------------------------------------------------

T = LOGIC.tlv["configure"]


def tlvs(*items, critical=()):
    return b"".join(m.tlv(tag, value, critical=tag in critical) for tag, value in items)


def u32(v):
    return struct.pack("<I", v)


def mode(v):
    return (T["mode"], bytes([v]))


def rate_tlv(v=1000):
    return (T["rate"], u32(v))


def samples(v=8):
    return (T["samples"], u32(v))


def configured_with_data(analog=False):
    ep, hst, clock = bench()
    t = track(hst, analog)
    t.configure(rate=1000, samples=8)
    t.start()
    clock.t = 20
    assert t.status().state == ST["done"]
    return ep, hst, clock, t


CONTRACT = [
    ("no mode", [rate_tlv(), samples()], "malformed"),
    ("no rate", [mode(1), samples()], "malformed"),
    ("no samples in mode 1", [mode(1), rate_tlv()], "malformed"),
    ("no samples in mode 2", [mode(2), rate_tlv()], "malformed"),
    ("rate 0", [mode(1), rate_tlv(0), samples()], "malformed"),
    ("samples 0", [mode(1), rate_tlv(), samples(0)], "malformed"),
    ("segments 0", [mode(2), rate_tlv(), samples(), (T["segments"], u32(0))], "malformed"),
    ("mode 3 samples", [mode(3), rate_tlv(), samples()], T["samples"]),
    ("mode 3 segments", [mode(3), rate_tlv(), (T["segments"], u32(2))], T["segments"]),
    ("mode 1 segments", [mode(1), rate_tlv(), samples(), (T["segments"], u32(2))], T["segments"]),
    ("pretrigger without trigger", [mode(1), rate_tlv(), samples(), (T["pretrigger"], u32(0))], T["pretrigger"]),
    ("pretrigger with type 0", [mode(1), rate_tlv(), samples(), (T["trigger"], struct.pack("<BBI", 0, 0, 0)),
                                (T["pretrigger"], u32(1))], T["pretrigger"]),
    ("pretrigger not below samples", [mode(1), rate_tlv(), samples(), (T["trigger"], None), (T["pretrigger"], u32(8))],
     T["pretrigger"]),
    ("pretrigger past max_pretrigger", [mode(3), rate_tlv(), (T["trigger"], None), (T["pretrigger"], u32(1 << 24))],
     T["pretrigger"]),
    ("trigger role not planned", [mode(1), rate_tlv(), samples(), (T["trigger"], "unplanned")], "cause6"),
    ("mode undefined", [mode(9), rate_tlv(), samples()], T["mode"]),
    ("rate under the range", [mode(1), rate_tlv(1), samples()], T["rate"]),
]


@pytest.mark.parametrize("op", ["configure", "query"])
@pytest.mark.parametrize("analog", [False, True])
@pytest.mark.parametrize("case, items, want", CONTRACT, ids=[row[0].replace(" ", "-") for row in CONTRACT])
def test_configure_contract_refusals_change_nothing(op, analog, case, items, want):
    """capture §3.3's contract (configure and query alike): a required TLV missing, or a "1 or more" value 0, is
    malformed; a TLV the table says "only" / "never" for is unsupported with its tag as received whatever its value;
    a non-immediate trigger on a role not in the plan is unavailable cause 6; a pretrigger past max_pretrigger or not
    below samples is unsupported (pretrigger); a value it does not handle is unsupported (the tag). Nothing changes."""
    ep, hst, clock, t = configured_with_data(analog)
    trig = struct.pack("<BBI", c.CROSS_UP if analog else c.EDGE, 0, 1)
    fixed = []
    for tag, v in items:
        if tag == T["trigger"] and v is None:
            v = trig
        elif v == "unplanned":
            v = struct.pack("<BBI", c.CROSS_UP if analog else c.EDGE, 9, 1)
        fixed.append((tag, v))
    before = snap(ep, t.fn)
    r = ask(hst, t.fn, t.CONFIGURE if op == "configure" else t.QUERY_OP, tlvs(*fixed), locked=op == "configure")
    if want == "malformed":
        assert malformed(r), r.describe()
    elif want == "cause6":
        assert cause(r) == 6
    else:
        assert unsupported_tag(r) == want
    assert snap(ep, t.fn) == before


@pytest.mark.parametrize("analog", [False, True])
def test_type_0_trigger_role_and_value_are_not_looked_at(analog):
    """capture §3.3: a type 0 trigger's role and value are unused - the probe does not refuse a role not in the plan."""
    ep, hst, clock = bench()
    t = track(hst, analog)
    r = hst.request(t.fn, t.CONFIGURE, tlvs(mode(1), rate_tlv(), samples(),
                                            (T["trigger"], struct.pack("<BBI", 0, 99, 7))))
    assert r.succeeded
    t.subscribe()
    t.start()
    clock.t = 20
    assert [e.kind for e in events(ep, t.fn)] == ["segment", "stopped"]   # immediate: no triggered


@pytest.mark.parametrize("analog", [False, True])
def test_the_critical_bit_on_the_tables_tlvs_changes_nothing(analog):
    """capture §3.3: the table's TLVs go without the critical bit; with it set the probe treats them the same - the
    same answer, and a refusal names the tag as received (bit 7 kept)."""
    ep, hst, clock = bench()
    t = track(hst, analog)
    items = [mode(2), rate_tlv(1000), samples(8), (T["segments"], u32(2))]
    plain = hst.request(t.fn, t.QUERY_OP, tlvs(*items), locked=False).payload
    crit = hst.request(t.fn, t.QUERY_OP, tlvs(*items, critical={tag for tag, _ in items}), locked=False).payload
    assert plain == crit
    bad = [mode(3), rate_tlv(1000), samples(8)]
    r = ask(hst, t.fn, t.QUERY_OP, tlvs(*bad, critical={T["samples"]}), locked=False)
    assert unsupported_tag(r) == 0x80 | T["samples"]
    t.configure(rate=1000, samples=8)                               # the client sends them without the bit
    assert not any(tag & 0x80 for tag, _ in m.split_tlvs(ep.requests[-1].payload))


def test_values_it_cannot_handle_are_unsupported_with_their_tag():
    """capture §3.3 (refusals not listed): a value the probe cannot handle beyond what describe can say is unsupported
    with that TLV's tag as received - here a frontend number the analog does not have (critical or not); capacity and
    rate follow their own rules (rounded, not refused)."""
    ep, hst, clock = bench()
    an = track(hst, analog=True, roles=(0,))
    for critical in (False, True):
        r = ask(hst, an.fn, an.CONFIGURE, _configure_body(rate=1000, samples=8)
                + m.tlv(ANALOG.tlv["configure"]["frontend"], bytes([0, 9]), critical=critical))
        assert unsupported_tag(r) == ANALOG.tlv["configure"]["frontend"] | (0x80 if critical else 0)
    assert an.configure(rate=1000, samples=1 << 30).samples == 65536


def test_two_refusals_answer_one_of_them():
    """capture §3.3: when two refusals apply the probe answers with one of them (no order defined) - here samples
    missing in mode 1 and a frontend it does not have."""
    ep, hst, clock = bench()
    an = track(hst, analog=True, roles=(0,))
    r = ask(hst, an.fn, an.CONFIGURE, tlvs(mode(1), rate_tlv())
            + m.tlv(ANALOG.tlv["configure"]["frontend"], bytes([0, 9])))
    assert malformed(r) or unsupported_tag(r) == ANALOG.tlv["configure"]["frontend"]


def test_query_is_its_own_lock_free_op_that_changes_nothing():
    """capture §3.3: query (0x18) takes configure's TLVs without the lock and answers the actual values; it changes no
    setting or data (a configured track's data and state are as before); with no plan it is unavailable cause 6."""
    ep, hst, clock, t = configured_with_data()
    other = h.Host(lambda b: ep.handle(b, 0))                        # no session at all
    before = snap(ep, t.fn)
    r = other.request(t.fn, t.QUERY_OP, _configure_body(rate=3000, samples=5), locked=False)
    assert c._config(r.payload, False).samples == 5 and snap(ep, t.fn) == before
    r = ask(other, t.fn, t.CONFIGURE, _configure_body(rate=3000, samples=5), locked=False)
    assert r.detail == m.SESSION_REQUIRED


@pytest.mark.parametrize("analog", [False, True])
def test_trigger_types_per_interface(analog):
    """capture §3.3: trigger types 0 immediate, 1 level (0 / 1), 2 edge (0 rise / 1 fall / 2 both) for logic, 3 / 4
    crossing up / down (the value after o / b) for analog; a type not declared is unsupported (trigger)."""
    ep, hst, clock = bench()
    t = track(hst, analog)
    mine, others = ((3, 4), (1, 2, 5)) if analog else ((1, 2), (3, 4, 5))
    declared = struct.unpack_from("<I", dict(describe(hst, t.fn))[DESCRIBE["trigger"]])[0]
    assert declared & 1 and all(declared >> k & 1 for k in mine) and not any(declared >> k & 1 for k in others)
    def ask_type(kind):
        body = tlvs(mode(1), rate_tlv(), samples(), (T["trigger"], struct.pack("<BBI", kind, 0, 1)))
        return ask(hst, t.fn, t.QUERY_OP, body, locked=False)

    for kind in mine:
        assert ask_type(kind).succeeded
    for kind in others:
        assert unsupported_tag(ask_type(kind)) == T["trigger"]


@pytest.mark.parametrize("value, first", [(0, 2), (1, 4), (2, 2)])
def test_logic_edge_values(value, first):
    """capture §3.3: edge value 0 rising, 1 falling, 2 both - on bit 1 of the counter (rises at 2, falls at 4)."""
    ep, hst, clock = bench()
    lc = track(hst)
    lc.configure(rate=1000, samples=8, trigger=(c.EDGE, 1, value), pretrigger=4)
    lc.start()
    clock.t = 30
    (seg,) = lc.segments()
    assert seg.trigger_index == min(first, 4) and seg.start_ns == (first - min(first, 4)) * 1_000_000


def test_samples_count_the_pretrigger_and_an_early_trigger_shortens():
    """capture §3.3: samples is the segment's total, pretrigger included; a trigger too early for the whole pretrigger
    makes a short segment with a smaller trigger_index; an immediate start sends no triggered."""
    ep, hst, clock = bench()
    lc = track(hst, roles=(0, 1, 2, 3))
    lc.subscribe()
    lc.configure(rate=1000, samples=16, trigger=(c.LEVEL, 3, 1), pretrigger=4)   # bit 3 is 1 from sample 8
    lc.start()
    clock.t = 40
    (seg,) = lc.segments()
    assert (seg.samples, seg.trigger_index) == (16, 4)
    lc.configure(rate=1000, samples=16, trigger=(c.EDGE, 1, 0), pretrigger=4)    # bit 1 rises at sample 2
    lc.start()
    clock.t = 80
    (seg,) = lc.segments()
    assert (seg.samples, seg.trigger_index) == (14, 2) and seg.flags & SHORT
    events(ep, lc.fn)
    lc.configure(rate=1000, samples=16)
    lc.start()
    clock.t = 120
    assert "triggered" not in [e.kind for e in events(ep, lc.fn)]


@pytest.mark.parametrize("asked, got", [(1000, 1000), (3_000_000, Fraction(20_000_000, 7)), (19_999_999, 20_000_000),
                                        (999, None), (20_000_001, None)])
def test_rate_is_the_nearest_kept_within_the_range(asked, got):
    """capture §3.3: the actual rate is the nearest the probe keeps within rate_range (actual_rate num/den); a rate
    outside rate_range is unsupported (rate)."""
    ep, hst, clock = bench()
    lc = track(hst)
    if got is None:
        r = ask(hst, lc.fn, lc.QUERY_OP, tlvs(mode(1), rate_tlv(asked), samples()), locked=False)
        assert unsupported_tag(r) == T["rate"]
    else:
        assert lc.configure(rate=asked, samples=8, query=True).rate == got


def test_analog_rate_is_per_channel_and_rounds_within_the_shared_budget():
    """capture §3.3 rate: analog's rate is per channel; when the role count lowers the top, the nearest kept value of
    that set is used (actual_rate), not a refusal."""
    ep, hst, clock = bench()
    one = track(hst, analog=True, roles=(0,))
    top1 = one.configure(rate=83_333, samples=8).rate
    core.plan_apply(hst, [(one.fn, 0, 16), (one.fn, 1, 17), (one.fn, 2, 18), (one.fn, 3, 19)])
    top4 = one.configure(rate=83_333, samples=8).rate
    assert top4 < top1 and top4 * 4 <= 83_333


def test_samples_and_segments_are_rounded_down_to_what_it_holds():
    """capture §3.3: samples past what the probe holds are rounded down to it and actual_samples says so; mode 2's
    segments are rounded down too and, left out, the probe picks them (actual_segments 1 or more); rounding alone is
    no refusal."""
    ep, hst, clock = bench(virtual_bench.esp32_v003)
    lc = c.LogicCapture(hst)
    core.plan_apply(hst, [(lc.fn, 0, 4)])
    assert lc.configure(rate=1_000_000, samples=100_000).samples == 65_536
    ep, hst, clock = bench()
    lc = track(hst)
    cfg = lc.configure(rate=1000, mode=c.REPEAT, samples=8, segments=1000)
    assert cfg.segments == 8
    r = hst.request(lc.fn, lc.QUERY_OP, tlvs(mode(2), rate_tlv(), samples()), locked=False)
    assert c._config(r.payload, False).segments >= 1


@pytest.mark.parametrize("analog", [False, True])
@pytest.mark.parametrize("which", [c.ONE_SHOT, c.REPEAT, c.STREAMING])
def test_configure_answer_rows(analog, which):
    """capture §3.3's answer: actual_rate (num, den >= 1) and layout always; actual_samples in modes 1 / 2,
    actual_segments in mode 2; analog: scale (role, zero i32, scale_nv i32) per planned role, skew (role, u32) where
    not 0, frontend_used (role, frontend) and reference (source, mv, how) as diagnostics."""
    ep, hst, clock = bench()
    t = track(hst, analog, roles=(0, 2) if analog else (0, 1))
    r = hst.request(t.fn, t.CONFIGURE, _configure_body(mode=which, rate=1000, samples=8))
    rows = m.split_tlvs(r.payload)
    got = {}
    for tag, v in rows:
        got.setdefault(tag, []).append(v)
    num, den = struct.unpack("<II", got[ANSWER["actual_rate"]][0])
    assert num >= 1 and den >= 1
    layout = got[ANSWER["layout"]][0]
    if analog:
        assert layout[0] in (8, 16, 32) and layout[3] == 2 and sorted(layout[4:6]) == [0, 2] and len(layout) == 6
    else:
        assert layout[1] == 2 and list(layout[2:4]) == [0, 1] and len(layout) == 4
    assert (ANSWER["actual_samples"] in got) == (which != c.STREAMING)
    assert (ANSWER["actual_segments"] in got) == (which == c.REPEAT)
    if ANSWER["actual_samples"] in got:
        assert struct.unpack("<I", got[ANSWER["actual_samples"]][0])[0] == 8
    if analog:
        scales = [struct.unpack("<Bii", v) for v in got[ANSWER["scale"]]]
        assert sorted(s[0] for s in scales) == [0, 2]
        skews = [struct.unpack("<BI", v) for v in got.get(ANSWER["skew"], [])]
        assert all(ns for _, ns in skews) and {k for k, _ in skews} <= {0, 2}
        assert sorted(v[0] for v in got[ANSWER["frontend_used"]]) == [0, 2]
        source, mv, how = struct.unpack("<BIB", got[ANSWER["reference"]][0])
        assert source in ANALOG.enum["reference_source"].values() and how in ANALOG.enum["reference_how"].values()
    else:
        assert not {ANSWER["scale"], ANSWER["skew"], ANSWER["frontend_used"], ANSWER["reference"]} & set(got)


def test_configure_clears_the_data_and_keeps_the_generation():
    """capture §3.3: a configure success clears the data and segments and goes to state 1; the generation stays (the
    next start moves it on)."""
    ep, hst, clock, t = configured_with_data()
    t.configure(rate=1000, samples=8)
    st = t.status()
    assert (st.state, st.write_pos, st.generation) == (1, 0, 1) and t.segments() == []


# ---- §3.4 notifications ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("analog", [False, True])
def test_segment_event_carries_the_record_and_not_in_streaming(analog):
    """capture §3.4: a segment event (kind 0x01) when a segment ends - one-shot's completion too - with its record (§2);
    none in streaming."""
    ep, hst, clock = bench()
    t = track(hst, analog)
    t.subscribe()
    t.configure(rate=1000, mode=c.REPEAT, samples=8, segments=4)
    t.start()
    clock.t = 17
    got = events(ep, t.fn)
    assert [e.kind for e in got] == ["segment", "segment"] and [e.segment for e in got] == t.segments()
    t.stop()
    t.configure(rate=1000, mode=c.STREAMING)
    events(ep, t.fn)
    t.start()
    clock.t = 60
    assert [e.kind for e in events(ep, t.fn)] == []


@pytest.mark.parametrize("analog", [False, True])
def test_stopped_event_reasons_error_and_generation(analog):
    """capture §3.4: stopped (kind 0x02) reason 0 complete, 1 the host's stop, 3 an error (always on entering state 6,
    with status's error), error(u8) and the generation it was made in."""
    ep, hst, clock = bench()
    t = track(hst, analog)
    t.subscribe()
    t.configure(rate=1000, samples=8)
    t.start()
    clock.t = 20
    assert [(e.reason, e.error, e.generation) for e in events(ep, t.fn) if e.kind == "stopped"] == [(0, 0, 1)]
    t.configure(rate=1000, mode=c.REPEAT, samples=8, segments=4)
    t.start()
    t.stop()
    assert [(e.reason, e.generation) for e in events(ep, t.fn) if e.kind == "stopped"] == [(1, 2)]
    t.start()
    ep.capture_overflow(t.fn, 1)
    assert [(e.reason, e.error, e.generation) for e in events(ep, t.fn)] == [(3, t.status().error, 3)]


@pytest.mark.parametrize("analog", [False, True])
def test_triggered_event_serial_index_time_generation(analog):
    """capture §3.4: triggered (kind 0x03) serial, trigger_index, trigger_ns (the probe's clock) and generation."""
    ep, hst, clock = bench()
    t = track(hst, analog)
    t.subscribe()
    t.configure(rate=1000, samples=16, trigger=(c.CROSS_UP, 1, 2100) if analog else (c.EDGE, 1, 0), pretrigger=4)
    clock.t = 5
    t.start()
    clock.t = 40
    trig, seg, stopped = events(ep, t.fn)
    assert trig.kind == "triggered" and (trig.serial, trig.generation) == (0, 1)
    assert trig.trigger_index == seg.segment.trigger_index
    first = 1 if analog else 2
    assert trig.trigger_ns == ep.captures[t.fn].time_of(first) + (t.config.skew_ns.get(1, 0) if analog else 0)


def test_streaming_data_frames_carry_generation_and_segment_info():
    """capture §3.4: data (role 0x04) only while streaming, core §10.2's form at read's byte positions, every frame with
    TLV 0x01 generation and 0x02 segment_info (24 bytes)."""
    ep, hst, clock = bench()
    lc = track(hst)
    lc.subscribe()
    lc.configure(rate=1000, mode=c.REPEAT, samples=8, segments=4)
    lc.start()
    clock.t = 20
    assert not [f for f in ep.pushes() if f[0] == m.ROLE_DATA]
    lc.stop()
    lc.configure(rate=1000, mode=c.STREAMING)
    lc.start()
    clock.t = 60
    frames = [f for f in ep.pushes() if f[0] == m.ROLE_DATA]
    assert frames
    for f in frames:
        n = struct.unpack_from("<H", f, 13)[0]
        tail = dict(m.split_tlvs(f[15 + n:]))
        assert struct.unpack("<I", tail[LOGIC.tlv["data"]["generation"]])[0] == lc.generation
        assert len(tail[LOGIC.tlv["data"]["segment_info"]]) == 24


def test_host_takes_events_of_another_generation_as_old():
    """capture §3.4: every event carries its generation; one from before the last start (it may come after start's
    answer) is the old capture's - the client drops it (stale_events) and streaming drops old data frames."""
    from types import SimpleNamespace
    ep, hst, clock = bench()
    lc = track(hst)
    lc.subscribe()
    lc.configure(rate=1000, samples=8)
    lc.start()
    clock.t = 20
    lc.status()
    old = list(ep.outbox)
    ep.outbox.clear()
    lc.start()
    clock.t = 40
    lc.status()
    link = SimpleNamespace(events=old + list(ep.outbox))
    ep.outbox.clear()
    got = lc.events(link)
    assert {e.generation for e in got} == {2} and lc.stale_events == 2


def test_streaming_needs_a_subscription_and_declares_subscribe():
    """capture §3.4 / §3.2: a fn declaring streaming has subscribe and unsubscribe in its ops; a mode 3 start without
    the fn's subscription is unavailable cause 6; the subscription gone while it captures, it goes on."""
    ep, hst, clock = bench()
    lc = track(hst)
    modes = [v[0] for tag, v in describe(hst, lc.fn) if tag == DESCRIBE["mode"]]
    assert c.STREAMING in modes and {0x01, 0x02} <= core.ops(hst, lc.fn)
    lc.configure(rate=1000, mode=c.STREAMING)
    before = snap(ep, lc.fn)
    assert cause(ask(hst, lc.fn, lc.START)) == 6 and snap(ep, lc.fn) == before
    lc.subscribe()
    lc.start()
    lc.unsubscribe()
    clock.t = 10
    assert lc.status().state == ST["capturing"]


# ---- §3.5 describe --------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["oep.fixture.logic", "oep.fixture.analog"])
def test_describe_declarations(name):
    """capture §3.5: ops (the optional query / force / subscribe / unsubscribe only when held), no features bit (left
    out at 0), one mode row per mode (mode, max_samples, max_segments - 0 outside mode 2), rate_range (1 <= min <=
    max), channels (u8), trigger (types u32 with type 0, max_pretrigger u32), analog frontends; plan role_channels."""
    ep, hst, clock = bench()
    fn = ep.fns[name]
    d = describe(hst, fn)
    tags = [t & 0x7F for t, _ in d]
    ops = core.ops(hst, fn)
    assert m.TAG_OPS in tags and {0x10, 0x11, 0x12, 0x14, 0x15, 0x16} <= ops
    for op in (0x13, 0x17, 0x18, 0x19, 0x01, 0x02):
        r = ask(hst, fn, op, b"", locked=False)
        assert (r.detail != m.UNKNOWN_OPERATION) == (op in ops), op
    feats = [v for t, v in d if t & 0x7F == 0x05]
    assert all(int.from_bytes(v, "little") == 0 for v in feats)
    rows = [struct.unpack("<BII", v) for t, v in d if t == DESCRIBE["mode"]]
    assert rows and len({r[0] for r in rows}) == len(rows)
    assert all((r[2] > 0) == (r[0] == c.REPEAT) for r in rows)
    lo, hi = struct.unpack("<II", dict(d)[DESCRIBE["rate_range"]])
    assert 1 <= lo <= hi
    assert len(dict(d)[DESCRIBE["channels"]]) == 1
    types, max_pre = struct.unpack("<II", dict(d)[DESCRIBE["trigger"]])
    assert types & 1 and max_pre >= 0
    fronts = [struct.unpack("<Bii", v) for t, v in d
              if name.endswith("analog") and t == ANALOG.tlv["describe"]["frontend"]]
    assert bool(fronts) == name.endswith("analog") and all(a <= b for _, a, b in fronts)
    assert any(t & 0x7F in (catalog_role_channels(), catalog_channel_group()) for t, _ in d)


def catalog_role_channels():
    return reg.DESCRIBE_COMMON["role_channels"]


def catalog_channel_group():
    return reg.DESCRIBE_COMMON["channel_group"]


def test_describe_answers_the_maximum_not_the_free_room():
    """capture §3.5: a mode row's max_samples / max_segments are the most it holds, not what is free now - the same
    while the ring is full."""
    ep, hst, clock = bench()
    lc = track(hst)
    before = describe(hst, lc.fn)
    lc.configure(rate=1000, mode=c.REPEAT, samples=8, segments=2)
    lc.start()
    clock.t = 30
    assert lc.status().state == ST["paused"] and describe(hst, lc.fn) == before


def test_a_probe_declares_only_the_modes_it_has():
    """capture §0 / §3.5: modes are declared one row each, only those offered; configure / query of another is
    unsupported (mode)."""
    ep, hst, clock = bench(virtual_bench.esp32_v003)
    lc = c.LogicCapture(hst)
    core.plan_apply(hst, [(lc.fn, 0, 4)])
    assert [v[0] for t, v in describe(hst, lc.fn) if t == DESCRIBE["mode"]] == [c.ONE_SHOT]
    for which in (c.REPEAT, c.STREAMING):
        for op in (lc.CONFIGURE, lc.QUERY_OP):
            r = ask(hst, lc.fn, op, _configure_body(mode=which, rate=1_000_000, samples=8), locked=op == lc.CONFIGURE)
            assert unsupported_tag(r) == T["mode"]


def test_the_configure_answer_is_what_holds_not_the_declaration():
    """capture §3.5: the declaration is a guide and configure's answer is right - a rate inside rate_range comes back
    as the nearest the probe keeps for this configuration."""
    ep, hst, clock = bench()
    an = track(hst, analog=True, roles=(0, 1, 2, 3))
    lo, hi = struct.unpack("<II", dict(describe(hst, an.fn))[DESCRIBE["rate_range"]])
    got = an.configure(rate=hi, samples=8, query=True).rate
    assert got != hi and got <= Fraction(hi, 4)


# ---- §3.7 calibration -----------------------------------------------------------------------------------------------

def test_calibration_returns_raw_factory_rows_and_vrefint_after_a_start():
    """capture §3.7: factory (frontend, scheme_len, scheme in reverse DNS of a-z 0-9 - ., raw_len u16, raw) per
    frontend, read as it is; vrefint (raw u32, ns u64, nominal_mv u32) measured after the last start - not returned
    before any; calibration changes nothing (lock-free, the data the same)."""
    import re
    ep, hst, clock = bench()
    an = track(hst, analog=True, roles=(0,))
    cal = ANALOG.tlv["calibration_answer"]
    rows = m.split_tlvs(hst.request(an.fn, an.CALIBRATION, locked=False).payload)
    assert cal["vrefint"] not in {t for t, _ in rows}
    frontends = []
    for tag, v in rows:
        rd = m.Reader(v)
        fe = rd.u8()
        scheme = rd.counted("B")
        raw = rd.counted("H")
        assert re.fullmatch(rb"[a-z0-9.-]{1,64}", scheme) and b"." in scheme and raw
        frontends.append(fe)
    assert sorted(frontends) == [0, 1, 2, 3]
    an.configure(rate=1000, samples=8)
    clock.t = 7
    an.start()
    clock.t = 20
    an.status()
    before = snap(ep, an.fn)
    got = an.calibration()
    assert got.vrefint is not None and got.vrefint[1] == 7_000_000 and got.vrefint_nominal_mv == 1100
    assert snap(ep, an.fn) == before


def test_chip_firmware_frontends_and_reference_are_where_the_rules_say():
    """capture §3.7: the chip and firmware are core describe's chip / firmware; each frontend's range is describe's
    frontend, the frontend each channel took is configure's frontend_used, the reference voltage is reference."""
    ep, hst, clock = bench()
    core_d = {t & 0x7F for t, _ in describe(hst, 0)}
    assert {reg.CORE.tlv["describe"]["chip"], reg.CORE.tlv["describe"]["firmware"]} <= core_d
    an = track(hst, analog=True, roles=(0,))
    cfg = an.configure(rate=1000, samples=8, frontends={0: 1})
    fronts = {v[0]: struct.unpack("<ii", v[1:]) for t, v in describe(hst, an.fn)
              if t == ANALOG.tlv["describe"]["frontend"]}
    assert cfg.frontend == {0: 1} and fronts[1] == (0, 1250) and cfg.reference == ("internal", 1100, False)


# ---- §4 oep.fixture.capture-group -----------------------------------------------------------------------------------

def grouped(mode=c.ONE_SHOT, samples=8):
    ep, hst, clock = bench()
    lc, an = track(hst), track(hst, analog=True, roles=(0,))
    grp = c.CaptureGroup(hst)
    for t in (lc, an):
        t.configure(rate=1000, mode=mode, samples=samples if mode != c.STREAMING else None,
                    segments=4 if mode == c.REPEAT else None)
    return ep, hst, clock, lc, an, grp


@pytest.mark.parametrize("name, number, locked", [("bind", 0x10, True), ("start", 0x11, True), ("stop", 0x12, True),
                                                  ("status", 0x14, False), ("subscribe", 0x01, True),
                                                  ("unsubscribe", 0x02, True)])
def test_group_operation_rows(name, number, locked):
    """capture §4.1's table: op numbers, the lock, and the forms - bind n x fn -> nothing; start -> start_ns(u64)
    generation(u32) n(u8) n x (fn u16, generation u32); stop -> nothing; status -> state(u8) start_ns(u64)
    generation(u32); subscribe / unsubscribe as core §10.3."""
    assert GROUP.op[name] == number
    ep, hst, clock, lc, an, grp = grouped()
    assert number in core.ops(hst, grp.fn)
    body = {"bind": struct.pack("<BHH", 2, lc.fn, an.fn), "subscribe": struct.pack("<HI", 0, 0)}.get(name, b"")
    if name in ("start", "stop"):
        grp.bind([lc, an])
    if locked:
        assert ask(hst, grp.fn, number, body, locked=False).detail == m.SESSION_REQUIRED
    r = ask(hst, grp.fn, number, body, locked=locked)
    assert r.succeeded, r.describe()
    if name == "start":
        start_ns, generation, n = struct.unpack_from("<QIB", r.payload)
        assert len(r.payload) == 13 + 6 * n == 25 and generation == 1
        assert [struct.unpack_from("<HI", r.payload, 13 + 6 * i) for i in range(n)] == [(lc.fn, 1), (an.fn, 1)]
    elif name == "status":
        assert len(r.payload) == 13 and struct.unpack("<BQI", r.payload) == (0, 0xFFFFFFFFFFFFFFFF, 0)
    else:
        assert r.payload == b""


def test_group_bind_rules():
    """capture §4.1 bind: configured logic / analog tracks in one mode with immediate triggers, each in state 1/4/6; the
    same list again changes nothing; a different list while the group captures is cause 6; the same fn twice malformed;
    an fn not in tracks unsupported (0x00 + fn); a track waiting / capturing alone cause 6; every refusal before any
    change."""
    ep, hst, clock, lc, an, grp = grouped()
    grp.bind([lc, an])
    grp.bind([lc, an])
    assert grp.status().state == ST["configured"]
    grp.start([lc, an])
    assert cause(ask(hst, grp.fn, grp.BIND, struct.pack("<BH", 1, lc.fn))) == 6
    assert ask(hst, grp.fn, grp.BIND, struct.pack("<BHH", 2, lc.fn, an.fn)).succeeded   # the same list: kept
    clock.t = 30
    grp.bind([])
    before = (snap(ep, lc.fn), snap(ep, an.fn))
    assert malformed(ask(hst, grp.fn, grp.BIND, struct.pack("<BHH", 2, lc.fn, lc.fn)))
    r = ask(hst, grp.fn, grp.BIND, struct.pack("<BH", 1, ep.fns["oep.fixture.gpio"]))
    assert unsupported_tag(r) == 0
    lc.configure(rate=1000, mode=c.REPEAT, samples=8, segments=4)
    lc.start()
    assert cause(ask(hst, grp.fn, grp.BIND, struct.pack("<BHH", 2, lc.fn, an.fn))) == 6   # capturing alone
    assert grp.status().state == 0 and ep.captures[an.fn].group is None


def test_group_unbind():
    """capture §4.1: n = 0 unbinds; while the group captures (state 3) it is cause 6; with nothing bound it is a no-op.
    A bound track's configure / start / stop / force and its plan change are cause 4; query, read, status, segments
    and release follow the track's own rules."""
    ep, hst, clock, lc, an, grp = grouped(mode=c.REPEAT)
    assert ask(hst, grp.fn, grp.BIND, b"\x00").succeeded
    grp.bind([lc, an])
    for op in (lc.CONFIGURE, lc.START, lc.STOP, lc.FORCE):
        assert cause(ask(hst, lc.fn, op, _configure_body(rate=1000, samples=8) if op == lc.CONFIGURE else b"")) == 4
    plan = ep.fns["oep.probe.plan"]
    assert cause(ask(hst, plan, reg.PROBE_PLAN.op["plan_release"], struct.pack("<BH", 1, lc.fn))) == 4
    grp.start([lc, an])
    assert cause(ask(hst, grp.fn, grp.BIND, b"\x00")) == 6
    clock.t = 9
    assert hst.request(lc.fn, lc.QUERY_OP, _configure_body(rate=1000, samples=8), locked=False).succeeded
    assert lc.status().state == ST["capturing"] and lc.segments() and lc.read(0, 2)
    lc.release(0)
    grp.stop()
    grp.bind([])
    assert ep.captures[lc.fn].group is None and grp.status().state == 0


def test_group_bind_checks_the_start_answer_fits_the_frame():
    """capture §4.1: bind also checks the start answer (18 + 6 x n bytes) fits the route's frame - unsupported (0x00)
    when it does not, before any change."""
    caps = {7: vbc.VirtualCapture({1}, {8}, 1, 1000, frontends={}), 11: vbc.VirtualCapture({1}, {8}, 1, 1000)}
    for cap in caps.values():
        cap.state, cap.mode = ST["configured"], c.ONE_SHOT
    g = vbc.VirtualGroup([7, 11], 2, [])
    with pytest.raises(vbc.Reject) as e:
        g.bind(caps, [7, 11], budget=29)
    assert (e.value.reason, e.value.payload[:1]) == (m.UNSUPPORTED, b"\x00") and g.tracks == []
    g.bind(caps, [7, 11], budget=30)
    assert g.tracks == [7, 11]


def test_group_start_only_from_1_4_6_and_atomic():
    """capture §4.1 start: bound, every track in state 1/4/6, else unavailable cause 6 before anything changes (the
    generations, states and data kept); then a new group generation and every track's, the old data gone; the answer in
    bind order; start_ns all ones before the first start; unbound start is cause 6."""
    ep, hst, clock, lc, an, grp = grouped()
    assert cause(ask(hst, grp.fn, grp.START)) == 6
    assert grp.status().start_ns is None
    grp.bind([an, lc])
    clock.t = 4
    start_ns = grp.start([lc, an])
    assert start_ns == 4_000_000 and grp.generation == 1 and list(grp.generations) == [an.fn, lc.fn]
    before = (snap(ep, lc.fn), snap(ep, an.fn), grp.status())
    assert cause(ask(hst, grp.fn, grp.START)) == 6                  # capturing
    assert (snap(ep, lc.fn), snap(ep, an.fn), grp.status()) == before
    clock.t = 30
    assert grp.status().state == ST["done"]
    grp.start([lc, an])
    assert grp.generation == 2 and lc.generation == an.generation == 2 and lc.segments() == []


def test_group_error_stops_the_others_and_a_failed_start():
    """capture §4.1: a track's error (state 6) makes the group 6 and stops the others as stop does (finished tracks keep
    their data); a start whose initialisation fails is completed failed with the same fixed form, the unstarted tracks
    in state 1, the failed one 6, no generation taken back."""
    ep, hst, clock, lc, an, grp = grouped(mode=c.REPEAT)
    hst.subscribe(grp.fn)
    grp.bind([lc, an])
    grp.start([lc, an])
    clock.t = 9
    ep.capture_overflow(an.fn, 2)
    assert grp.status().state == ST["error"] and lc.status().state == ST["configured"] and lc.segments()
    assert [(e.kind, e.reason, e.error, e.generation) for e in events(ep, grp.fn)] == [("stopped", 3, 2, 1)]
    ep.capture_fail_start = {lc.fn}
    grp.bind([])
    grp.bind([an, lc])
    r = hst.request(grp.fn, grp.START, b"")
    assert (r.resolution, r.detail) == (m.COMPLETED, m.FAILED)
    start_ns, generation, n = struct.unpack_from("<QIB", r.payload)
    assert generation == 2 and n == 2 and len(r.payload) == 25
    assert lc.status().state == ST["error"] and an.status().state == ST["configured"]
    assert lc.status().generation == an.status().generation == 2


def test_group_stop_and_status_state():
    """capture §4.1: stop applies to every track (short segments per track); unbound stop is a no-op; status state is 0
    unbound, 6 if any is 6, 4 once started and all are 4, 3 if any is 3 / 5, else 1; the group generation survives a
    bind change."""
    ep, hst, clock, lc, an, grp = grouped(mode=c.REPEAT)
    assert ask(hst, grp.fn, grp.STOP).succeeded and grp.status().state == 0
    grp.bind([lc, an])
    assert grp.status().state == 1
    grp.start([lc, an])
    clock.t = 3
    assert grp.status().state == 3
    grp.stop()
    assert lc.status().state == an.status().state == 1
    assert lc.segments()[-1].flags & SHORT and an.segments()[-1].flags & SHORT
    clock.t = 10
    grp.bind([])
    lc.configure(rate=1000, samples=8)
    an.configure(rate=1000, samples=8)
    grp.bind([lc, an])
    assert grp.status().generation == 1
    grp.start([lc, an])
    clock.t = 30
    assert grp.status().state == 4 and grp.status().generation == 2


def test_group_notifications_are_stopped_only():
    """capture §4.2: the group's subscription gets stopped only (reason 0 every track done, 1 stop, 3 error; error 0
    unless 3; the group generation); no triggered from an immediate group or its tracks; each track's events go to that
    track's subscription."""
    ep, hst, clock, lc, an, grp = grouped()
    hst.subscribe(grp.fn)
    grp.bind([lc, an])
    grp.start([lc, an])
    clock.t = 30
    assert [(e.kind, e.reason, e.error, e.generation) for e in events(ep, grp.fn)] == [("stopped", 0, 0, 1)]
    assert events(ep, lc.fn) == []                                   # the track's own: not subscribed
    lc.subscribe()
    grp.start([lc, an])
    grp.stop()
    assert [(e.kind, e.reason, e.generation) for e in events(ep, grp.fn)] == [("stopped", 1, 2)]
    assert "triggered" not in [e.kind for e in events(ep, lc.fn)]


def test_group_describe_tracks():
    """capture §4.3: describe has ops and tracks (n >= 1, n x fn); no features bit."""
    ep, hst, clock = bench()
    grp = c.CaptureGroup(hst)
    d = dict(describe(hst, grp.fn))
    v = d[GROUP.tlv["describe"]["tracks"]]
    assert v[0] >= 1 and list(struct.unpack_from(f"<{v[0]}H", v, 1)) == [ep.fns["oep.fixture.logic"],
                                                                          ep.fns["oep.fixture.analog"]]
    assert int.from_bytes(d.get(0x05, b"\0"), "little") == 0
