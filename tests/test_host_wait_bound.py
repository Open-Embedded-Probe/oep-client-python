"""core §4.4 (oep-spec a09fa45): the wait for an answer starts from the whole request's processing bound - max_op_ms,
fn 0's quick ops min(max_op_ms, core_quick_op_max_ms), core_quick_op_max_ms while max_op_ms is unknown - never from an
argument's wait alone; a host reads max_op_ms before a request that changes state."""

import struct

from oep_client import catalog, endpoint, host as h, link, message as m, registry as reg, virtual_bench


class Clock:
    t = 0

    def __call__(self):
        return self.t


def bench(max_op_ms=None):
    probe = virtual_bench.esp32_v003()
    if max_op_ms:                                                      # fn 0's describe max_op_ms (core §7.5)
        tag = reg.CORE.tlv["describe"]["max_op_ms"]
        probe.offered = [virtual_bench.Offered(o.fn, o.instance, o.name, tuple(t for t in o.tlvs if t[0] != tag)
                                               + (catalog.u32(tag, max_op_ms),), o.revision, inner=o.inner)
                         if o.fn == 0 else o for o in probe.offered]
    ep = endpoint.Endpoint(probe, Clock())
    seen = []

    def send(b):
        seen.append(hst.expect_ms)
        return ep.handle(b, 0)
    hst = h.Host(send)
    return ep, hst, seen


def test_the_bound_by_request():
    ep, hst, seen = bench(max_op_ms=5000)
    assert hst.max_op_ms() is None and hst.op_bound_ms(0, m.OP_CONFIRM) == hst.op_bound_ms(4, 0x10) == 1000
    hst.confirm()
    assert seen[-1] == 1000                                             # quick bound before max_op_ms is known
    hst.open(3000)                                                     # reads fn 0's describe first
    assert hst.max_op_ms() == 5000
    assert hst.op_bound_ms(0, reg.CORE.op["clock"]) == 1000 and hst.op_bound_ms(0, m.OP_OPEN) == 5000
    assert hst.op_bound_ms(4, 0x10) == 5000
    hst.keepalive()
    assert seen[-1] == 1000
    hst.call(4, 0x11, struct.pack("<B", 0) + b"")                      # gpio read: max_op_ms
    assert seen[-1] == 5000
    with hst.expecting(9000):                                          # an argument longer than the bound: kept
        hst.keepalive()
    assert seen[-1] == 9000


def test_a_short_max_op_ms_shortens_the_quick_bound():
    ep, hst, seen = bench(max_op_ms=300)
    hst.open(3000)
    hst.keepalive()
    assert seen[-1] == 300 and hst.op_bound_ms(0, m.OP_OPEN) == 300


def test_open_reads_max_op_ms_before_it_goes_out():
    ep, hst, _ = bench()
    hst.open(3000)
    ops = [(r.fn, r.op) for r in ep.requests]
    assert ops.index((0, m.OP_DESCRIBE)) < ops.index((0, m.OP_OPEN))


def test_the_probing_confirm_waits_two_seconds_and_the_transfer():
    assert link.PROBE_WAIT_S == (reg.TIMING["core_quick_op_max_ms"] + reg.TIMING["host_wait_add_ms"]) / 1000 == 2.0
