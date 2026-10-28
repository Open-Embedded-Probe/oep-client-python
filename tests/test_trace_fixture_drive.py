"""oep.fixture.i2c-controller / spi-controller / pwm / counter / dac (oep-spec interfaces/oep-if-fixture.ja.md §5-§9) on
the virtual bench: the plan's roles, configure and its refusals, the transfers and their results, the outputs and the
counts, what each does to its pins."""

import struct

import pytest

from oep_client import catalog, core, endpoint, fixture, host as h, message as m, registry as reg, virtual_bench as vb
from oep_client.virtual_fixture_drive import I2cDevice

I2CC, SPIC, PWM, CNT, DAC = (reg.FIXTURE_I2C_CONTROLLER, reg.FIXTURE_SPI_CONTROLLER, reg.FIXTURE_PWM,
                             reg.FIXTURE_COUNTER, reg.FIXTURE_DAC)
RESULT = I2CC.enum["result"]
EDGE = CNT.enum["edge"]
SDA, SCL = 40, 41                       # p4-x035 pins no other test fn here holds
SCK, MOSI, MISO, CS = 42, 43, 44, 45
OUT = 46


class Clock:
    t = 0

    def __call__(self):
        return self.t


def bench(profile=vb.p4_x035):
    ep = endpoint.Endpoint(profile(), Clock())
    hst = h.Host(lambda b: ep.handle(b, 1))
    hst.open(3000)
    return ep, hst


def raw(ep, hst, fn, op, payload=b"", locked=True) -> m.Result:
    """A request as sent, its result whatever it is."""
    return m.Result.unpack(ep.handle(m.Request(hst.next_corr(), fn, op, payload, hst.session if locked else 0).pack(), 1))


def cause(r: m.Result) -> int:
    return dict(m.split_tlvs(r.payload))[reg.CORE.tlv["unavailable_payload"]["cause"]][0]


WRONG_STATE = reg.CORE.enum["unavailable_cause"]["wrong_state"]
HELD_BY_SETTINGS = reg.CORE.enum["unavailable_cause"]["held_by_settings"]
PIN_IN_USE = reg.CORE.enum["unavailable_cause"]["pin_in_use"]


def replaced(probe, name, tlvs):
    """The profile with `name`'s declarations replaced (VIRTUAL BENCH ONLY: a probe of another shape)."""
    return lambda: vb._again(probe(), [vb._with(o, tlvs(o)) if o.name == name else o for o in probe().offered])


def with_decl(o, tag, value: bytes):
    tlvs = o if isinstance(o, tuple) else o.tlvs
    return tuple(t for t in tlvs if t[0] != tag) + (catalog.tlv(tag, value),)


# ---- §0 the interfaces -------------------------------------------------------------------------------------------

@pytest.mark.parametrize("name, roles", [(I2CC.name, {"sda": 1, "scl": 2}), (SPIC.name, {"sck": 1, "mosi": 2, "miso": 3, "cs": 4}),
                                         (PWM.name, None), (CNT.name, None), (DAC.name, None)])
def test_names_revisions_and_roles(name, roles):
    """fixture §0: the drive fixtures, revision 1; the controllers' roles are the table's, the others' role k is k."""
    ep, hst = bench(vb.esp32_v003)
    entry = next(e for e in core.list_entries(hst) if e.name == name)
    assert entry.revision == 1
    if roles:
        assert I2CC.enum["role"] == roles if name == I2CC.name else SPIC.enum["role"] == roles


# ---- §5 i2c-controller -------------------------------------------------------------------------------------------

def i2c(profile=vb.p4_x035, plan=True):
    ep, hst = bench(profile)
    ic = fixture.I2cController(hst)
    if plan:
        core.plan_apply(hst, ic.assignments(SDA, SCL))
    return ep, hst, ic


def test_i2c_plan_takes_sda_and_scl_once_on_two_channels():
    ep, hst, ic = i2c(plan=False)
    for got in ([(ic.fn, 1, SDA)], [(ic.fn, 1, SDA), (ic.fn, 2, SDA)], [(ic.fn, 1, SDA), (ic.fn, 1, SCL)]):
        with pytest.raises(h.Unsupported):
            core.plan_apply(hst, got)
    core.plan_apply(hst, ic.assignments(SDA, SCL))


