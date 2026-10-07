"""The virtual bench's oep.fixture.i2c-target / spi-target (oep-spec oep-if-fixture §3 / §4) through the client classes: the
plan roles, configure / arm / preload / read_rx / status, the refusals in core §4.3's order, and the bus side
by the endpoint's test hooks (i2c_write / i2c_read / spi_transfer: the virtual bench has no bus controller of its own)."""

import random
import struct

import pytest

from oep_client import core, endpoint, virtual_bench, fixture, host, message as m, registry as reg

P4_I2C, P4_SPI = 8, 9                      # p4_x035: queue_depth 8, max_length 128 / 64, i2c stretch, spi features 0b1
V003_I2C, V003_SPI = 7, 8                  # esp32_v003: queue_depth 4, max_length 16 / 32, no stretch, spi 0 (groups)


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
    return st.state, st.queued, st.rx_frames, st.tx_slots, st.errors


def spi_status(t):
    st = t.status()
    return st.state, st.mode, st.bit_order, st.armed, st.queued, st.transactions, st.errors


# ---- i2c-target ---------------------------------------------------------------------------------------------------

@pytest.fixture
def i2c():
    ep, hst, clock = bench(virtual_bench.p4_x035())
    t = fixture.I2cTarget(hst)
    assert t.fn == P4_I2C
    core.plan_apply(hst, t.assignments(20, 21))
    return ep, t, clock


def test_i2c_plan_roles_and_channels():
    ep, hst, _ = bench(virtual_bench.p4_x035())
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
    """fixture §3: configure is address(u8) alone; > 0x7F malformed, I2C's reserved addresses unsupported (fixed
    part), both before the missing plan (core §4.3: every check before any change)."""
    ep, hst, _ = bench(virtual_bench.esp32_v003())
    t = fixture.I2cTarget(hst)
    assert t.fn == V003_I2C
    with pytest.raises(host.Unavailable) as e:
        t.configure(0x42)                                           # before the plan
    assert e.value.cause == "wrong_state"
    with pytest.raises(host.Rejected) as e:
        t.configure(0x80)
    assert detail(e) == m.MALFORMED
    for address in (0x00, 0x07, 0x78, 0x7F):                        # the client refuses them before sending ...
        with pytest.raises(ValueError):
            t.configure(address)
        with pytest.raises(host.Unsupported) as e:                  # ... and the probe answers unsupported (0x00)
            hst.request(t.fn, t.CONFIGURE, bytes([address]))
        assert e.value.tag is None
    assert i2c_status(t) == (0, 0, 0, 0, 0)
    with pytest.raises(host.Rejected) as e:
        t.stretch(10)                                               # stretch not in this probe's ops
    assert detail(e) == m.UNKNOWN_OPERATION
    with pytest.raises(host.Rejected) as e:
        t.preload_tx(b"")
    assert detail(e) == m.MALFORMED
    with pytest.raises(host.Unsupported):
        t.preload_tx(bytes(17))                                     # over max_length 16, before the state
    with pytest.raises(host.Unavailable) as e:
        t.preload_tx(b"\x01")                                       # state 0
    assert e.value.cause == "wrong_state"
    with pytest.raises(host.Unavailable) as e:
        t.read_rx()                                                 # state 0
    assert e.value.cause == "wrong_state"
    assert ep.i2c_write(t.fn, b"\x01") is False and ep.i2c_read(t.fn, 1) is None   # state 0: no ACK


@pytest.mark.parametrize("probe, sda, scl", [(virtual_bench.p4_x035, 20, 21), (virtual_bench.esp32_v003, 25, 26)])
def test_i2c_arm_rx_and_reset_are_gone(probe, sda, scl):
    """fixture §3: one form - ops 0x02 (was arm_rx) and 0x06 (was reset) are not i2c-target ops: unknown_operation,
    in any state; the describe's ops do not list them."""
    ep, hst, _ = bench(probe())
    t = fixture.I2cTarget(hst)
    assert not {0x02, 0x06} & set(reg.FIXTURE_I2C_TARGET.op.values())
    assert not {0x02, 0x06} & t.ops()
    for configured in (False, True):
        if configured:
            core.plan_apply(hst, t.assignments(sda, scl))
            t.configure(0x42)
        for op in (0x02, 0x06):
            for body in (b"", struct.pack("<H", 4)):
                with pytest.raises(host.Rejected) as e:
                    hst.request(t.fn, op, body)
                assert detail(e) == m.UNKNOWN_OPERATION
    assert not hasattr(t, "arm_rx") and not hasattr(t, "reset")


