"""The fake's oep.fixture.i2c-target / spi-target (oep-spec oep-if-fixture §3 / §4) through the client classes: the
plan roles, configure / arm / preload / read_rx / status / reset, the refusals in core §4.3's order, and the bus side
by the endpoint's test hooks (i2c_write / i2c_read / spi_transfer: the fake has no bus controller of its own)."""

import random

import pytest

from oep_client import core, endpoint, fake, fixture, host, message as m

P4_I2C, P4_SPI = 8, 9                      # p4_x035: queue_depth 8, max_length 128 / 64, i2c features 0b11, spi 0b1
V003_I2C, V003_SPI = 7, 8                  # esp32_v003: queue_depth 4, max_length 16 / 32, i2c 0b01, spi 0 (groups)


class Clock:
    def __init__(self):
        self.ms = 0

    def __call__(self):
        return self.ms


def bench(probe):
    clock = Clock()
    ep = endpoint.Endpoint(probe, clock)
    hst = host.Host(ep.handle, rng=random.Random(1))
    hst.open(lease_ms=10000)
    return ep, hst, clock


def detail(exc) -> int:
    return exc.value.result.detail


def i2c_status(t):
    st = t.status()
    return st.state, st.mode, st.armed, st.queued, st.rx_frames, st.tx_slots, st.errors


def spi_status(t):
    st = t.status()
    return st.state, st.mode, st.bit_order, st.armed, st.queued, st.transactions, st.errors


# ---- i2c-target ---------------------------------------------------------------------------------------------------

@pytest.fixture
def i2c():
    ep, hst, clock = bench(fake.p4_x035())
    t = fixture.I2cTarget(hst)
    assert t.fn == P4_I2C
    core.plan_apply(hst, t.assignments(20, 21))
    return ep, t, clock


def test_i2c_plan_roles_and_channels():
    ep, hst, _ = bench(fake.p4_x035())
    t = fixture.I2cTarget(hst)
    with pytest.raises(host.Unsupported):
        core.plan_apply(hst, t.assignments(20, 21) + [(t.fn, 3, 22)])   # no role 3
    for bad in ([(t.fn, 1, 20)],                                    # SCL missing
                t.assignments(20, 21) + [(t.fn, 1, 22)],            # SDA twice
                t.assignments(20, 20)):                             # both roles on one channel
        with pytest.raises(host.Rejected) as e:
            core.plan_apply(hst, bad)
        assert detail(e) == m.MALFORMED
    assert not any(a[0] == t.fn for a in ep.plan)
    with pytest.raises(host.Unsupported):
        core.plan_apply(hst, t.assignments(2, 21))                  # 2 is reserved (SWDIO): not a candidate
    core.plan_apply(hst, [(fixture.Gpio(hst).fn, 1, 30)])
    with pytest.raises(host.Unavailable) as e:
        core.plan_apply(hst, t.assignments(30, 21))                 # the gpio holds 30
    assert e.value.cause == "pin_in_use" and e.value.channels == [30]
    core.plan_apply(hst, t.assignments(20, 21))
    assert {a for a in ep.plan if a[0] == t.fn} == {(t.fn, 1, 20), (t.fn, 2, 21)}


def test_i2c_refusals_before_configure_and_in_order():
    ep, hst, _ = bench(fake.esp32_v003())
    t = fixture.I2cTarget(hst)
    assert t.fn == V003_I2C
    with pytest.raises(host.Unavailable) as e:
        t.configure(0x42, t.MODE_FIXED_RX)                          # before the plan
    assert e.value.cause == "wrong_state"
    for address, mode, why in ((0x80, 1, m.MALFORMED), (0x80, 4, m.MALFORMED), (0x42, 0, m.UNSUPPORTED),
                               (0x42, 4, m.UNSUPPORTED)):
        with pytest.raises(host.Rejected) as e:                     # malformed, then unsupported, before the missing plan
            t.configure(address, mode)
        assert detail(e) == why                                     # mode 0 / 4+: a later revision may define it (C-02)
    assert i2c_status(t) == (0, 0, False, 0, 0, 0, 0)
    with pytest.raises(host.Unavailable):
        t.reset()                                                   # state 0
    with pytest.raises(host.Rejected) as e:
        t.stretch(10)                                               # no features bit1 on this probe
    assert detail(e) == m.UNKNOWN_OPERATION
    with pytest.raises(host.Rejected) as e:
        t.arm_rx(0)
    assert detail(e) == m.MALFORMED
    with pytest.raises(host.Unsupported):
        t.arm_rx(17)                                                # over max_length 16, before the state
    with pytest.raises(host.Unavailable):
        t.arm_rx(4)                                                 # not configured
    with pytest.raises(host.Unavailable) as e:
        t.read_rx()                                                 # state 0
    assert e.value.cause == "wrong_state"


