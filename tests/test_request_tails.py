"""The request-tail rules (oep-core §2.2, §2.3) on every op the virtual bench serves.

- An unknown TLV (a tag this probe does not implement in that (fn, op) context; 0x00 and 0x7F are never TLV tags) is
  ignored silently when it is not critical - the answer is the one the request gets without it - and refused rejected
  unsupported with the tag as received when it is critical.
- A TLV the probe implements is checked the same with or without bit 7: a value of another length (longer too) or a
  value the definition excludes -> malformed; a value the definition leaves unused or this probe does not handle ->
  unsupported with the tag as received.
- A tag the definition does not repeat, sent twice: the first is used.

The requests come from the client classes driving each interface, so the answers are also read back through the
client with the extra TLV in the request."""

import copy
import dataclasses
import struct

import pytest

from oep_client import (capture as c, console, settings, core, endpoint, virtual_bench, fixture, host as h, message as m,
                        registry as reg, riscv)

UNKNOWN = 0x3D                                 # no context defines it
CORE_NAME = "(core)"                           # fn 0: the core has no name (core §0); a label for these tests
# every request reads its tail by the general rule; the settings' set / unset carry their values before it (core §13.5)
NO_TAIL: set = set()


class Clock:
    t = 0

    def __call__(self):
        return self.t


def raw_tlv(tag: int, value: bytes) -> bytes:
    """A TLV with any tag byte (m.tlv refuses 0x00 / 0x7F, which a request may still carry)."""
    return struct.pack("<BH", tag, len(value)) + value


def answer(ep: endpoint.Endpoint, req: m.Request, payload: bytes, transport: int) -> m.Result:
    """`req` with another payload, on a copy of `ep` (the probe as it was before `req`)."""
    return m.Result.unpack(copy.deepcopy(ep).handle(dataclasses.replace(req, payload=payload).pack(), transport))


class Tagging:
    """A transport that appends an unknown TLV to every request. The answer must be the one the request gets without
    it (core §2.3: ignored, nothing in the answer names it); the same request with the TLV critical, sent to a copy of
    the probe as it was, must be rejected unsupported with the tag as received. `seen` collects the (interface name,
    op) pairs checked."""

    def __init__(self, ep: endpoint.Endpoint, transport: int = 1, tag: int = UNKNOWN):
        self.ep, self.transport, self.tag, self.seen = ep, transport, tag, set()

    def name(self, fn: int) -> str:
        return CORE_NAME if fn == m.CORE_FN else self.ep.names.get(fn, "")

    def __call__(self, data: bytes) -> bytes | None:
        req = m.Request.unpack(data)
        key = (self.name(req.fn), req.op)
        if key in NO_TAIL or (key == (CORE_NAME, m.OP_CONFIRM) and self.ep.revision == 0):
            return self.ep.handle(data, self.transport)
        plain = answer(self.ep, req, req.payload, self.transport)
        crit = answer(self.ep, req, req.payload + raw_tlv(self.tag | m.TAG_CRITICAL, b"\x04io.x"), self.transport)
        out = self.ep.handle(dataclasses.replace(req, payload=req.payload + raw_tlv(self.tag, b"\x04io.x")).pack(),
                             self.transport)
        res = m.Result.unpack(out)
        assert res == plain, (key, res, plain)                     # ignored silently
        if plain.resolution == m.COMPLETED:
            assert (crit.resolution, crit.detail, crit.payload) == \
                (m.REJECTED, m.UNSUPPORTED, bytes([self.tag | m.TAG_CRITICAL])), (key, crit)
            self.seen.add(key)
        return out


# free channels per profile: gpio, uart rx / tx (per uart), i2c sda / scl, spi sck / mosi / miso / cs, logic x2, analog
CHANNELS = {"p4-x035": {"gpio": 30, "uart": [(31, 32), (33, 34)], "i2c": (20, 21), "spi": (26, 27, 28, 29),
                        "logic": (36, 37), "analog": 17, "pwm": 40},
            "esp32-v003": {"gpio": 33, "uart": [(17, 19)], "i2c": (21, 22), "spi": (14, 13, 27, 26), "logic": (25, 32),
                           "pwm": 4, "dac": 25}}


