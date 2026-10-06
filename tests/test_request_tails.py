"""The request-tail rule (oep-core §2.3) on every op the fake serves: an unknown non-critical TLV appended to a request
that completes is listed once in the answer's ignored (0x7F), and the same request with the TLV critical is rejected
unsupported with the tag as received. The requests come from the client classes driving each interface, so the
answers are also read back through the client with the extra TLV in them."""

import copy
import dataclasses
import struct

import pytest

from oep_client import (capture as c, config, console, core, endpoint, fake, fixture, host as h, message as m,
                        registry as reg, riscv)

UNKNOWN = 0x3D                                 # no context defines it
IGNORED = bytes([m.TAG_IGNORED, 1, 0, UNKNOWN])           # 0x7F len(u16) the tag (core §2.2, §2.3)
CORE_NAME = "(core)"                           # fn 0: the core has no name (core §0); a label for these tests
# requests that take no TLV tail of this kind: describe / probe.config get (a TLV there is malformed, core §7.3),
# probe.config set (its TLVs are the items: an unknown one is an undeclared item, rejected unsupported - probe.config §1)
NO_TAIL = {(CORE_NAME, reg.CORE.op["describe"]), ("oep.probe.config", reg.PROBE_CONFIG.op["get"]),
           ("oep.probe.config", reg.PROBE_CONFIG.op["set"])}


class Clock:
    t = 0

    def __call__(self):
        return self.t


class Tagging:
    """A transport that appends the unknown TLV to every request. A completed answer must end in exactly one ignored
    TLV naming it; the same request with the critical bit, sent to a copy of the probe as it was, must be rejected
    unsupported. `seen` collects the (interface name, op) pairs that completed with it."""

    def __init__(self, ep: endpoint.Endpoint, transport: int = 1):
        self.ep, self.transport, self.seen = ep, transport, set()

    def name(self, fn: int) -> str:
        return CORE_NAME if fn == m.CORE_FN else self.ep.names.get(fn, "")

    def __call__(self, data: bytes) -> bytes | None:
        req = m.Request.unpack(data)
        key = (self.name(req.fn), req.op)
        if key in NO_TAIL or (key == (CORE_NAME, m.OP_CONFIRM) and self.ep.revision == 0):
            return self.ep.handle(data, self.transport)
        twin = copy.deepcopy(self.ep)
        crit = twin.handle(dataclasses.replace(req, payload=req.payload + m.tlv(UNKNOWN | m.TAG_CRITICAL, b"\x01")).pack(),
                           self.transport)
        out = self.ep.handle(dataclasses.replace(req, payload=req.payload + m.tlv(UNKNOWN, b"\x01")).pack(),
                             self.transport)
        res = m.Result.unpack(out)
        if res.resolution == m.COMPLETED:
            assert res.payload.endswith(IGNORED), (key, res.payload.hex())
            assert not res.payload[:-len(IGNORED)].endswith(IGNORED), (key, "listed twice")
            r = m.Result.unpack(crit)
            assert (r.resolution, r.detail, r.payload) == (m.REJECTED, m.UNSUPPORTED, bytes([UNKNOWN | m.TAG_CRITICAL])), key
            self.seen.add(key)
        return out


# free channels per profile: gpio, uart rx / tx (per uart), i2c sda / scl, spi sck / mosi / miso / cs, logic x2, analog
CHANNELS = {"p4-x035": {"gpio": 30, "uart": [(31, 32), (33, 34)], "i2c": (20, 21), "spi": (26, 27, 28, 29),
                        "logic": (36, 37), "analog": 17},
            "esp32-v003": {"gpio": 33, "uart": [(17, 19)], "i2c": (21, 22), "spi": (14, 13, 27, 26), "logic": (25, 32)}}


def bench(probe, transport=1):
    ep = endpoint.Endpoint(probe, Clock())
    tag = Tagging(ep, transport)
    hst = h.Host(tag)
    return ep, tag, hst


def every_op(ep: endpoint.Endpoint) -> set[tuple[str, int]]:
    out = {(CORE_NAME, op) for op in reg.CORE.op.values() if ep.offers(m.CORE_FN, op)}
    for fn, name in ep.names.items():
        if fn == m.CORE_FN:
            continue
        i = reg.INTERFACES.get(name)
        if i is None:
            out |= {(name, endpoint.TOY_WRITE), (name, endpoint.TOY_READ)}
        elif fn != m.CORE_FN:
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
    i2c.configure(0x42, i2c.MODE_FIXED_RX)
    i2c.arm_rx(4)
    i2c.read_rx()
    i2c.configure(0x42, i2c.MODE_PRELOADED_TX)
    i2c.preload_tx(b"\x11")
    i2c.status()
    if i2c.max_stretch_us is not None:
        i2c.stretch(10)
    i2c.reset()
    spi = fixture.SpiTarget(hst)
    core.plan_apply(hst, spi.assignments(*ch["spi"]))
    spi.configure(0)
    spi.arm(4, b"\x01")
    spi.read_rx()
    spi.status()
    spi.reset()


def drive_config(hst):
    cfg = config.ProbeConfig(hst)
    cfg.describe()
    cfg.set([config.Label(channel=30, text="t.nrst")])
    cfg.get()
    cfg.state()
    cfg.unset([("label", 30)])
    cfg.save()
    cfg.erase()


def one_shot(cap, **kw):
    cap.configure(query=True, **kw)
    cap.configure(**kw)
    cap.start()
    cap.force()
    (seg,) = cap.wait()
    cap.status()
    cap.segments()
    cap.read_segment(seg)
    cap.release(seg.serial)
    cap.stop()