def test_a_running_i2c_controller_refuses_a_plan_change_and_a_release_starts_it_again():
    ep, hst, ic = i2c()
    ic.configure(100_000, 1000)
    ic.write(0x50, b"\x00\x01", stop=False)
    with pytest.raises(h.Unavailable) as e:
        core.plan_apply(hst, ic.assignments(SDA, 47))
    assert e.value.cause == "wrong_state"
    core.plan_release(hst, [ic.fn])
    st = ic.status()
    assert (st.state, st.held, st.transfers, st.errors) == (0, False, 0, 0)


def test_i2c_lines_are_open_drain_from_configure_and_idle_in_state_0():
    ep, hst, ic = i2c()
    assert ep.pin_state(SDA) == ep.pin_state(SCL) == "idle hi-z"
    ic.configure(100_000, 1000)
    assert ic.internal_pullups and ep.pin_state(SDA) == ep.pin_state(SCL) == "open-drain pull-up"
    core.plan_release(hst, [ic.fn])
    assert ep.pin_state(SDA) == "idle hi-z"


def test_i2c_configure_rules():
    ep, hst, ic = i2c(plan=False)
    op = I2CC.op["configure"]
    assert raw(ep, hst, ic.fn, op, struct.pack("<II", 100_000, 1000)).detail == m.UNAVAILABLE   # no plan: cause 6
    core.plan_apply(hst, ic.assignments(SDA, SCL))
    for clock, stretch in ((0, 1000), (100_000, 0)):
        assert raw(ep, hst, ic.fn, op, struct.pack("<II", clock, stretch)).detail == m.MALFORMED
    for clock, stretch in ((ic.min_clock_hz - 1, 1000), (ic.max_clock_hz + 1, 1000), (100_000, ic.max_stretch_timeout_us + 1)):
        r = raw(ep, hst, ic.fn, op, struct.pack("<II", clock, stretch))
        assert (r.detail, r.payload) == (m.UNSUPPORTED, bytes([m.TAG_FIXED])) and ic.status().state == 0
    assert ic.configure(333_333, 1000) <= 333_333
    ic.write(0x51, b"\x00")                                        # an error, counted
    ic.configure(100_000, 1000)                                    # made anew: the counts go
    assert (ic.status().transfers, ic.status().errors) == (0, 0)
    ep.drive_fail_configure.add(ic.fn)
    r = raw(ep, hst, ic.fn, op, struct.pack("<II", 100_000, 1000))
    assert (r.resolution, r.detail, r.payload) == (m.COMPLETED, m.FAILED, b"") and ic.status().state == 0


def test_i2c_segments_with_repeated_start_and_an_address_only_write():
    ep, hst, ic = i2c()
    ic.configure(100_000, 1000)
    assert ic.write(0x50, b"\x10\xaa\xbb").ok and ic.write_read(0x50, b"\x10", 2).data == b"\xaa\xbb"
    assert ic.write(0x50, b"").ok                                  # count 0: the address byte alone
    ep.i2c_devices[ic.fn].update({0x00: I2cDevice(), 0x7F: I2cDevice()})   # every 7-bit address taken, reserved too
    assert ic.read(0x00, 1).ok and ic.read(0x7F, 1).ok


def test_without_stop_the_bus_stays_held_and_a_stop_alone_is_no_transfer():
    ep, hst, ic = i2c()
    ic.configure(100_000, 1000)
    ic.write(0x50, b"\x00", stop=False)
    assert ic.status().held
    ic.read(0x50, 1, stop=False)                                   # repeated START
    before = ic.status().transfers
    ic.stop()                                                      # n 0 with bit0: a STOP alone
    assert not ic.status().held and ic.status().transfers == before


def test_i2c_transfer_refusals_change_nothing():
    ep, hst, ic = i2c()
    op = I2CC.op["transfer"]
    seg = lambda d, a, c, data=b"": struct.pack("<BBH", d, a, c) + data        # noqa: E731
    assert raw(ep, hst, ic.fn, op, struct.pack("<BB", 1, 1) + seg(0, 0x50, 1, b"\x00")).detail == m.UNAVAILABLE   # state 0
    ic.configure(100_000, 1000)
    for body in (struct.pack("<BB", 0, 0), struct.pack("<BB", 0b11, 1) + seg(0, 0x50, 0), struct.pack("<BB", 1, 1) + seg(2, 0x50, 0),
                 struct.pack("<BB", 1, 1) + seg(0, 0x80, 0), struct.pack("<BB", 1, 1) + seg(1, 0x50, 0),
                 struct.pack("<BB", 1, 1) + seg(0, 0x50, 4, b"\x00")):
        assert raw(ep, hst, ic.fn, op, body).detail == m.MALFORMED, body
    big = ic.max_length + 1
    assert raw(ep, hst, ic.fn, op, struct.pack("<BB", 1, 1) + seg(1, 0x50, big)).detail == m.UNSUPPORTED
    assert ic.status().transfers == 0


