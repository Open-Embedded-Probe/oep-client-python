"""oep-if-debug §1-§3 (oep-spec a09fa45): attach names its pins and max_speed in the fixed part and always makes a new
connection - pins a live connection holds are refused (unavailable cause 1 with the channel) and the connection is left
as it was, whatever the request carries; a scan never touches a live connection's pins; a new connection without
idle_clock rests high. (Before a09fa45 an attach joined the live connection: 59dd028's rules, gone.)"""

import struct

from oep_client import endpoint, virtual_bench, host as h, message as m, riscv

from test_virtual_bench_spec import Clock, Host

W = riscv.reg.WIRE_RVSWD.op
HIGH, LOW = m.tlv(0x04, b"\x00", critical=True), m.tlv(0x04, b"\x01", critical=True)
UNA = riscv.reg.CORE.tlv["unavailable_payload"]


def bench():
    ep = endpoint.Endpoint(virtual_bench.p4_bench(), Clock())
    hv = Host(ep)
    assert hv.open().succeeded
    return ep, hv


def fixed(pair, hz=4_000_000, method=1):
    return struct.pack("<BHHI", method, *pair, hz)


def attach(hv, pair, *tlvs, hz=4_000_000):
    r = hv.raw(1, W["attach"], fixed(pair, hz) + b"".join(tlvs))
    assert r.succeeded, r.describe()
    cid, _, flags, speed = struct.unpack_from("<IIBI", r.payload)
    return cid, flags, speed


def test_busy_pins_are_refused_and_the_connection_is_left_as_it_was():
    ep, hv = bench()
    pair = ep.pairs[1][0]
    cid, _, _ = attach(hv, pair, LOW, hz=1_000_000)
    for tlvs, hz in ((HIGH, 4_000_000), (b"", 500_000)):
        r = hv.raw(1, W["attach"], fixed(pair, hz) + tlvs)
        assert r.detail == m.UNAVAILABLE
        assert dict(m.split_tlvs(r.payload)) == {UNA["cause"]: b"\x01", UNA["channel"]: struct.pack("<H", pair[0])}
        assert (ep.conns[cid].speed, ep.conns[cid].idle_clock) == (1_000_000, 1)
    assert list(ep.conns) == [cid]


def test_a_new_connection_without_idle_clock_rests_high():
    ep, hv = bench()
    pair = ep.pairs[1][0]
    cid, _, _ = attach(hv, pair, LOW)
    assert hv.raw(1, W["detach"], struct.pack("<I", cid)).succeeded
    assert ep._conn_at(1, pair) is None
    again, _, _ = attach(hv, pair)
    assert again != cid and ep.conns[again].idle_clock == 0          # numbers are not reused (core §9)


def test_the_speed_is_at_most_max_speed():
    ep, hv = bench()
    _, _, hz = attach(hv, ep.pairs[1][0], hz=1_000_000)
    assert hz == 1_000_000


def test_a_scan_over_a_live_pair_is_refused_and_leaves_its_settings():
    ep, hv = bench()
    pair = ep.pairs[1][0]
    cid, _, _ = attach(hv, pair, LOW, hz=1_000_000)
    for tlvs in (HIGH + m.tlv(0x01, struct.pack("<I", 4_000_000), critical=True), b""):
        r = hv.raw(1, W["scan"], b"\x01" + struct.pack("<HH", *pair) + tlvs)
        assert r.detail == m.UNAVAILABLE
        assert (ep.conns[cid].speed, ep.conns[cid].idle_clock) == (1_000_000, 1)


def test_the_limit_closes_nothing():
    ep, hv = bench()                                                  # max_connections 2
    a, b, c = ep.pairs[1]
    first, _, _ = attach(hv, a)
    second, _, _ = attach(hv, b)
    r = hv.raw(1, W["attach"], fixed(c))
    assert r.detail == m.UNAVAILABLE and dict(m.split_tlvs(r.payload)) == {UNA["cause"]: b"\x02"}
    assert set(ep.conns) == {first, second}


def test_resource_ids_go_past_u16_and_are_never_reused():
    ep, hv = bench()
    ep._next_resource = 0x10000
    cid, _, _ = attach(hv, ep.pairs[1][0])
    assert cid == 0x10000
    rows = m.Reader(hv.raw(1, W["connections"], b"\x00", session=False).payload)
    assert rows.take("BB") == (0, 1) and rows.take("IHHIBB")[0] == 0x10000


# ---- the client --------------------------------------------------------------------------------------------------

def client():
    ep = endpoint.Endpoint(virtual_bench.p4_bench(), Clock())
    hst = h.Host(lambda b: ep.handle(b, 1))
    hst.open(3000)
    return ep, riscv.Wire(hst, "oep.wire.rvswd")


def test_the_client_sends_idle_clock_high_explicitly():
    ep, wire = client()
    pair = ep.pairs[1][0]
    assert wire.attach_body(True, 1_000_000, pair, "high").endswith(HIGH)
    assert wire.attach_body(True, 1_000_000, pair, "low").endswith(LOW)
    assert wire.attach_body(True, 1_000_000, pair) == fixed(pair, 1_000_000)   # None: no idle_clock TLV


def test_the_client_needs_pins_where_the_probe_allows_several_pairs():
    ep, wire = client()
    assert wire.candidate_pairs() == ep.pairs[1]
    try:
        wire.attach_body(True, 1_000_000)
    except ValueError as e:
        assert "3 pairs" in str(e)
    else:
        raise AssertionError("no pins on a probe with three pairs")


def test_the_client_reads_connections():
    ep, wire = client()
    pair = ep.pairs[1][1]
    ep.targets[(1, pair)].target_id = 0x035E0601
    conn, _ = wire.attach(pins=pair, idle_clock="low")
    (row,) = wire.connections()
    assert (row.connection, row.pins, row.target_id, row.targetsel) == (
        conn, pair, (1, struct.pack("<I", 0x035E0601)), None)
    wire.detach(conn)
    assert wire.connections() == []