def bench(probe, transport=1, tag=UNKNOWN):
    ep = endpoint.Endpoint(probe, Clock())
    tagging = Tagging(ep, transport, tag)
    hst = h.Host(tagging)
    return ep, tagging, hst


def every_op(ep: endpoint.Endpoint) -> set[tuple[str, int]]:
    out = {(CORE_NAME, op) for op in reg.CORE.op.values() if ep.offers(m.CORE_FN, op)}
    for fn, name in ep.names.items():
        if fn == m.CORE_FN:
            continue
        i = reg.INTERFACES.get(name)
        if i is None:
            out |= {(name, endpoint.TOY_WRITE), (name, endpoint.TOY_READ)}
        else:
            out |= {(name, op) for op in i.op.values() if ep.offers(fn, op)}   # optional ops when offered (core §1.2)
    return out - NO_TAIL


def drive_core(hst, ep):
    hst.confirm()
    core.list_entries(hst)
    core.describe(hst, 0)
    hst.open(3000, owner="tails")
    hst.lock_state()
    hst.keepalive()
    hst.clock()
    for name in ("oep.fixture.logic", "oep.fixture.analog", "oep.fixture.capture-group"):   # their own ops (§11.3)
        for fn in core.find_all(hst, name):
            hst.subscribe(fn, 0, 1000)
            hst.unsubscribe(fn)
    gpio = fixture.Gpio(hst)
    core.plan_apply(hst, [(gpio.fn, 1, CHANNELS[ep.probe.label]["gpio"])])
    core.plan_release(hst, [gpio.fn])
    link = core.link_fn(hst)                                          # oep.probe.link: the link test (oep-if-link §2)
    assert core.link_source_data(hst.call(link, core.LINK_SOURCE, core.link_source_request(8), locked=False).payload) \
        == bytes(range(8))
    hst.call(link, core.LINK_SINK, core.link_sink_request(b"abc"), locked=False)


def drive_restart(hst):
    """oep.probe.restart's restart (oep-if-restart), last: the probe restarts once the answer is out."""
    hst.open(3000)
    hst.request_restart()


def drive_wire_dm_console(hst, wire_name):
    wire = riscv.Wire(hst, wire_name)
    wire.scan()
    conn, _ = wire.attach(halt=True)
    wire.connections()
    wire.holds()                                          # debug §2.2, lock-free
    dm = riscv.RiscvDm(hst, conn)
    dm.dmi([dm.step_read(0x11)])
    dm.halt()
    dm.write_block(0x20000000, b"\x01\x02\x03\x04")
    dm.read_block(0x20000000, 4)
    dm.run(0x20000000, [(10, 1)], timeout_ms=10)
    dm.halt()
    if "step" in dm.declared():
        dm.step()                                                     # optional: in the ops (debug §4)
    dm.reset_halt()
    dm.resume()
    con = console.Console(hst)
    con.open(conn)
    con.read()
    con.marks()
    con.mark(7)
    con.write(b"x")
    con.clear()
    con.streams()
    con.close()
    wire.detach(conn)


DRIVE_NAMES = {"oep.fixture.i2c-controller", "oep.fixture.spi-controller", "oep.fixture.pwm", "oep.fixture.counter",
               "oep.fixture.dac"}