@pytest.mark.parametrize("fault, result", [("absent", RESULT["address_nack"]), ("nack", RESULT["data_nack"]),
                                           ("collide", RESULT["bus_error"]), ("stretch", RESULT["stretch_timeout"])])
def test_i2c_results_stop_the_segments_and_let_the_bus_go(fault, result):
    ep, hst, ic = i2c()
    ic.configure(100_000, 1000)
    device = ep.i2c_devices[ic.fn][0x50]
    if fault == "nack":
        device.nack_at = 2                                         # the last byte too: result 2 (I2C allows it, the host judges)
    if fault == "collide":
        device.collide = True
    if fault == "stretch":
        device.stretch = True
    address = 0x51 if fault == "absent" else 0x50
    later = I2cDevice()
    ep.i2c_devices[ic.fn][0x52] = later
    done = ic.transfer([fixture.I2cSegment.write(0x52, b"\x00\x01"), fixture.I2cSegment.write(address, b"\x00\x01\x02"),
                        fixture.I2cSegment.write(0x52, b"\x00\x09")], stop=False)
    assert (done.result, done.at, done.index) == (result, 1, 2 if fault == "nack" else 0)
    assert later.memory[0] == 1                                    # the segment after the stop never ran
    st = ic.status()
    assert not st.held and (st.transfers, st.errors) == (1, 1)


def test_a_transfer_past_max_op_ms_is_refused_like_one_past_max_length():
    """fixture §5: one transfer fits the core's max_op_ms; a longer one (clock_hz and count) is unsupported."""
    long_ = replaced(vb.p4_x035, I2CC.name, lambda o: with_decl(o, catalog.MAX_LENGTH, struct.pack("<H", 4000)))
    ep, hst, ic = i2c(long_)
    ic.configure(ic.min_clock_hz, 1000)
    r = raw(ep, hst, ic.fn, I2CC.op["transfer"], struct.pack("<BB", 1, 1) + struct.pack("<BBH", 1, 0x50, 3000))
    assert r.detail == m.UNSUPPORTED and ic.status().transfers == 0


def test_i2c_status_counts_saturate():
    ep, hst, ic = i2c()
    ic.configure(100_000, 1000)
    ep.i2cc[ic.fn].transfers = ep.i2cc[ic.fn].errors = 0xFFFFFFFF
    ic.write(0x51, b"\x00")
    st = ic.status()
    assert (st.transfers, st.errors) == (0xFFFFFFFF, 0xFFFFFFFF)


@pytest.mark.parametrize("profile", [vb.p4_x035, vb.esp32_v003, vb.esp32_v003_64])
def test_controller_max_length_fits_every_frame(profile):
    """fixture §5 / §6: describe's max_length keeps the largest request and answer inside max_frame."""
    ep, hst = bench(profile)
    frame = profile().max_frame
    request, result = reg.LIMITS["request_header_bytes"], reg.LIMITS["result_header_bytes"]
    ic, sc = fixture.I2cController(hst), fixture.SpiController(hst)
    assert request + 1 + 1 + 4 + ic.max_length <= frame and result + 1 + 1 + 2 + 2 + ic.max_length <= frame
    assert request + 1 + 14 + sc.max_length <= frame and result + 14 + sc.max_length <= frame
    assert ic.max_stretch_timeout_us >= 1 and sc.time_step_ns and sc.modes and sc.max_delay_ns


# ---- §6 spi-controller -------------------------------------------------------------------------------------------

def spi(profile=vb.p4_x035, plan=True):
    ep, hst = bench(profile)
    sc = fixture.SpiController(hst)
    if plan:
        core.plan_apply(hst, sc.assignments(SCK, MOSI, MISO, CS))
    return ep, hst, sc