def test_i2c_declarations():
    _, hst, _ = bench(virtual_bench.p4_x035())
    t = fixture.I2cTarget(hst)
    assert (t.max_length, t.max_clock_hz, t.features, t.queue_depth, t.max_stretch_us) == (128, 1_000_000, 0, 8,
                                                                                           100_000)
    assert t.offers(t.STRETCH) and not t.internal_pullups          # stretch: an optional op, in the ops tag (§3)
    _, hst, _ = bench(virtual_bench.esp32_v003())
    t = fixture.I2cTarget(hst)
    assert (t.max_length, t.max_clock_hz, t.features, t.queue_depth, t.max_stretch_us) == (16, 100_000, 0, 4, None)
    assert not t.offers(t.STRETCH) and t.offers(t.CONFIGURE) and t.offers(t.PRELOAD_TX)
    assert "pullup_ohms" not in reg.FIXTURE_I2C_TARGET.tlv["describe"]   # no pullup_ohms (§3: features bit2 only)
    ep, hst, _ = bench(virtual_bench.with_i2c_pullups(virtual_bench.p4_x035()))
    t = fixture.I2cTarget(hst)
    assert t.internal_pullups and t.features == t.FEATURE_INTERNAL_PULLUPS
    assert not [tag for tag, _ in core.describe(hst, t.fn) if tag & 0x7F == 0x42]


def test_i2c_writes_are_frames_cut_at_max_length_and_overflow(i2c):
    """fixture §3: a controller write with data is one frame; bytes past max_length are cut (the frame keeps
    max_length bytes) and errors + 1; with queue_depth frames queued the next one is dropped, errors + 1 (not in
    rx_frames); a write both over max_length and dropped counts 1 (at most 1 a write, oep-spec 4a4631a); an
    address-only write counts nothing; another address is not ACKed."""
    ep, t, clock = i2c
    t.configure(0x42)
    assert i2c_status(t) == (1, 0, 0, 0, 0)
    assert not ep.i2c_write(t.fn, b"\x01", 0x43)                    # another address: not ACKed, not counted
    assert ep.i2c_write(t.fn, b"", 0x42)                            # address only: ACKed, counts nothing
    assert i2c_status(t) == (1, 0, 0, 0, 0)
    clock.ms = 5
    assert ep.i2c_write(t.fn, b"\x10\x20\x30\x40", 0x42)            # any length: one frame each
    clock.ms = 6
    ep.i2c_write(t.fn, b"\x50")
    ep.i2c_write(t.fn, bytes(range(130)))                           # 2 past max_length 128: cut, an error
    assert i2c_status(t) == (1, 3, 3, 0, 1)
    assert t.read_rx() == (2, b"\x10\x20\x30\x40") and t.last_ns == 5_000_000
    assert t.read_rx() == (1, b"\x50") and t.last_ns == 6_000_000
    assert t.read_rx() == (0, bytes(range(128)))
    assert t.read_rx() == (0, b"") and t.last_ns is None            # nothing left: count 0
    ep.i2c_write(t.fn, bytes(128))                                  # exactly max_length: no error
    assert t.status().errors == 1
    t.read_rx()
    for k in range(9):                                              # queue_depth 8: the 9th frame is dropped
        ep.i2c_write(t.fn, bytes([k]))
    assert i2c_status(t) == (1, 8, 12, 0, 2)                        # the dropped frame is not in rx_frames
    assert t.read_rx() == (7, b"\x00")                              # the oldest; the newest one went
    ep.i2c_write(t.fn, b"\x09")                                     # the queue full again
    ep.i2c_write(t.fn, bytes(130))                                  # over max_length and dropped: errors + 1, not 2
    assert t.status().errors == 3
    while t.read_rx()[1]:
        pass
    assert ep.i2c_read(t.fn, 2) == b"\xff\xff"                      # no preload slot: 0xFF
    t.configure(0x42)
    assert i2c_status(t) == (1, 0, 0, 0, 0)                         # configure makes the target anew
    t.configure(0x21)
    assert not ep.i2c_write(t.fn, b"\x01", 0x42) and ep.i2c_write(t.fn, b"\x01", 0x21)


def test_i2c_preload_slots_answer_reads(i2c):
    """fixture §3: reads are answered from the preload_tx slots in order (one slot a read, cut or 0xFF past it; the
    next read takes the next slot), else 0xFF; at most queue_depth unread (then unavailable cause 2); preload_tx
    answers nothing. Writes keep being frames while slots wait (one form)."""
    ep, t, _ = i2c
    with pytest.raises(host.Unavailable):
        t.preload_tx(b"\x01")                                       # not configured
    t.configure(0x42)
    assert ep.i2c_read(t.fn, 1) == b"\xff"
    with pytest.raises(host.Rejected) as e:
        t.preload_tx(b"")
    assert detail(e) == m.MALFORMED
    with pytest.raises(host.Unsupported):
        t.preload_tx(bytes(129))                                    # over max_length 128
    assert [t.preload_tx(bytes([0xA0 + i, i])) for i in range(8)] == [None] * 8
    with pytest.raises(host.Unavailable) as e:
        t.preload_tx(b"\x09")                                       # queue_depth slots unread
    assert e.value.cause == "limit"
    assert i2c_status(t) == (1, 0, 0, 8, 0)
    assert ep.i2c_read(t.fn, 3) == b"\xa0\x00\xff"                  # past the slot: 0xFF
    assert ep.i2c_read(t.fn, 1) == b"\xa1"                          # shorter: the next read takes the next slot
    assert t.status().tx_slots == 6
    t.preload_tx(b"\x77")
    ep.i2c_write(t.fn, b"\x01")                                     # a write is a frame, slots or not
    assert i2c_status(t) == (1, 1, 1, 7, 0)
    for k in range(2, 8):
        assert ep.i2c_read(t.fn, 2) == bytes([0xA0 + k, k])
    assert ep.i2c_read(t.fn, 1) == b"\x77" and ep.i2c_read(t.fn, 1) == b"\xff"   # the slots used up: 0xFF
    t.preload_tx(b"\x01")
    t.configure(0x42)
    assert t.status().tx_slots == 0 and ep.i2c_read(t.fn, 1) == b"\xff"   # configure empties the slots