def drive_fixtures(hst, ep):
    ch = CHANNELS[ep.probe.label]
    gpio = fixture.Gpio(hst)
    core.plan_apply(hst, [(gpio.fn, 1, ch["gpio"])])
    gpio.set([(ch["gpio"], gpio.OUTPUT_HIGH)])
    gpio.read([ch["gpio"]])
    for fn, (rx, tx) in zip([fn for fn, name in ep.names.items() if name == "oep.fixture.uart"], ch["uart"]):
        uart = fixture.FixtureUart(hst, fn)
        core.plan_apply(hst, [(fn, 1, rx), (fn, 2, tx)])
        uart.configure(115200)
        uart.status()
        uart.write(b"hi")
        uart.read()
        uart.marks()
        uart.mark(1)
        uart.clear()
    i2c = fixture.I2cTarget(hst)
    core.plan_apply(hst, i2c.assignments(*ch["i2c"]))
    i2c.configure(0x42)
    i2c.preload_tx(b"\x11")
    i2c.read_rx()
    i2c.status()
    if i2c.max_stretch_us is not None:
        i2c.stretch(10)
    spi = fixture.SpiTarget(hst)
    core.plan_apply(hst, spi.assignments(*ch["spi"]))
    spi.configure(0)
    spi.arm(4, b"\x01")
    spi.read_rx()
    spi.status()
    core.plan_release(hst, [i2c.fn, spi.fn])                          # their pins for the controllers
    names = set(ep.names.values())
    if "oep.fixture.i2c-controller" in names:                          # fixture §5
        ic = fixture.I2cController(hst)
        core.plan_apply(hst, ic.assignments(*ch["i2c"]))
        ic.configure(100_000, 1_000)
        ic.write(0x50, b"\x00\x01")
        ic.status()
    if "oep.fixture.spi-controller" in names:                          # fixture §6
        sc = fixture.SpiController(hst)
        core.plan_apply(hst, sc.assignments(*ch["spi"]))
        sc.configure(0, 0, 1_000_000)
        sc.exchange(b"\x01")
        sc.status()
    if "oep.fixture.pwm" in names:                                     # fixture §7, §8: counted on its own pin
        pwm, counter = fixture.Pwm(hst), fixture.Counter(hst)
        core.plan_apply(hst, pwm.assignments(ch["pwm"]) + counter.assignments(ch["pwm"]))
        pwm.start(0, 1_000)
        pwm.status()
        counter.measure(1_000)
    if "oep.fixture.dac" in names:                                     # fixture §9
        dac = fixture.Dac(hst)
        core.plan_apply(hst, dac.assignments(ch["dac"]))
        dac.set([(0, 1)])
        dac.status()
    core.plan_release(hst, [fn for fn, name in ep.names.items() if name in DRIVE_NAMES])   # their pins for what follows


def drive_config(hst):
    st = settings.Settings(hst)
    st.set_named(settings.CORE_CATEGORY, {"channel.GPIO30.label": "t.nrst"})
    st.get()
    st.state()
    if st.offers("network"):
        st.network()                                               # the network op, when the probe has netifs
    st.unset_named(settings.CORE_CATEGORY, ["channel.GPIO30.label"])
    st.save()


def one_shot(cap, clock, **kw):
    cap.configure(query=True, **kw)
    cap.configure(**kw)
    cap.start()
    cap.force()
    clock.t += 5                                                         # one-shot completes once its samples have elapsed
    (seg,) = cap.wait()
    cap.status()
    cap.segments()
    cap.read_segment(seg)
    cap.release(seg.serial)
    cap.triggers()
    cap.stop()


def drive_captures(hst, ep):
    ch = CHANNELS[ep.probe.label]
    lc = c.LogicCapture(hst)
    core.plan_apply(hst, [(lc.fn, 0, ch["logic"][0]), (lc.fn, 1, ch["logic"][1])])
    one_shot(lc, ep.now, rate=1_000_000, samples=100)
    if "oep.fixture.analog" in ep.fns:
        an, grp = c.AnalogCapture(hst), c.CaptureGroup(hst)
        core.plan_apply(hst, [(an.fn, 0, ch["analog"])])
        one_shot(an, ep.now, rate=10_000, samples=10)
        an.calibration()
        grp.bind([lc, an])
        grp.start([lc, an])
        grp.force()
        grp.status()
        grp.stop()


def drive_all_p4_x035(hst, ep):
    drive_core(hst, ep)
    drive_wire_dm_console(hst, "oep.wire.rvswd")
    drive_fixtures(hst, ep)
    drive_captures(hst, ep)
    drive_config(hst)
    hst.end()
    drive_restart(hst)


