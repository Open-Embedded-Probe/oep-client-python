"""fixture §2.1 / console §2.1 pass-throughs and transports §4 (the settings, core §13): a serial port carries what a fixture UART receives
outside the OEP frames while no session uses the port, the UART's fn or its RX pin; a session that sends on the port,
or takes the fn's plan, stops it for the session. A host reads past the raw bytes as noise."""

import struct
import time

import pytest

from oep_client import core, endpoint, host as h, link, message as m, settings, virtual_bench, virtual_bench_serial


UART_FN, RX = 5, 13


UART = "oep.fixture.uart/0"
PORT = "bridge0"                                                          # the UART bridge, serial port 0 (core §13.3)


def uart_values(rx=RX, baud=115200) -> dict:
    return {f"passthrough.{PORT}.rx": f"GPIO{rx}", f"passthrough.{PORT}.baud": baud}


def raw(ep, fn, name, key, data) -> tuple:
    """One set value by the item's name in fn's list (what a host could send that the client by name would not)."""
    return (fn, ep.setting_number(name, fn), key, data)


@pytest.fixture
def bench():
    start = time.monotonic()
    ep = endpoint.Endpoint(virtual_bench.esp32_v003(), lambda: int((time.monotonic() - start) * 1000))
    s = virtual_bench_serial.VirtualSerialStream(ep, 0)
    lk = link.SerialLink.on_stream(s, "cobs", 0.3)
    lk.transport = "serial"
    hst = h.Host(lk.send)
    lk.attach_host(hst)
    return ep, s, lk, hst


def test_the_uart_goes_out_between_frames_while_no_session_uses_it(bench):
    ep, s, lk, hst = bench
    core.take(hst, 10000)
    settings.Settings(hst).set_named(UART, uart_values())
    ep.uart_rx(UART_FN, b"boot\r\n", RX)
    assert s.port.output() == b""                         # the session's port: nothing raw (transports §4)
    hst.end()
    ep.uart_rx(UART_FN, b"hello\r\n", RX)
    assert s.port.output() == b"hello\r\n\x00"            # then a delimiter: the next frame does not take them in
    for _ in range(5):                                    # session-less requests read the raw bytes past as noise
        ep.uart_rx(UART_FN, b"tick 123\r\n", RX)
        assert hst.lock_state()[0] is False
    assert lk.noise >= 1


def test_a_session_on_the_port_or_the_fns_plan_stops_it(bench):
    ep, s, lk, hst = bench
    core.take(hst, 10000)
    settings.Settings(hst).set_named(UART, uart_values())
    hst.end()
    assert ep.passthroughs() == {0: (UART_FN, RX)}
    core.take(hst, 10000)                                 # a session's request on the port
    assert ep.passthroughs() == {}
    hst.end()
    assert ep.passthroughs() == {0: (UART_FN, RX)}
    ep.session_ports.add(9)                               # (as if the session came on another transport)
    ep.plan.add((UART_FN, 1, RX))                         # the fn's plan: the session's UART wins
    assert ep.passthroughs() == {}


@pytest.mark.parametrize("value, reason", [
    (("passthrough.{}.rx", 7, b"\x0d\x00"), m.UNSUPPORTED),          # no such serial port
    (("passthrough.{}.rx", 0, b"\x17\x00"), m.UNSUPPORTED),          # not the UART's RX candidate
    (("passthrough.{}.baud", 0, b"\x00\x00\x00\x00"), m.MALFORMED),
    (("passthrough.{}.format", 0, b"\x40"), m.UNSUPPORTED),           # a format the UART does not declare
], ids=["port", "rx", "baud0", "format"])
def test_a_set_it_cannot_take_is_refused(bench, value, reason):
    ep, s, lk, hst = bench
    core.take(hst, 10000)
    with pytest.raises(h.Rejected) as e:
        settings.Settings(hst).set([raw(ep, UART_FN, *value)])
    assert e.value.result.detail == reason


def test_a_disabled_rx_is_held_by_settings(bench):
    ep, s, lk, hst = bench
    core.take(hst, 10000)
    st = settings.Settings(hst)
    st.set_named(settings.CORE_CATEGORY, {f"channel.GPIO{RX}.disable": True})
    with pytest.raises(h.Unavailable) as e:
        st.set_named(UART, uart_values())
    assert e.value.cause == "held_by_settings"            # cause 5


def test_the_client_sets_reads_and_unsets_it(bench):
    ep, s, lk, hst = bench
    core.take(hst, 10000)
    st = settings.Settings(hst)
    st.set_named(UART, uart_values())
    shown = {(c, n): v for c, n, v, o in st.show()}
    assert shown[(UART, f"passthrough.{PORT}.rx")] == f"GPIO{RX}" and shown[(UART, f"passthrough.{PORT}.baud")] == 115200
    st.unset_named(UART, [f"passthrough.{PORT}.rx", f"passthrough.{PORT}.baud"])
    assert not [n for c, n, v, o in st.show() if c == UART]
    assert ep.passthroughs() == {}


def test_what_it_passes_is_not_kept_in_the_uarts_stream(bench):
    """fixture §2.1: the pass-through's bytes go to the port only - never into the fixture UART's stream - and what
    the port could not take is dropped (nothing waits for a session)."""
    ep, s, lk, hst = bench
    core.take(hst, 10000)
    settings.Settings(hst).set_named(UART, uart_values())
    hst.end()
    kept = lambda: bytes(ep.uarts[UART_FN].data) if UART_FN in ep.uarts else b""
    before = kept()
    ep.uart_rx(UART_FN, b"dropped\r\n", RX)
    ep.session_ports.add(0)                               # a session takes the port before it went out
    assert s.port.output() == b"" and kept() == before
    ep.session_ports.clear()
    assert s.port.output() == b""                         # not kept for later