def test_i2c_preload_tx_answers_nothing():
    """fixture §3: preload_tx's answer has no payload (no slot number)."""
    ep, hst, _ = bench(virtual_bench.p4_x035())
    t = fixture.I2cTarget(hst)
    core.plan_apply(hst, t.assignments(20, 21))
    t.configure(0x42)
    assert hst.request(t.fn, t.PRELOAD_TX, struct.pack("<H", 1) + b"\x01").payload == b""


def test_i2c_stretch_and_plan_release(i2c):
    ep, t, _ = i2c
    t.stretch(50)                                                   # state 0 too
    assert ep.i2c[t.fn].stretch_us == 50
    t.stretch(t.max_stretch_us)
    with pytest.raises(host.Unsupported):
        t.stretch(t.max_stretch_us + 1)
    assert ep.i2c[t.fn].stretch_us == t.max_stretch_us
    t.configure(0x42)
    assert ep.i2c[t.fn].stretch_us == t.max_stretch_us              # configure keeps it
    ep.i2c_write(t.fn, b"\x01")
    t.preload_tx(b"\x02")
    core.plan_release(t.host, [t.fn])
    assert i2c_status(t) == (0, 0, 0, 0, 0)                         # the target goes with its plan
    assert ep.i2c[t.fn].stretch_us == 0                             # and the stretch with it
    with pytest.raises(host.Unavailable):
        t.read_rx()
    assert not ep.i2c_write(t.fn, b"\x01")
    with pytest.raises(host.Unavailable):
        t.configure(0x42)


def test_i2c_status_is_lock_free(i2c):
    ep, t, _ = i2c
    other = fixture.I2cTarget(host.Host(ep.handle, rng=random.Random(9)))
    assert other.status().state == 0


# ---- spi-target ---------------------------------------------------------------------------------------------------

@pytest.fixture
def spi():
    ep, hst, clock = bench(virtual_bench.p4_x035())
    t = fixture.SpiTarget(hst)
    assert t.fn == P4_SPI
    core.plan_apply(hst, t.assignments(20, 21, 22, 23))
    return ep, t, clock


def test_spi_channel_groups_match_exactly():
    ep, hst, _ = bench(virtual_bench.esp32_v003())
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
    with pytest.raises(host.Rejected) as e:
        t.host.request(t.fn, 0x05)                                  # fixture §4: no reset op (0x05 is no op)
    assert detail(e) == m.UNKNOWN_OPERATION
    assert not hasattr(t, "reset") and 0x05 not in t.ops()
    with pytest.raises(host.Unavailable) as e:
        t.read_rx()                                                 # state 0
    assert e.value.cause == "wrong_state"
    assert spi_status(t) == (0, 0, 0, False, 0, 0, 0)


def test_spi_plan_holds_each_role_once():
    ep, hst, _ = bench(virtual_bench.p4_x035())
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
    t.arm(1)
    ep.spi_transfer(t.fn, b"\x09")                                  # the queue full again
    t.arm(1)
    ep.spi_transfer(t.fn, b"\x01\x02\x03")                          # past the length AND the queue full: errors + 1
    assert spi_status(t) == (1, 1, 1, False, 8, 15, 4)              # at most 1 a transfer (fixture §4)
    ep.spi_transfer(t.fn, b"\x01")                                  # not armed, the queue full: once again
    assert spi_status(t) == (1, 1, 1, False, 8, 16, 5)
    t.configure(2, t.MSB_FIRST)
    assert spi_status(t) == (1, 2, 0, False, 0, 0, 0)               # configure makes the target anew (no reset op)


def test_spi_release_ends_the_target(spi):
    ep, t, _ = spi
    t.configure(0, 0)
    t.arm(4)
    core.plan_release(t.host, [t.fn])
    assert spi_status(t) == (0, 0, 0, False, 0, 0, 0)
    assert ep.spi_transfer(t.fn, b"\x01") == b"\x00" and t.status().transactions == 0