def test_every_op_ignores_an_unknown_tlv_and_refuses_it_critical_p4_x035():
    ep, tag, hst = bench(virtual_bench.p4_x035())
    drive_all_p4_x035(hst, ep)
    assert every_op(ep) - tag.seen == set()


def test_every_op_ignores_an_unknown_tlv_and_refuses_it_critical_esp32_v003():
    ep, tag, hst = bench(virtual_bench.with_stand_in(virtual_bench.esp32_v003()), transport=0)
    drive_core(hst, ep)
    hst.call(ep.link_fn, endpoint.OP_PORT_STATE, b"", locked=False)                    # oep-if-link §1
    hst.call(ep.link_fn, endpoint.OP_PORT_SPEED,
             struct.pack("<IBH", 230400, reg.PROBE_LINK.enum["port_speed_step"]["try"], 500))   # oep-if-link §3
    drive_wire_dm_console(hst, "oep.wire.swio")
    drive_fixtures(hst, ep)
    drive_captures(hst, ep)
    drive_config(hst)
    toy = next(fn for fn, name in ep.names.items() if name not in reg.INTERFACES and fn)
    hst.call(toy, endpoint.TOY_WRITE, struct.pack("<I", 5))
    hst.request(toy, endpoint.TOY_READ, locked=False)
    hst.end()
    drive_restart(hst)
    assert every_op(ep) - tag.seen == set()


def test_tag_0x7f_is_an_unknown_tag_on_every_op():
    """core §2.2: tag numbers are 0x01-0x7F; the virtual bench implements no 0x7F, so a request carrying one is read like
    any unknown TLV - ignored plain, unsupported (the byte as received) critical."""
    ep, tagging, hst = bench(virtual_bench.p4_x035(), tag=0x7F)
    drive_all_p4_x035(hst, ep)
    assert every_op(ep) - tagging.seen == set()


@pytest.mark.parametrize("tag", [0x00, 0x80])
def test_a_tlv_numbered_0_is_malformed(tag):
    """core §2.2: TLV number 0, with or without the critical bit, is malformed in a request."""
    ep = endpoint.Endpoint(virtual_bench.p4_bench(), Clock())
    req = m.Request(1, m.CORE_FN, m.OP_CLOCK, struct.pack("<BH", tag, 0))
    assert m.Result.unpack(ep.handle(req.pack(), 1)).detail == m.MALFORMED


@pytest.mark.parametrize("op", ["describe", "get"])
def test_a_tlv_in_a_describe_or_get_request_follows_the_general_rule(op):
    """core §2.3: describe and the settings' get read their tail like any request (no malformed for a TLV there)."""
    ep = endpoint.Endpoint(virtual_bench.p4_bench(), Clock())
    hst = h.Host(lambda b: ep.handle(b, 1))
    if op == "describe":
        fn, op, payload = m.CORE_FN, reg.CORE.op["describe"], struct.pack("<HH", 0, 0)
    else:
        fn, op, payload = m.CORE_FN, reg.CORE.op["get"], struct.pack("<H", 0)
    plain = hst.request(fn, op, payload, locked=False)
    assert plain.succeeded
    assert hst.request(fn, op, payload + m.tlv(UNKNOWN, b"\x01"), locked=False).payload == plain.payload
    with pytest.raises(h.Unsupported) as e:
        hst.request(fn, op, payload + m.tlv(UNKNOWN, b"\x01", critical=True), locked=False)
    assert e.value.result.payload == bytes([UNKNOWN | m.TAG_CRITICAL])


def test_settings_set_refuses_an_item_it_does_not_declare_with_the_values_place():
    ep = endpoint.Endpoint(virtual_bench.p4_bench(), Clock())
    hst = h.Host(lambda b: ep.handle(b, 1))
    hst.open(3000)
    for number in (UNKNOWN, UNKNOWN | m.TAG_CRITICAL):           # a number past fn 0's items (core §13.5)
        with pytest.raises(h.Unsupported) as e:
            hst.call(m.CORE_FN, reg.CORE.op["set"], b"\x01" + struct.pack("<HBHH", 0, number, 30, 1) + b"\x01")
        assert e.value.item == 0