def test_spi_plan_takes_the_four_roles_once_on_four_channels():
    ep, hst, sc = spi(plan=False)
    with pytest.raises(h.Unsupported):
        core.plan_apply(hst, sc.assignments(SCK, MOSI, MISO, CS)[:3])
    with pytest.raises(h.Unsupported):
        core.plan_apply(hst, sc.assignments(SCK, MOSI, MISO, MISO))
    core.plan_apply(hst, sc.assignments(SCK, MOSI, MISO, CS))
    sc.configure(0, 0, 1_000_000)
    with pytest.raises(h.Unavailable) as e:
        core.plan_apply(hst, sc.assignments(SCK, MOSI, MISO, 47))
    assert e.value.cause == "wrong_state"


def test_spi_pins_from_configure_and_back_at_release():
    ep, hst, sc = spi()
    assert {ep.pin_state(c) for c in (SCK, MOSI, MISO, CS)} == {"idle hi-z"}
    sc.configure(3, 0, 1_000_000)
    assert [ep.pin_state(c) for c in (SCK, MOSI, MISO, CS)] == ["output", "output", "input", "output"]
    core.plan_release(hst, [sc.fn])
    st = sc.status()
    assert (st.state, st.mode, st.bit_order, st.transfers) == (0, 0, 0, 0) and ep.pin_state(SCK) == "idle hi-z"


def test_spi_configure_rules():
    ep, hst, sc = spi(plan=False)
    op = SPIC.op["configure"]
    assert raw(ep, hst, sc.fn, op, struct.pack("<BBI", 0, 0, 1_000_000)).detail == m.UNAVAILABLE
    core.plan_apply(hst, sc.assignments(SCK, MOSI, MISO, CS))
    for body in (struct.pack("<BBI", 4, 0, 1_000_000), struct.pack("<BBI", 0, 2, 1_000_000), struct.pack("<BBI", 0, 0, 0)):
        assert raw(ep, hst, sc.fn, op, body).detail == m.MALFORMED
    for body in (struct.pack("<BBI", 0, 0, sc.min_clock_hz - 1), struct.pack("<BBI", 0, 0, sc.max_clock_hz + 1)):
        assert raw(ep, hst, sc.fn, op, body).detail == m.UNSUPPORTED
    assert sc.configure(1, 1, 1_500_000) <= 1_500_000
    assert (sc.status().state, sc.status().mode, sc.status().bit_order) == (1, 1, 1)
    ep.drive_fail_configure.add(sc.fn)
    r = raw(ep, hst, sc.fn, op, struct.pack("<BBI", 0, 0, 1_000_000))
    assert (r.resolution, r.detail, r.payload) == (m.COMPLETED, m.FAILED, b"") and sc.status().state == 0


def test_a_mode_or_bit_order_not_declared_is_unsupported():
    narrow = replaced(vb.p4_x035, SPIC.name, lambda o: with_decl(with_decl(o, SPIC.tlv["describe"]["modes"], b"\x01"),
                                                                  catalog.FEATURES, struct.pack("<I", 0)))
    ep, hst, sc = spi(narrow)
    for body in (struct.pack("<BBI", 1, 0, 1_000_000), struct.pack("<BBI", 0, 1, 1_000_000)):
        assert raw(ep, hst, sc.fn, SPIC.op["configure"], body).detail == m.UNSUPPORTED


def test_spi_transfers_in_order_with_their_times_and_bit_order():
    ep, hst, sc = spi()
    seen = []
    ep.spi_device[sc.fn] = lambda mode, mosi: (seen.append(mosi), bytes(b ^ 0xFF for b in mosi))[1]
    sc.configure(0, 0, 1_000_000)
    done = sc.transfer([fixture.SpiItem(tx=b"\x01\x02", gap_ns=999, setup_ns=150, hold_ns=0), fixture.SpiItem(tx=b"", gap_ns=150,
                        setup_ns=100, hold_ns=250)])
    step = sc.time_step_ns
    assert [(d.gap_ns, d.setup_ns, d.hold_ns) for d in done] == [(0, 2 * step, 0), (2 * step, step, 3 * step)]
    assert done[0].rx == b"\xfe\xfd" and done[1].rx == b"" and sc.status().transfers == 2
    sc.configure(0, 1, 1_000_000)                                  # LSB first: the wire carries each byte reversed
    assert sc.exchange(b"\x01") == b"\xfe" and seen[-1] == bytes([0x80])