def test_i2c_declarations():
    _, hst, _ = bench(fake.p4_x035())
    t = fixture.I2cTarget(hst)
    assert (t.max_length, t.max_clock_hz, t.features, t.queue_depth, t.max_stretch_us) == (128, 1_000_000, 0b11, 8, 100_000)
    _, hst, _ = bench(fake.esp32_v003())
    t = fixture.I2cTarget(hst)
    assert (t.max_length, t.max_clock_hz, t.features, t.queue_depth, t.max_stretch_us) == (16, 100_000, 0b01, 4, None)
    assert not t.features & t.FEATURE_STRETCH


def test_i2c_mode_3_only_when_declared():
    probe = fake.esp32_v003()
    probe = fake.FakeProbe(probe.label, probe.max_frame, [
        o if o.fn != V003_I2C else fake._i2c_target(V003_I2C, [25, 26], 16, 100_000, features=0, queue_depth=4)
        for o in probe.offered])
    ep, hst, _ = bench(probe)
    t = fixture.I2cTarget(hst)
    core.plan_apply(hst, t.assignments(25, 26))
    with pytest.raises(host.Unsupported):
        t.configure(0x42, t.MODE_PRELOADED_TX)
    t.configure(0x42, t.MODE_FRAMED_RX)


def test_i2c_fixed_rx_queued_consumed_and_overflow(i2c):
    ep, t, clock = i2c
    t.configure(0x42, t.MODE_FIXED_RX)
    assert i2c_status(t) == (1, 1, False, 0, 0, 0, 0)
    assert ep.i2c_write(t.fn, b"\x01\x02\x03\x04", 0x42)            # not armed: ACKed, dropped, an error
    assert not ep.i2c_write(t.fn, b"\x01", 0x43)                    # another address: not ACKed, not counted
    assert i2c_status(t) == (1, 1, False, 0, 0, 0, 1)
    t.arm_rx(2)
    t.arm_rx(4)                                                     # the new length replaces the wait
    assert t.status().armed
    ep.i2c_write(t.fn, b"\xaa\xbb", 0x42)                           # not the armed length: a receive error, still armed
    assert i2c_status(t) == (1, 1, True, 0, 0, 0, 2)
    assert ep.i2c_write(t.fn, b"", 0x42)                            # address only: ACKed, counts nothing
    assert i2c_status(t) == (1, 1, True, 0, 0, 0, 2)
    clock.ms = 5
    ep.i2c_write(t.fn, b"\x10\x20\x30\x40", 0x42)                   # the frame: queued, the wait goes on
    assert i2c_status(t) == (1, 1, True, 1, 1, 0, 2)
    ep.i2c_write(t.fn, b"\x50\x60\x70\x80", 0x42)                   # a second frame of the same length
    assert i2c_status(t) == (1, 1, True, 2, 2, 0, 2)
    assert t.read_rx() == (1, b"\x10\x20\x30\x40") and t.last_ns == 5_000_000
    assert t.read_rx() == (0, b"\x50\x60\x70\x80")
    assert t.read_rx() == (0, b"") and t.last_ns is None            # nothing left: count 0
    t.arm_rx(1)
    for k in range(9):                                              # queue_depth 8: the 9th frame overflows
        ep.i2c_write(t.fn, bytes([k]))
    assert i2c_status(t) == (1, 1, True, 8, 10, 0, 3)               # the dropped frame is not in rx_frames
    assert t.read_rx() == (7, b"\x00")                              # the oldest; the newest one went
    assert ep.i2c_read(t.fn, 2) == b"\xff\xff"                      # mode 1 answers reads with 0xFF
    t.reset()
    assert i2c_status(t) == (1, 1, False, 0, 0, 0, 0)               # mode and address kept, the rest gone
    assert ep.i2c_write(t.fn, b"")                                  # address only, not armed: still nothing
    assert t.status().errors == 0
    t.arm_rx(2)
    t.configure(0x42, t.MODE_FIXED_RX)
    assert not t.status().armed                                     # configure ends the wait


def test_i2c_framed_rx(i2c):
    ep, t, _ = i2c
    t.configure(0x42, t.MODE_FRAMED_RX)
    with pytest.raises(host.Unavailable):
        t.arm_rx(4)                                                 # mode 1 only
    ep.i2c_write(t.fn, b"\x03abc")
    ep.i2c_write(t.fn, b"\x05ab")                                   # the length does not match: an error
    ep.i2c_write(t.fn, b"\x00")                                     # L = 0: an error
    ep.i2c_write(t.fn, bytes([129]) + bytes(129))                   # L over max_length 128: an error
    ep.i2c_write(t.fn, b"")                                         # address only: nothing
    assert i2c_status(t) == (1, 2, False, 1, 1, 0, 3)
    assert t.read_rx() == (0, b"abc")