# ---- implemented TLVs: the same with or without bit 7 (core §2.3) ----------------------------------------------

class Recorder:
    """A transport that keeps, per (fn, op), the last request and the probe as it was just before it - so a test can
    send the same request with another tail to that probe (`variant`)."""

    def __init__(self, ep: endpoint.Endpoint, transport: int = 1):
        self.ep, self.transport, self.last = ep, transport, {}

    def __call__(self, data: bytes) -> bytes | None:
        req = m.Request.unpack(data)
        self.last[(req.fn, req.op)] = (req, copy.deepcopy(self.ep))
        return self.ep.handle(data, self.transport)

    def variant(self, fn: int, op: int, fixed: int, edit) -> m.Result:
        """The last (fn, op) request with its TLV tail - after `fixed` bytes - replaced by edit(tlvs), a list of
        (tag as sent, value) -> a list of the same, sent to the probe as it was before that request."""
        req, before = self.last[(fn, op)]
        head, tlvs = req.payload[:fixed], m.split_tlvs(req.payload[fixed:])
        return answer(before, req, head + b"".join(raw_tlv(t, v) for t, v in edit(list(tlvs))), self.transport)


def with_bit(tlvs, number, critical, value=None):
    """`tlvs` with every TLV of `number` sent with (critical) or without bit 7, its value replaced when given."""
    return [((t & 0x7F) | (m.TAG_CRITICAL if critical else 0), v if value is None else value)
            if t & 0x7F == number else (t, v) for t, v in tlvs]


def refused(r: m.Result) -> tuple[int, int, bytes]:
    assert r.resolution == m.REJECTED, r
    return r.detail, r.payload


def recorded(probe=None):
    ep = endpoint.Endpoint(probe or virtual_bench.p4_x035(), Clock())
    rec = Recorder(ep)
    hst = h.Host(rec)
    hst.open(3000)
    return ep, rec, hst


BITS = pytest.mark.parametrize("critical", [False, True])


@BITS
def test_attach_reset_of_another_length_is_malformed_either_way(critical):
    """The reset TLV is channel(u16) hold_ms(u16): shorter or longer is malformed, before the channel is looked at."""
    ep, rec, hst = recorded()
    wire = riscv.Wire(hst, "oep.wire.rvswd")
    wire.attach(halt=True)
    tag = wire.TAG_RESET & 0x7F
    sent = tag | (m.TAG_CRITICAL if critical else 0)
    for value in (b"\x17\x00\x14", b"\x17\x00\x14\x00\x00"):        # shorter, longer
        assert refused(rec.variant(wire.fn, wire.ATTACH, 9, lambda t: t + [(sent, value)])) == (m.MALFORMED, b"")


@BITS
def test_attach_idle_clock_value_left_unused_is_unsupported_with_the_tag_as_received(critical):
    """rvswd's idle_clock: 0 high, 1 low; 2 is a value the definition leaves unused (core §2.3, §2.5)."""
    ep, rec, hst = recorded()
    wire = riscv.Wire(hst, "oep.wire.rvswd")
    wire.attach(halt=True)
    tag = reg.WIRE_RVSWD.tlv["attach"]["idle_clock"]
    sent = tag | (m.TAG_CRITICAL if critical else 0)
    assert refused(rec.variant(wire.fn, wire.ATTACH, 9, lambda t: t + [(sent, b"\x02")])) \
        == (m.UNSUPPORTED, bytes([sent]))
    assert refused(rec.variant(wire.fn, wire.ATTACH, 9, lambda t: t + [(sent, b"\x01\x00")])) == (m.MALFORMED, b"")
    assert rec.variant(wire.fn, wire.ATTACH, 9, lambda t: t + [(sent, b"\x01")]).resolution == m.COMPLETED