def test_spi_transfer_refusals_change_nothing():
    ep, hst, sc = spi()
    op = SPIC.op["transfer"]
    item = lambda count, tx=b"", delay=0: struct.pack("<IIIH", delay, delay, delay, count) + tx   # noqa: E731
    assert raw(ep, hst, sc.fn, op, b"\x01" + item(1, b"\x00")).detail == m.UNAVAILABLE
    sc.configure(0, 0, 1_000_000)
    assert raw(ep, hst, sc.fn, op, b"\x00").detail == m.MALFORMED
    assert raw(ep, hst, sc.fn, op, b"\x01" + item(2, b"\x00")).detail == m.MALFORMED
    assert raw(ep, hst, sc.fn, op, b"\x01" + item(sc.max_length + 1, bytes(sc.max_length + 1))).detail in (m.UNSUPPORTED, m.MALFORMED)
    assert raw(ep, hst, sc.fn, op, b"\x01" + item(1, b"\x00", sc.max_delay_ns + 1)).detail == m.UNSUPPORTED
    assert sc.status().transfers == 0


# ---- §7 pwm, §8 counter --------------------------------------------------------------------------------------------

def outputs(profile=vb.p4_x035):
    ep, hst = bench(profile)
    return ep, hst, fixture.Pwm(hst), fixture.Counter(hst)


def test_pwm_roles_once_on_their_own_channels_and_counter_roles_once():
    ep, hst, pwm, cnt = outputs()
    for got in ([(pwm.fn, 0, OUT), (pwm.fn, 0, 47)], [(pwm.fn, 0, OUT), (pwm.fn, 1, OUT)], [(cnt.fn, 0, OUT), (cnt.fn, 0, 47)]):
        with pytest.raises(h.Unsupported):
            core.plan_apply(hst, got)
    core.plan_apply(hst, [(cnt.fn, 0, OUT), (cnt.fn, 1, OUT)])     # a counter listens: one channel twice is fine


def test_pwm_set_status_and_stop():
    ep, hst, pwm, cnt = outputs()
    core.plan_apply(hst, pwm.assignments(OUT, 47))
    assert ep.pin_state(OUT) == "idle hi-z"                        # kept as it was until the first set
    assert [(o.role, o.num, o.den, o.duty) for o in pwm.status()] == [(0, 0, 1, 0), (1, 0, 1, 0)]
    made = pwm.set([(0, 1000, 32768), (1, 50_000, 0)])
    assert abs(made[0].hz - 1000) < 1 and made[1].duty == 0 and ep.pin_state(OUT) == "pwm"
    assert pwm.set([(1, 2000, 0xFFFF)])[0].duty == 0xFFFF
    stopped = pwm.stop(0)
    assert (stopped.num, stopped.den, stopped.duty) == (0, 1, 0) and ep.pin_state(OUT) == "output low"
    core.plan_release(hst, [pwm.fn])
    assert ep.pin_state(OUT) == "idle hi-z" and pwm.status() == []


def test_pwm_set_refusals_change_nothing():
    ep, hst, pwm, cnt = outputs()
    core.plan_apply(hst, pwm.assignments(OUT))
    op = PWM.op["set"]
    row = lambda role, hz, duty=32768: struct.pack("<BIH", role, hz, duty)   # noqa: E731
    assert raw(ep, hst, pwm.fn, op, b"\x00").detail == m.MALFORMED
    assert raw(ep, hst, pwm.fn, op, b"\x02" + row(0, 1000)).detail == m.MALFORMED
    for body in (b"\x01" + row(pwm.channels, 1000), b"\x02" + row(0, 1000) + row(0, 2000),
                 b"\x01" + row(0, pwm.min_clock_hz - 1), b"\x01" + row(0, pwm.max_clock_hz + 1)):
        r = raw(ep, hst, pwm.fn, op, body)
        assert (r.detail, r.payload) == (m.UNSUPPORTED, bytes([m.TAG_FIXED])), body
    r = raw(ep, hst, pwm.fn, op, b"\x01" + row(1, 1000))
    assert r.detail == m.UNAVAILABLE and cause(r) == WRONG_STATE
    assert pwm.status()[0].num == 0


