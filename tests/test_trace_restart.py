"""oep.probe.restart (oep-spec a09fa45 interfaces/oep-if-restart.ja.md): the rules tests/test_restart.py and the
restart checker leave to the trace - at most one oep.probe.restart listed, no notification once the answer is out,
the target left as it was, and the serial port back at its boot speed after the restart."""

import struct

import pytest

from oep_client import core, endpoint, virtual_bench, host as h, message as m, registry as reg

from test_port_speed import Port, committed, opened
from test_virtual_bench_capture_requests import configured

RESTART = reg.PROBE_RESTART.op["restart"]


class Clock:
    t = 0

    def __call__(self):
        return self.t


@pytest.mark.parametrize("name", sorted(virtual_bench.PROFILES))
def test_a_probe_lists_oep_probe_restart_at_most_once(name):
    """restart preamble: the interface is optional, and a probe lists at most one oep.probe.restart."""
    ep = endpoint.Endpoint(virtual_bench.PROFILES[name](), Clock())
    hst = h.Host(lambda b: ep.handle(b, 0))
    found = core.find_all(hst, virtual_bench.RESTART)
    assert len(found) <= 1 and found == ([ep.restart_fn] if ep.restart_fn is not None else [])


def test_no_notification_goes_out_once_the_restart_is_answered():
    """restart §2 (after the answer): no request on any route is answered and no notification is sent until the probe
    has restarted; the same subscribed capture without a restart does send its events."""
    ep, hst, track, clock = configured(False)
    track.subscribe()
    track.start()
    clock.t += 1
    out = m.Result.unpack(ep.handle(m.Request(hst.next_corr(), ep.restart_fn, RESTART, b"", hst.session).pack(), 0))
    assert out.succeeded and out.payload == b"" and ep.restarting and ep.reboots == 0
    assert ep.pushes() == []                                            # the events wait behind the restart: never sent
    assert ep.handle(m.Request(hst.next_corr(), 0, m.OP_LOCK_STATE, b"").pack(), 1) is None   # another route: no answer
    ep.after_answer()
    assert ep.reboots == 1 and ep.pushes() == [] and not ep.subscribed
    twin, _, track, clock = configured(False)                           # the control: no restart, the events go out
    track.subscribe()
    track.start()
    clock.t += 1
    assert any(f[0] == m.ROLE_EVENT for f in twin.pushes())


def test_the_restart_leaves_the_target_as_it_was():
    """restart §2 (target): the probe's restart does not reset or resume the target on purpose - a halted hart stays
    halted where it was, havereset is not raised."""
    ep = endpoint.Endpoint(virtual_bench.p4_x035(), Clock())
    hst = h.Host(lambda b: ep.handle(b, 1))
    hst.open(3000)
    tg = ep.target
    tg.halted, tg.dpc, tg.havereset = True, 0x1234, False
    hst.request(ep.restart_fn, RESTART)
    assert ep.reboots == 1 and ep.target is tg
    assert (tg.halted, tg.dpc, tg.havereset) == (True, 0x1234, False)


def test_after_the_restart_the_serial_port_runs_at_its_boot_speed():
    """restart §2 (after the restart): as at power-on - the UART bridge at its boot speed (transports §4), a raised
    rate gone with the old boot; the answer itself still went out at the raised rate."""
    p = Port()
    sid = opened(p)
    committed(p, sid)
    assert p.ep.port_baud(0) == 500000
    before = p.ep.boot_id
    r = p.send(p.ep.restart_fn, RESTART, b"", sid)
    assert r is not None and r.succeeded and r.payload == b""
    assert p.ep.reboots == 1 and p.ep.boot_id != before
    assert p.ep.port_baud(0) == 115200 and p.ep.speed_state == "base" and p.ep.speed_port is None
    assert p.send(0, m.OP_CONFIRM, m.CONFIRM_REQUEST + struct.pack("<BB", 1, 1)).succeeded   # at the boot speed


def test_the_interface_row_lists_oep_probe_restart_at_revision_1():
    """restart preamble (the interface row): a probe offering it lists oep.probe.restart by that name at revision 1."""
    ep = endpoint.Endpoint(virtual_bench.p4_x035(), Clock())
    hst = h.Host(lambda b: ep.handle(b, 0))
    entries = [e for e in core.list_entries(hst) if e.name == "oep.probe.restart"]
    assert [(e.fn, e.revision) for e in entries] == [(ep.restart_fn, 1)] and reg.PROBE_RESTART.revision == 1


def test_nothing_of_the_old_boot_is_left_on_the_bench():
    """restart §2.5 (after the restart, core §9's restart row): runtime resources, subscriptions and the resend state
    of the old boot are gone - the connection, the plan and the gpio drive (the pin back at its idle), the uart stream
    with its configure, bytes and position, the remembered answers; a request naming the old connection is
    no_resource in the new boot."""
    from test_virtual_bench_spec import Host as Raw
    ep = endpoint.Endpoint(virtual_bench.p4_bench(), Clock())
    hv = Raw(ep)
    assert hv.open().succeeded
    attach = reg.WIRE_RVSWD.op["attach"]
    pair = ep.pairs[1][0]
    r = hv.raw(1, attach, struct.pack("<BHHI", 1, *pair, 4_000_000))
    assert r.succeeded
    cid = struct.unpack_from("<I", r.payload)[0]
    gpio, uart = ep.fns["oep.fixture.gpio"], ep.fns["oep.fixture.uart"]
    assign = lambda *rows: b"".join(m.tlv(0x10, struct.pack("<HBH", *row), critical=True) for row in rows)  # noqa: E731
    hv.ok(hv.plan_fn, reg.PROBE_PLAN.op["plan_apply"], assign((gpio, 1, 30), (uart, 1, 20)))
    hv.ok(gpio, 0x10, bytes([1]) + struct.pack("<HB", 30, 4))
    hv.ok(uart, 0x10, struct.pack("<I", 115200))
    ep.uart_rx(uart, b"old boot")
    assert ep.conns and ep.resources and ep.resend and ep.plan and ep.pin_state(30) == "gpio 4"
    idle = endpoint.Endpoint(virtual_bench.p4_bench(), Clock()).pin_state(30)
    r = hv.raw(ep.restart_fn, RESTART)
    assert r.succeeded and r.payload == b""
    ep.after_answer()
    assert ep.reboots == 1
    assert (ep.conns, ep.resources, len(ep.resend), ep.plan, ep.gpio_modes, ep.uarts, ep.uart_baud) == (
        {}, {}, 0, set(), {}, {}, {})
    assert ep.pin_state(30) == idle and ep.holder is None and ep.subscribed == {}
    hv.session = 0x52
    assert hv.open().succeeded
    r = hv.raw(1, reg.WIRE_RVSWD.op["detach"], struct.pack("<I", cid))
    assert r.detail == m.NO_RESOURCE                                       # the old boot's number names nothing
    hv.ok(hv.plan_fn, reg.PROBE_PLAN.op["plan_apply"], assign((uart, 1, 20)))
    hv.ok(uart, 0x10, struct.pack("<I", 115200))
    r = hv.raw(uart, 0x11, struct.pack("<BQH", 1, 0, 64), session=False)
    assert r.payload == struct.pack("<QBH", 0, 0, 0)                       # the stream starts afresh: no old bytes
