"""oep-spec 59dd028 against the fake and the client: an attach that joins an existing connection keeps the connection's
current setting for every setting TLV it does not carry - idle_clock absent on a join keeps the current rest, "absent
= high" is for a new connection only; max_speed is always carried and a join only lowers the speed - and a scan never
changes a live connection's settings (debug §1, §3; probe-config §1.1)."""

import struct

from oep_client import endpoint, fake, host as h, message as m, riscv

from test_fake_spec import SPEED, Clock, Host, slot_item

HIGH, LOW = m.tlv(0x04, b"\x00", critical=True), m.tlv(0x04, b"\x01", critical=True)


def bench():
    ep = endpoint.Endpoint(fake.p4_bench(), Clock())
    hv = Host(ep)
    assert hv.open().succeeded
    return ep, hv


def pins(pair):
    return m.tlv(0x03, struct.pack("<HH", *pair), critical=True)


def speed(hz):
    return m.tlv(0x01, struct.pack("<I", hz), critical=True)


def attach(hv, pair, *tlvs, max_speed=SPEED):
    r = hv.raw(1, 0x02, b"\x01" + max_speed + pins(pair) + b"".join(tlvs))
    assert r.succeeded, r.describe()
    cid, _, flags, hz = struct.unpack_from("<HIBI", r.payload)
    return cid, flags, hz


def test_a_join_without_idle_clock_keeps_the_rest_low():
    ep, hv = bench()
    pair = ep.pairs[1][0]
    cid, flags, _ = attach(hv, pair, LOW)
    assert not flags & 0x02 and ep.conns[cid].idle_clock == 1
    again, flags, _ = attach(hv, pair)                                 # joins; no idle_clock TLV
    assert again == cid and flags & 0x02
    assert ep.conns[cid].idle_clock == 1                               # not the absent value's high: the current rest


def test_a_join_carrying_idle_clock_high_changes_the_rest():
    ep, hv = bench()
    pair = ep.pairs[1][0]
    cid, _, _ = attach(hv, pair, LOW)
    attach(hv, pair, HIGH)
    assert ep.conns[cid].idle_clock == 0
    attach(hv, pair, LOW)
    assert ep.conns[cid].idle_clock == 1


def test_a_new_connection_without_idle_clock_rests_high():
    ep, hv = bench()
    pair = ep.pairs[1][0]
    cid, _, _ = attach(hv, pair, LOW)
    assert hv.raw(1, 0x03, struct.pack("<H", cid)).succeeded         # the last user: the connection closes
    assert ep._conn_at(1, pair) is None
    cid, flags, _ = attach(hv, pair)                                   # a new connection: absent = high
    assert not flags & 0x02 and ep.conns[cid].idle_clock == 0


def test_a_join_without_idle_clock_keeps_a_slots_low_rest():
    """The bench failure: an at-boot slot whose target rests low, and a tool that only joins its connection."""
    ep, hv = bench()
    pair = ep.pairs[1][1]
    hv.ok(6, 0x02, slot_item(0, 1, pair, name="l103", max_speed=1_000_000, idle=1))
    cid = ep._conn_at(1, pair)
    assert (ep.conns[cid].speed, ep.conns[cid].idle_clock) == (1_000_000, 1)
    again, flags, hz = attach(hv, pair)                                # max_speed 4 MHz, no idle_clock
    assert again == cid and flags & 0x02
    assert (ep.conns[cid].speed, ep.conns[cid].idle_clock, hz) == (1_000_000, 1, 1_000_000)   # a join never raises


def test_a_join_only_lowers_the_speed():
    ep, hv = bench()
    pair = ep.pairs[1][0]
    cid, _, hz = attach(hv, pair, max_speed=speed(1_000_000))
    assert hz == 1_000_000
    _, _, hz = attach(hv, pair, max_speed=speed(4_000_000))
    assert hz == 1_000_000 and ep.conns[cid].speed == 1_000_000
    _, _, hz = attach(hv, pair, max_speed=speed(500_000))
    assert hz == 500_000 and ep.conns[cid].speed == 500_000


def test_a_scan_over_a_live_pair_leaves_its_settings():
    ep, hv = bench()
    pair = ep.pairs[1][0]
    cid, _, _ = attach(hv, pair, LOW, max_speed=speed(1_000_000))
    for tlvs in (HIGH + speed(4_000_000), HIGH + speed(100_000), b""):
        r = hv.raw(1, 0x01, b"\x01" + struct.pack("<HH", *pair) + tlvs)
        assert r.succeeded and r.payload[:2] == b"\x01\x01"            # tried 1, found 1: read over the connection
        assert (ep.conns[cid].speed, ep.conns[cid].idle_clock) == (1_000_000, 1)
    r = hv.raw(1, 0x01, b"\x00" + HIGH + speed(4_000_000))             # count 0 (the live pair among them)
    assert r.succeeded and (ep.conns[cid].speed, ep.conns[cid].idle_clock) == (1_000_000, 1)


# ---- the client: Wire.attach carries idle_clock when it is given, high included ------------------------------------

def client():
    ep = endpoint.Endpoint(fake.p4_bench(), Clock())
    hst = h.Host(lambda b: ep.handle(b, 1))
    hst.open(3000)
    return ep, riscv.Wire(hst, "oep.wire.rvswd")


def test_the_client_sends_idle_clock_high_explicitly():
    wire = client()[1]
    assert wire.attach_body(True, 1_000_000, None, "high").endswith(HIGH)
    assert wire.attach_body(True, 1_000_000, None, "low").endswith(LOW)
    assert wire.attach_body(True, 1_000_000) == b"\x01" + speed(1_000_000)   # None: no idle_clock TLV


def test_the_client_joining_keeps_or_sets_the_rest_as_asked():
    ep, wire = client()
    pair = ep.pairs[1][0]
    conn, _ = wire.attach(pins=pair, idle_clock="low")
    wire.attach(pins=pair)                                             # a join that does not carry it
    assert wire.existing and ep.conns[conn].idle_clock == 1
    wire.attach(pins=pair, idle_clock="high")                          # the slot's known high, sent explicitly
    assert wire.existing and ep.conns[conn].idle_clock == 0