def test_i2c_preloaded_tx_slots(i2c):
    ep, t, _ = i2c
    with pytest.raises(host.Unavailable):
        t.preload_tx(b"\x01")                                       # not configured
    t.configure(0x42, t.MODE_PRELOADED_TX)
    with pytest.raises(host.Rejected) as e:
        t.preload_tx(b"")
    assert detail(e) == m.MALFORMED
    with pytest.raises(host.Unsupported):
        t.preload_tx(bytes(129))                                    # over max_length 128
    assert [t.preload_tx(bytes([0xA0 + i, i])) for i in range(8)] == list(range(1, 9))
    with pytest.raises(host.Unavailable) as e:
        t.preload_tx(b"\x09")                                       # queue_depth slots unread
    assert e.value.cause == "limit"
    assert i2c_status(t) == (1, 3, False, 0, 0, 8, 0)
    assert ep.i2c_read(t.fn, 3) == b"\xa0\x00\xff"                  # past the slot: 0xFF
    assert ep.i2c_read(t.fn, 1) == b"\xa1"                          # shorter: the next read takes the next slot
    assert t.status().tx_slots == 6
    assert t.preload_tx(b"\x77") == 9
    ep.i2c_write(t.fn, b"\x01")                                     # mode 3 takes no writes: an error
    ep.i2c_write(t.fn, b"")                                         # address only: nothing
    assert t.status().errors == 1
    t.reset()
    assert i2c_status(t) == (1, 3, False, 0, 0, 0, 0)
    assert t.preload_tx(b"\x01") == 1                               # the count starts again
    t.configure(0x42, t.MODE_FIXED_RX)
    with pytest.raises(host.Unavailable):
        t.preload_tx(b"\x01")                                       # mode 3 only
    assert t.status().tx_slots == 0


def test_i2c_stretch_and_plan_release(i2c):
    ep, t, _ = i2c
    t.stretch(50)                                                   # state 0 too
    assert ep.i2c[t.fn].stretch_us == 50
    t.stretch(t.max_stretch_us)
    with pytest.raises(host.Unsupported):
        t.stretch(t.max_stretch_us + 1)
    assert ep.i2c[t.fn].stretch_us == t.max_stretch_us
    t.configure(0x42, t.MODE_FIXED_RX)
    t.reset()
    assert ep.i2c[t.fn].stretch_us == t.max_stretch_us              # configure and reset keep it
    t.arm_rx(1)
    core.plan_release(t.host, [t.fn])
    assert i2c_status(t) == (0, 0, False, 0, 0, 0, 0)               # the target goes with its plan
    assert ep.i2c[t.fn].stretch_us == 0                             # and the stretch with it
    with pytest.raises(host.Unavailable):
        t.read_rx()
    assert not ep.i2c_write(t.fn, b"\x01")
    with pytest.raises(host.Unavailable):
        t.configure(0x42, t.MODE_FIXED_RX)


def test_i2c_status_is_lock_free(i2c):
    ep, t, _ = i2c
    other = fixture.I2cTarget(host.Host(ep.handle, rng=random.Random(9)))
    assert other.status().state == 0


# ---- spi-target ---------------------------------------------------------------------------------------------------

@pytest.fixture
def spi():
    ep, hst, clock = bench(fake.p4_x035())
    t = fixture.SpiTarget(hst)
    assert t.fn == P4_SPI
    core.plan_apply(hst, t.assignments(20, 21, 22, 23))
    return ep, t, clock


def test_spi_channel_groups_match_exactly():
    ep, hst, _ = bench(fake.esp32_v003())
    t = fixture.SpiTarget(hst)
    assert t.fn == V003_SPI
    with pytest.raises(host.Unsupported):
        core.plan_apply(hst, t.assignments(18, 19, 27, 4))          # a mix of the two groups
    with pytest.raises(host.Rejected) as e:
        core.plan_apply(hst, [(t.fn, 1, 18), (t.fn, 2, 19)])        # roles 3 and 4 missing: malformed first
    assert detail(e) == m.MALFORMED
    core.plan_apply(hst, t.assignments(14, 13, 27, 26))             # group 2
    with pytest.raises(host.Unsupported):
        t.configure(0, t.LSB_FIRST)                                 # features bit0 not declared here
    t.configure(3, t.MSB_FIRST)
    assert spi_status(t) == (1, 3, 0, False, 0, 0, 0)