def drive_captures(hst, ep):
    ch = CHANNELS[ep.probe.label]
    lc = c.LogicCapture(hst)
    core.plan_apply(hst, [(lc.fn, 0, ch["logic"][0]), (lc.fn, 1, ch["logic"][1])])
    one_shot(lc, rate=1_000_000, samples=100)
    if "oep.fixture.analog" in ep.fns:
        an, grp = c.AnalogCapture(hst), c.CaptureGroup(hst)
        core.plan_apply(hst, [(an.fn, 0, ch["analog"])])
        one_shot(an, rate=10_000, samples=10)
        an.calibration()
        grp.bind([lc, an])
        grp.start([lc, an])
        grp.force()
        grp.status()
        grp.stop()


def test_every_op_lists_an_unknown_tlv_and_refuses_it_critical_p4_x035():
    ep, tag, hst = bench(fake.p4_x035())
    drive_core(hst, ep)
    drive_wire_dm_console(hst, "oep.wire.rvswd")
    drive_fixtures(hst, ep)
    drive_captures(hst, ep)
    drive_config(hst)
    hst.end()
    drive_restart(hst)
    assert every_op(ep) - tag.seen == set()


def test_every_op_lists_an_unknown_tlv_and_refuses_it_critical_esp32_v003():
    ep, tag, hst = bench(fake.with_stand_in(fake.esp32_v003()), transport=0)
    drive_core(hst, ep)
    hst.call(ep.link_fn, endpoint.OP_PORT_SPEED, struct.pack("<BIBHI", 0, 230400, 0, 500, 0))   # try
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


@pytest.mark.parametrize("op, payload", [
    (reg.CORE.op["describe"], struct.pack("<HH", 0, 0)),
    ("get", struct.pack("<H", 0)),
])
def test_a_tlv_in_a_describe_or_get_request_is_malformed(op, payload):
    ep = endpoint.Endpoint(fake.p4_bench(), Clock())
    hst = h.Host(lambda b: ep.handle(b, 1))
    fn = m.CORE_FN if op != "get" else ep.fns["oep.probe.config"]
    op = op if op != "get" else reg.PROBE_CONFIG.op["get"]
    assert hst.request(fn, op, payload, locked=False).succeeded
    with pytest.raises(h.Rejected, match="malformed"):
        hst.request(fn, op, payload + m.tlv(UNKNOWN, b"\x01"), locked=False)


def test_plan_apply_lists_an_unknown_tlv_and_refuses_tag_0x7f():
    ep = endpoint.Endpoint(fake.p4_bench(), Clock())
    hst = h.Host(lambda b: ep.handle(b, 1))
    hst.open(3000)
    gpio = ep.fns["oep.fixture.gpio"]
    ra = m.tlv(reg.PROBE_PLAN.tlv["plan_apply"]["role_assignment"], struct.pack("<HBH", gpio, 1, 30), critical=True)
    plan = ep.fns["oep.probe.plan"]
    r = hst.request(plan, core.OP_PLAN_APPLY, ra + m.tlv(UNKNOWN, b""))
    assert r.succeeded and r.payload == IGNORED
    for bad in (m.TAG_IGNORED, m.TAG_INVALID, m.TAG_FIXED):
        with pytest.raises(h.Rejected, match="malformed"):
            hst.request(plan, core.OP_PLAN_APPLY, ra + bytes([bad, 0]))


def test_config_set_refuses_an_unknown_item_with_the_tag_as_received():
    ep = endpoint.Endpoint(fake.p4_bench(), Clock())
    hst = h.Host(lambda b: ep.handle(b, 1))
    hst.open(3000)
    fn = ep.fns["oep.probe.config"]
    for tag in (UNKNOWN, UNKNOWN | m.TAG_CRITICAL):              # an item it does not declare (probe.config §1)
        with pytest.raises(h.Unsupported) as e:
            hst.call(fn, reg.PROBE_CONFIG.op["set"], m.tlv(tag, b"\x01"))
        assert e.value.result.payload[:1] == bytes([tag])


class Failing:
    """A transport that appends the unknown TLV to every request and collects the completed answers whose outcome is
    failed or partial: (interface name, op) -> the answer's payload."""

    def __init__(self, ep: endpoint.Endpoint):
        self.ep, self.failed = ep, {}

    def __call__(self, data: bytes) -> bytes | None:
        req = m.Request.unpack(data)
        key = (CORE_NAME if req.fn == m.CORE_FN else self.ep.names.get(req.fn, ""), req.op)
        if key in NO_TAIL:
            return self.ep.handle(data, 1)
        out = self.ep.handle(dataclasses.replace(req, payload=req.payload + m.tlv(UNKNOWN, b"\x01")).pack(), 1)
        res = m.Result.unpack(out)
        if res.resolution == m.COMPLETED and res.detail != m.SUCCESS:
            self.failed[key] = res.payload
        return out


def test_a_failed_or_partial_answer_lists_the_unknown_tlv_too():
    """core §2.3 (oep-spec 9ed53e7): ignored goes on every completed answer, also when the op's status is a failure."""
    ep = endpoint.Endpoint(fake.p4_x035(), Clock())
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
    for key, payload in tag.failed.items():
        assert payload.endswith(IGNORED), (key, payload.hex())
        assert not payload[:-len(IGNORED)].endswith(IGNORED), (key, "listed twice")