# ---- the console's pass-through (console §2.1) ------------------------------------------------------------------------

WIRE, PAIR, CONSOLE_FN = 1, (16, 0xFFFF), 3
CONSOLE = "oep.target.console/0"


def console_values(swdio=PAIR[0], mech="dmseq", wire="oep.wire.swio/0") -> dict:
    return {f"passthrough.{PORT}.wire": wire, f"passthrough.{PORT}.swdio": f"GPIO{swdio}",
            f"passthrough.{PORT}.mechanism": mech}


def test_the_targets_console_goes_out_while_no_session_uses_it(bench):
    ep, s, lk, hst = bench
    core.take(hst, 10000)
    settings.Settings(hst).set_named(CONSOLE, console_values())
    ep.target_says(b"in session\r\n")
    assert s.port.output() == b""                         # the session's port: nothing raw
    hst.end()
    ep.target_says(b"printf\r\n")
    assert s.port.output() == b"printf\r\n\x00"
    assert not ep.conns and not ep.streams                # no OEP resource: no connection, no stream (console §2.1)
    assert hst.lock_state()[0] is False                   # session-less requests still answered


def test_a_session_on_the_port_closes_it_and_it_comes_back_after(bench):
    ep, s, lk, hst = bench
    core.take(hst, 10000)
    settings.Settings(hst).set_named(CONSOLE, console_values())
    hst.end()
    assert ep.console_passthroughs() == {0: (WIRE, PAIR)}
    core.take(hst, 10000)
    assert ep.console_passthroughs() == {} and 0 not in ep.resident   # closed, the target untouched
    ep.target_says(b"x")
    hst.end()
    assert s.port.output() == b""                         # what the session's time made was not kept
    ep.target_says(b"y")
    assert s.port.output() == b"y\x00"


def test_swio_tries_again_until_the_target_answers(bench):
    ep, s, lk, hst = bench
    core.take(hst, 10000)
    settings.Settings(hst).set_named(CONSOLE, console_values())
    ep.target.present = False
    hst.end()
    assert ep.console_passthroughs() == {}
    ep.target.present = True                              # swio's wake does not reset the target: tried again
    assert ep.console_passthroughs() == {0: (WIRE, PAIR)}


def test_not_every_value_there_is_no_pass_through(bench):
    ep, s, lk, hst = bench
    core.take(hst, 10000)
    values = console_values()
    del values[f"passthrough.{PORT}.mechanism"]
    settings.Settings(hst).set_named(CONSOLE, values)
    hst.end()
    assert ep.console_passthroughs() == {}


@pytest.mark.parametrize("value, reason", [
    (("passthrough.{}.wire", 7, b"\x01\x00"), m.UNSUPPORTED),         # no such serial port
    (("passthrough.{}.wire", 0, b"\x04\x00"), m.UNSUPPORTED),         # not a wire
    (("passthrough.{}.max-speed", 0, b"\x00\x00\x00\x00"), m.MALFORMED),
    (("passthrough.{}.mechanism", 0, b"nope"), m.UNSUPPORTED),         # not in the console's mechanism list
], ids=["port", "wire", "speed0", "mechanism"])
def test_a_console_value_it_cannot_take_is_refused(bench, value, reason):
    ep, s, lk, hst = bench
    core.take(hst, 10000)
    with pytest.raises(h.Rejected) as e:
        settings.Settings(hst).set([raw(ep, CONSOLE_FN, *value)])
    assert e.value.result.detail == reason


def test_a_pair_the_wire_does_not_allow_is_refused(bench):
    ep, s, lk, hst = bench
    core.take(hst, 10000)
    with pytest.raises(h.Unsupported):
        settings.Settings(hst).set_named(CONSOLE, console_values(swdio=17))


def test_one_pass_through_per_port(bench):
    ep, s, lk, hst = bench
    core.take(hst, 10000)
    st = settings.Settings(hst)
    st.set_named(UART, uart_values())
    with pytest.raises(h.Unavailable) as e:
        st.set_named(CONSOLE, console_values())
    assert e.value.cause == "held_by_settings"


def test_after_a_line_loss_rvswd_waits_for_the_condition_to_start_again():
    """console §2.1: line loss closes it; rvswd (its wake may reset the target) is not tried again until the condition
    starts again - a session on the port and its end, or unset and set; swio tries again by itself."""
    start = time.monotonic()
    ep = endpoint.Endpoint(virtual_bench.p4_x035(), lambda: int((time.monotonic() - start) * 1000))
    hst = h.Host(ep.handle)
    core.take(hst, 10000)
    wire = core.find(hst, "oep.wire.rvswd")
    (pair,) = [p for (f, p) in ep.targets if f == wire]
    st = settings.Settings(hst)
    route = st.catalog().route[0]
    values = {f"passthrough.{route}.wire": "oep.wire.rvswd/0", f"passthrough.{route}.swdio": f"GPIO{pair[0]}",
              f"passthrough.{route}.swclk": f"GPIO{pair[1]}", f"passthrough.{route}.mechanism": "dmseq"}
    st.set_named(CONSOLE, values)
    hst.end()
    assert ep.console_passthroughs() == {0: (wire, pair)}
    ep.console_line_lost(0)
    assert ep.console_passthroughs() == {}                # not tried again by itself
    core.take(hst, 10000)
    st.unset_named(CONSOLE, [f"passthrough.{route}.wire"])
    st.set_named(CONSOLE, values)
    hst.end()
    assert ep.console_passthroughs() == {0: (wire, pair)}  # unset and set: the condition started again