def test_spi_refusals_in_order(spi):
    ep, t, _ = spi
    core.plan_release(t.host, [t.fn])
    with pytest.raises(host.Unavailable) as e:
        t.configure(0, 0)                                           # before the plan
    assert e.value.cause == "wrong_state"
    for mode, order in ((4, 0), (0, 2)):
        with pytest.raises(host.Rejected) as e:
            t.configure(mode, order)
        assert detail(e) == m.MALFORMED
    with pytest.raises(host.Rejected) as e:
        t.arm(0)
    assert detail(e) == m.MALFORMED
    with pytest.raises(host.Rejected) as e:
        t.arm(1, b"\x01\x02")                                       # count over length
    assert detail(e) == m.MALFORMED
    with pytest.raises(host.Unsupported):
        t.arm(65)                                                   # over max_length 64
    with pytest.raises(host.Unavailable):
        t.arm(4)                                                    # not configured
    with pytest.raises(host.Unavailable):
        t.reset()
    with pytest.raises(host.Unavailable) as e:
        t.read_rx()                                                 # state 0
    assert e.value.cause == "wrong_state"
    assert spi_status(t) == (0, 0, 0, False, 0, 0, 0)


def test_spi_plan_holds_each_role_once():
    ep, hst, _ = bench(fake.p4_x035())
    t = fixture.SpiTarget(hst)
    for bad in (t.assignments(20, 21, 22, 23)[:3],                  # CS missing
                t.assignments(20, 21, 22, 23) + [(t.fn, 4, 24)],    # CS twice
                t.assignments(20, 21, 22, 22)):                     # MISO and CS on one channel
        with pytest.raises(host.Rejected) as e:
            core.plan_apply(hst, bad)
        assert detail(e) == m.MALFORMED
    assert not any(a[0] == t.fn for a in ep.plan)
    assert (t.max_length, t.max_clock_hz, t.features, t.queue_depth) == (64, 3_000_000, t.FEATURE_LSB_FIRST, 8)


def test_spi_armed_consumed_unarmed_and_overflow(spi):
    ep, t, clock = spi
    t.configure(1, t.LSB_FIRST)
    assert spi_status(t) == (1, 1, 1, False, 0, 0, 0)
    assert ep.spi_transfer(t.fn, b"\x11\x22") == b"\x00\x00"        # not armed: MISO 0, MOSI dropped
    assert spi_status(t) == (1, 1, 1, False, 0, 1, 1)               # transactions and errors both count
    t.arm(4, b"\xa5\x5a")
    with pytest.raises(host.Unavailable) as e:
        t.arm(4)                                                    # one at a time
    assert e.value.cause == "wrong_state"
    assert t.status().armed
    clock.ms = 7
    assert ep.spi_transfer(t.fn, b"\x01\x02\x03") == b"\xa5\x5a\x00"   # MISO 0 after tx
    assert spi_status(t) == (1, 1, 1, False, 1, 2, 1)
    assert t.read_rx() == (0, 24, b"\x01\x02\x03") and t.last_ns == 7_000_000
    assert t.read_rx() == (0, 0, b"") and t.last_ns is None
    t.arm(2)
    assert ep.spi_transfer(t.fn, b"", bits=0) == b""                # CS without SCK: no transfer, still armed
    assert spi_status(t) == (1, 1, 1, True, 0, 2, 1)
    ep.spi_transfer(t.fn, b"\x01\x02\x03\x04", bits=30)             # past the length: kept to it, an error
    assert t.read_rx() == (0, 30, b"\x01\x02")
    assert t.status().errors == 2
    t.arm(2)
    ep.spi_transfer(t.fn, b"\x01\x02", bits=12)                     # 12 bits: 2 bytes, within the length
    assert t.read_rx() == (0, 12, b"\x01\x02") and t.status().errors == 2
    ep.spi_transfer(t.fn, b"", bits=0)                              # not armed, 0 bits: nothing counts
    assert spi_status(t) == (1, 1, 1, False, 0, 4, 2)
    for k in range(9):                                              # queue_depth 8: the 9th is dropped
        t.arm(1)
        ep.spi_transfer(t.fn, bytes([k]))
    assert spi_status(t) == (1, 1, 1, False, 8, 13, 3)
    assert t.read_rx() == (7, 8, b"\x00")
    t.reset()
    assert spi_status(t) == (1, 1, 1, False, 0, 0, 0)               # mode and bit order kept


def test_spi_release_ends_the_target(spi):
    ep, t, _ = spi
    t.configure(0, 0)
    t.arm(4)
    core.plan_release(t.host, [t.fn])
    assert spi_status(t) == (0, 0, 0, False, 0, 0, 0)
    assert ep.spi_transfer(t.fn, b"\x01") == b"\x00" and t.status().transactions == 0