def test_detach_has_no_tlv_any_more():
    """oep-if-debug §2: detach has no force TLV - its old tag is an unknown TLV (core §2.3)."""
    ep, rec, hst = recorded()
    wire = riscv.Wire(hst, "oep.wire.rvswd")
    conn, _ = wire.attach(halt=True)
    wire.detach(conn)
    assert rec.variant(wire.fn, wire.DETACH, 4, lambda t: [(0x01, b"")]).resolution == m.COMPLETED
    assert refused(rec.variant(wire.fn, wire.DETACH, 4, lambda t: [(0x81, b"")])) == (m.UNSUPPORTED, b"\x81")


@BITS
def test_uart_format_of_another_length_is_malformed_and_an_unhandled_one_unsupported(critical):
    ep, rec, hst = recorded()
    uart = fixture.FixtureUart(hst)
    core.plan_apply(hst, [(uart.fn, 1, 31), (uart.fn, 2, 32)])
    uart.configure(115200, 0)
    tag = uart.TAG_FORMAT
    sent = tag | (m.TAG_CRITICAL if critical else 0)
    ok = rec.variant(uart.fn, uart.CONFIGURE, 4, lambda t: with_bit(t, tag, critical))
    assert ok.resolution == m.COMPLETED and ok.detail == m.SUCCESS
    assert refused(rec.variant(uart.fn, uart.CONFIGURE, 4, lambda t: with_bit(t, tag, critical, b"\x00\x00"))) \
        == (m.MALFORMED, b"")
    for bad in (0x03, 0x80):                                       # parity 3, bit 7: values left unused (fixture §2)
        assert refused(rec.variant(uart.fn, uart.CONFIGURE, 4, lambda t: with_bit(t, tag, critical, bytes([bad])))) \
            == (m.UNSUPPORTED, bytes([sent]))


@BITS
def test_capture_configure_unhandled_values_are_unsupported_with_the_tag_as_received(critical):
    """capture §3.3 with core §2.3: mode / rate the probe does not handle -> unsupported with the tag as received,
    critical or not; another length -> malformed."""
    ep, rec, hst = recorded()
    lc = c.LogicCapture(hst)
    core.plan_apply(hst, [(lc.fn, 0, 36), (lc.fn, 1, 37)])
    lc.configure(rate=1_000_000, samples=100)
    mode, rate = c.MODE, c.RATE
    op = lc.CONFIGURE

    def send(number, value):
        return rec.variant(lc.fn, op, 0, lambda t: with_bit(t, number, critical, value))
    assert send(mode, None).resolution == m.COMPLETED
    sent = lambda n: bytes([n | (m.TAG_CRITICAL if critical else 0)])   # noqa: E731
    assert refused(send(mode, b"\xEE")) == (m.UNSUPPORTED, sent(mode))
    assert refused(send(rate, struct.pack("<I", 0xFFFFFFFF))) == (m.UNSUPPORTED, sent(rate))
    assert refused(send(rate, struct.pack("<IB", 1_000_000, 0))) == (m.MALFORMED, b"")
    assert refused(send(mode, b"\x00\x00")) == (m.MALFORMED, b"")


@BITS
def test_plan_apply_role_assignment_checked_either_way(critical):
    """oep-if-plan §2.1 with core §2.3: another length -> malformed; a channel the fn does not declare ->
    unsupported with the tag as received (this client sends role_assignment plain)."""
    ep, rec, hst = recorded()
    gpio = fixture.Gpio(hst)
    core.plan_apply(hst, [(gpio.fn, 1, 30)])
    plan = ep.fns["oep.probe.plan"]
    tag = reg.PROBE_PLAN.tlv["plan_apply"]["role_assignment"]
    sent = tag | (m.TAG_CRITICAL if critical else 0)
    req, _ = rec.last[(plan, core.OP_PLAN_APPLY)]
    assert req.payload[0] == tag                                   # sent plain (oep-if-plan §2.1)
    assert rec.variant(plan, core.OP_PLAN_APPLY, 0, lambda t: with_bit(t, tag, critical)).resolution == m.COMPLETED
    assert refused(rec.variant(plan, core.OP_PLAN_APPLY, 0,
                               lambda t: with_bit(t, tag, critical, struct.pack("<HBHB", gpio.fn, 1, 30, 0)))) \
        == (m.MALFORMED, b"")
    detail, payload = refused(rec.variant(plan, core.OP_PLAN_APPLY, 0,
                                          lambda t: with_bit(t, tag, critical, struct.pack("<HBH", gpio.fn, 1, 0xFFF0))))
    assert (detail, payload[:1]) == (m.UNSUPPORTED, bytes([sent]))