def test_the_counter_counts_a_pwm_on_its_own_pin_and_the_edges_chosen():
    ep, hst, pwm, cnt = outputs()
    core.plan_apply(hst, pwm.assignments(OUT) + cnt.assignments(OUT, 47))
    ep.counter_signals[47] = 5000.0
    pwm.start(0, 2000)
    assert ep.pin_state(OUT) == "pwm"                              # the counter changes nothing on it
    got = cnt.measure(100_000, EDGE["rising"])
    assert got.window_ns == 100_000_000 and got.counts == {0: 200, 1: 500}
    assert cnt.measure(100_000, EDGE["both"]).counts == {0: 400, 1: 1000}
    ep.counter_signals[47] = 10 * cnt.max_clock_hz
    assert cnt.measure(1000, EDGE["falling"]).counts[1] <= cnt.max_clock_hz * 1000 // 1_000_000   # faster may be missed


def test_counter_refusals():
    ep, hst, pwm, cnt = outputs()
    op = CNT.op["measure"]
    assert raw(ep, hst, cnt.fn, op, struct.pack("<IB", 1000, 0)).detail == m.UNAVAILABLE   # no plan
    core.plan_apply(hst, cnt.assignments(OUT))
    for body in (struct.pack("<IB", 0, 0), struct.pack("<IB", 1000, 3)):
        assert raw(ep, hst, cnt.fn, op, body).detail == m.MALFORMED
    assert raw(ep, hst, cnt.fn, op, struct.pack("<IB", cnt.max_gate_us + 1, 0)).detail == m.UNSUPPORTED


def test_the_counter_shares_an_output_idle_or_a_gpio_pin():
    ep, hst, pwm, cnt = outputs()
    gpio = fixture.Gpio(hst)
    core.plan_apply(hst, [(gpio.fn, 1, OUT)] + cnt.assignments(OUT))
    gpio.set([(OUT, gpio.OUTPUT_HIGH)])
    assert ep.pin_state(OUT) == "gpio output-high" or ep.pin_state(OUT).startswith("gpio")


def test_pwm_and_counter_describe():
    ep, hst, pwm, cnt = outputs()
    assert pwm.channels and pwm.duty_bits and pwm.min_clock_hz <= pwm.max_clock_hz
    assert cnt.channels and cnt.max_gate_us * 1000 // 1_000_000 <= vb.MAX_OP_MS * 1000 and cnt.max_clock_hz


# ---- §9 dac ----------------------------------------------------------------------------------------------------------

def dac_bench():
    ep, hst = bench(vb.esp32_v003)
    return ep, hst, fixture.Dac(hst)


def test_the_dac_shares_its_pin_with_nothing():
    ep, hst, dac = dac_bench()
    gpio = fixture.Gpio(hst)
    core.plan_apply(hst, [(gpio.fn, 1, 25)])
    with pytest.raises(h.Unavailable):
        core.plan_apply(hst, dac.assignments(25))
    core.plan_release(hst, [gpio.fn])
    core.plan_apply(hst, dac.assignments(25))
    with pytest.raises(h.Unavailable):
        core.plan_apply(hst, [(gpio.fn, 1, 25)])


def test_dac_set_status_and_release():
    ep, hst, dac = dac_bench()
    core.plan_apply(hst, dac.assignments(25, 26))
    assert ep.pin_state(25) == "idle hi-z" and dac.status() == [(0, 0, False), (1, 0, False)]
    dac.set([(0, 128), (1, (1 << dac.bits) - 1)])
    assert dac.status() == [(0, 128, True), (1, 255, True)] and ep.pin_state(25) == "analog"
    core.plan_release(hst, [dac.fn])
    assert ep.pin_state(25) == "idle hi-z"


def test_dac_refusals():
    ep, hst, dac = dac_bench()
    core.plan_apply(hst, dac.assignments(25))
    op = DAC.op["set"]
    assert raw(ep, hst, dac.fn, op, b"\x00").detail == m.MALFORMED
    for body in (b"\x01" + struct.pack("<BI", 0, 1 << dac.bits), b"\x01" + struct.pack("<BI", dac.channels, 1)):
        assert raw(ep, hst, dac.fn, op, body).detail == m.UNSUPPORTED
    r = raw(ep, hst, dac.fn, op, b"\x01" + struct.pack("<BI", 1, 1))
    assert r.detail == m.UNAVAILABLE and cause(r) == WRONG_STATE


def test_dac_describe_and_the_nominal_voltage():
    ep, hst, dac = dac_bench()
    low, high = dac.range_mv
    assert dac.channels == 2 and dac.bits == 8 and (low, high) == (0, 3300)
    assert dac.value_for_mv(high) == (1 << dac.bits) - 1 and dac.value_for_mv(low) == 0