@BITS
def test_open_owner_length_checked_either_way(critical):
    """core §6.4: owner is text of 1-32 bytes (the length rule only, the text is not validated); another length ->
    malformed with or without bit 7."""
    ep, rec, hst = recorded()
    hst.end()
    hst.open(3000, owner="tails")
    owner = reg.CORE.tlv["open"]["owner"]
    assert rec.variant(m.CORE_FN, m.OP_OPEN, 5, lambda t: with_bit(t, owner, critical, b"\xff\x00")).resolution \
        == m.COMPLETED                                             # not UTF-8, a C0 byte: not looked at
    for value in (b"", b"x" * (reg.LIMITS["owner_max_bytes"] + 1)):
        assert refused(rec.variant(m.CORE_FN, m.OP_OPEN, 5, lambda t: with_bit(t, owner, critical, value))) \
            == (m.MALFORMED, b"")


def test_settings_value_of_another_length_is_malformed():
    """core §13.3: a value of another length than its form -> malformed (values have no bit-7 rule: a number is a
    place in the fn's setting list)."""
    ep = endpoint.Endpoint(virtual_bench.p4_bench(), Clock())
    hst = h.Host(lambda b: ep.handle(b, 1))
    hst.open(3000)
    idle = ep.setting_number("channel.{}.idle")
    low = bytes([reg.CORE.enum["channel_idle"]["low"]])
    hst.call(m.CORE_FN, reg.CORE.op["set"], b"\x01" + struct.pack("<HBHH", 0, idle, 30, 1) + low)
    for value in (b"", low + b"\x00"):
        with pytest.raises(h.Rejected) as e:
            hst.call(m.CORE_FN, reg.CORE.op["set"], b"\x01" + struct.pack("<HBHH", 0, idle, 30, len(value)) + value)
        assert e.value.result.detail == m.MALFORMED


# ---- a non-repeating tag twice: the first is used (core §2.3) ------------------------------------------------

def test_a_tag_sent_twice_uses_the_first():
    ep, rec, hst = recorded()
    uart = fixture.FixtureUart(hst)
    core.plan_apply(hst, [(uart.fn, 1, 31), (uart.fn, 2, 32)])
    uart.configure(115200, 0)
    tag = uart.TAG_FORMAT
    good, bad, short = (tag, b"\x00"), (tag, b"\x03"), (tag, b"")
    assert refused(rec.variant(uart.fn, uart.CONFIGURE, 4, lambda t: [good, bad])) == (m.UNSUPPORTED, bytes([tag]))
    assert refused(rec.variant(uart.fn, uart.CONFIGURE, 4, lambda t: [good, short])) == (m.MALFORMED, b"")  # later copies checked (core §2.3)
    assert refused(rec.variant(uart.fn, uart.CONFIGURE, 4, lambda t: [bad, good])) == (m.UNSUPPORTED, bytes([tag]))
    assert refused(rec.variant(uart.fn, uart.CONFIGURE, 4, lambda t: [short, good])) == (m.MALFORMED, b"")
    wire = riscv.Wire(hst, "oep.wire.rvswd")
    wire.attach(halt=True)
    idle = reg.WIRE_RVSWD.tlv["attach"]["idle_clock"]
    r = rec.variant(wire.fn, wire.ATTACH, 9, lambda t: [(idle | m.TAG_CRITICAL, b"\x01"), (idle, b"\x00")])
    assert r.resolution == m.COMPLETED                             # the first holds: rests low
    assert refused(rec.variant(wire.fn, wire.ATTACH, 9, lambda t: [(idle, b"\x01"), (idle, b"\x01\x00")])) \
        == (m.MALFORMED, b"")


def test_an_unknown_critical_and_a_malformed_known_tlv_get_either_reason():
    """core §4.3: every check before any change, and any one reason that applies."""
    ep, rec, hst = recorded()
    uart = fixture.FixtureUart(hst)
    core.plan_apply(hst, [(uart.fn, 1, 31), (uart.fn, 2, 32)])
    uart.configure(115200, 0)
    tag = uart.TAG_FORMAT
    crit = (UNKNOWN | m.TAG_CRITICAL, b"")
    for tlvs in ([crit, (tag, b"\x00\x00")], [(tag, b"\x00\x00"), crit]):
        assert refused(rec.variant(uart.fn, uart.CONFIGURE, 4, lambda t: tlvs)) in \
            ((m.MALFORMED, b""), (m.UNSUPPORTED, bytes([UNKNOWN | m.TAG_CRITICAL])))


class Failing:
    """A transport that appends the unknown TLV to every request and collects the completed answers whose outcome is
    failed or partial, with the same request's answer without it: (interface name, op) -> (with, without)."""

    def __init__(self, ep: endpoint.Endpoint):
        self.ep, self.failed = ep, {}

    def __call__(self, data: bytes) -> bytes | None:
        req = m.Request.unpack(data)
        key = (CORE_NAME if req.fn == m.CORE_FN else self.ep.names.get(req.fn, ""), req.op)
        if key in NO_TAIL:
            return self.ep.handle(data, 1)
        plain = answer(self.ep, req, req.payload, 1)
        out = self.ep.handle(dataclasses.replace(req, payload=req.payload + m.tlv(UNKNOWN, b"\x01")).pack(), 1)
        res = m.Result.unpack(out)
        if res.resolution == m.COMPLETED and res.detail != m.SUCCESS:
            self.failed[key] = (res, plain)
        return out


def test_a_failed_or_partial_answer_ignores_the_unknown_tlv_too():
    """core §2.3: an unknown non-critical TLV changes nothing, also when the op's status is a failure."""
    ep = endpoint.Endpoint(virtual_bench.p4_x035(), Clock())
    tag = Failing(ep)
    hst = h.Host(tag)
    hst.open(3000)
    wire = riscv.Wire(hst, "oep.wire.rvswd")
    tg = ep.targets[(wire.fn, ep.pairs[wire.fn][0])]
    tg.silent_until_reset = True
    with pytest.raises(h.Failed):
        wire.attach()                                              # failed, status line
    tg.silent_until_reset = False
    conn, _ = wire.attach(halt=False)
    dm = riscv.RiscvDm(hst, conn)
    tg.halted, tg.resume_misses, tg.fail_write = False, 1, {0x10}
    for call in (dm.step, lambda: dm.read_block(0x20000000, 4), lambda: dm.write_block(0x20000000, b"\0" * 4),
                 lambda: dm.run(0x20000000, [], timeout_ms=10), dm.resume,
                 lambda: dm.dmi([dm.step_read(0x11), dm.step_write(0x10, 1)])):
        try:
            call()
        except h.OepError:
            pass
    con = console.Console(hst)
    con.open(conn)
    ep.streams[con.stream].queue[:] = bytes(ep.send_queue)        # the send queue full (no time passes: no poll)
    with pytest.raises(h.OepError):
        con.write(b"x")                                            # nothing fit: failed
    assert len(tag.failed) == 8 and ("oep.wire.rvswd", wire.ATTACH) in tag.failed, tag.failed   # attach, 6 dm, write
    for key, (res, plain) in tag.failed.items():
        assert res == plain, key
