"""probe.config §1.1 passthrough and transports §4 (oep-spec df32360): a serial port carries what a fixture UART receives
outside the OEP frames while no session uses the port, the UART's fn or its RX pin; a session that sends on the port,
or takes the fn's plan, stops it for the session. A host reads past the raw bytes as noise."""

import struct
import time

import pytest

from oep_client import config, core, endpoint, host as h, link, message as m, virtual_bench, virtual_bench_serial

UART_FN, RX = 5, 13


def item(port=0, fn=UART_FN, rx=RX, baud=115200, fmt=0):
    return struct.pack("<BHB", config.ITEM["passthrough"], 10, 0)[:3] + struct.pack("<BHHIB", port, fn, rx, baud, fmt)


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
    config.ProbeConfig(hst).set([item()])
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
    config.ProbeConfig(hst).set([item()])
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
    (dict(port=7), m.UNSUPPORTED),                        # no such serial port
    (dict(fn=4), m.UNSUPPORTED),                          # not a fixture UART
    (dict(rx=23), m.UNSUPPORTED),                         # not the UART's RX candidate
    (dict(baud=0), m.MALFORMED),
    (dict(fmt=0x40), m.UNSUPPORTED),                      # a format the UART does not declare
])
def test_a_set_it_cannot_take_is_refused(bench, value, reason):
    ep, s, lk, hst = bench
    core.take(hst, 10000)
    with pytest.raises(h.Rejected) as e:
        config.ProbeConfig(hst).set([item(**value)])
    assert e.value.result.detail == reason


def test_a_disabled_rx_or_an_output_idle_on_it_is_held_by_settings(bench):
    ep, s, lk, hst = bench
    core.take(hst, 10000)
    disable = struct.pack("<BH", config.ITEM["disable"], 2) + struct.pack("<H", RX)
    with pytest.raises(h.Unavailable) as e:
        config.ProbeConfig(hst).set([disable, item()])
    assert e.value.cause == "held_by_settings"            # cause 5


def test_the_client_sets_reads_and_unsets_it(bench):
    ep, s, lk, hst = bench
    core.take(hst, 10000)
    cfg = config.ProbeConfig(hst)
    cfg.set([config.Passthrough(port=0, fn=UART_FN, rx=RX)])
    assert config.Passthrough(port=0, fn=UART_FN, rx=RX, baud=115200, format=0) in cfg.items()
    cfg.unset([("passthrough", 0)])                       # the key, the port, in unset's channel field (§1.1)
    assert not [it for it in cfg.items() if isinstance(it, config.Passthrough)]


def test_what_it_passes_is_not_kept_in_the_uarts_stream(bench):
    """§1.1: the pass-through's bytes go to the port only - never into the fixture UART's stream - and what the port
    could not take is dropped (nothing waits for a session)."""
    ep, s, lk, hst = bench
    core.take(hst, 10000)
    config.ProbeConfig(hst).set([item()])
    hst.end()
    kept = lambda: bytes(ep.uarts[UART_FN].data) if UART_FN in ep.uarts else b""
    before = kept()
    ep.uart_rx(UART_FN, b"dropped\r\n", RX)
    ep.session_ports.add(0)                               # a session takes the port before it went out
    assert s.port.output() == b"" and kept() == before
    ep.session_ports.clear()
    assert s.port.output() == b""                         # not kept for later


# ---- console_passthrough (probe.config §1.2) -------------------------------------------------------------------------

WIRE, PAIR, CONSOLE, DMSEQ = 1, (16, 0xFFFF), 3, 2


def console_item(port=0, wire=WIRE, pair=PAIR, speed=1_000_000, console=CONSOLE, mech=DMSEQ):
    return (struct.pack("<BH", config.ITEM["console_passthrough"], 14)
            + struct.pack("<BHHHIHB", port, wire, pair[0], pair[1], speed, console, mech))


def test_the_targets_console_goes_out_while_no_session_uses_it(bench):
    ep, s, lk, hst = bench
    core.take(hst, 10000)
    config.ProbeConfig(hst).set([console_item()])
    ep.target_says(b"in session\r\n")
    assert s.port.output() == b""                         # the session's port: nothing raw
    hst.end()
    ep.target_says(b"printf\r\n")
    assert s.port.output() == b"printf\r\n\x00"
    assert not ep.conns and not ep.streams                # no OEP resource: no connection, no stream (§1.2)
    assert hst.lock_state()[0] is False                   # session-less requests still answered


def test_a_session_on_the_port_closes_it_and_it_comes_back_after(bench):
    ep, s, lk, hst = bench
    core.take(hst, 10000)
    config.ProbeConfig(hst).set([console_item()])
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
    config.ProbeConfig(hst).set([console_item()])
    ep.target.present = False
    hst.end()
    assert ep.console_passthroughs() == {}
    ep.target.present = True                              # swio's wake does not reset the target: tried again
    assert ep.console_passthroughs() == {0: (WIRE, PAIR)}


@pytest.mark.parametrize("value, reason", [
    (dict(port=7), m.UNSUPPORTED), (dict(wire=4), m.UNSUPPORTED), (dict(pair=(17, 0xFFFF)), m.UNSUPPORTED),
    (dict(speed=0), m.MALFORMED), (dict(console=4), m.UNSUPPORTED), (dict(mech=9), m.UNSUPPORTED),
], ids=["port", "wire", "pair", "speed0", "console", "mechanism"])
def test_a_console_item_it_cannot_take_is_refused(bench, value, reason):
    ep, s, lk, hst = bench
    core.take(hst, 10000)
    with pytest.raises(h.Rejected) as e:
        config.ProbeConfig(hst).set([console_item(**value)])
    assert e.value.result.detail == reason


def test_one_pass_through_per_port(bench):
    ep, s, lk, hst = bench
    core.take(hst, 10000)
    with pytest.raises(h.Unavailable) as e:
        config.ProbeConfig(hst).set([item(), console_item()])
    assert e.value.cause == "held_by_settings"


def test_the_client_sets_and_reads_a_console_pass_through(bench):
    ep, s, lk, hst = bench
    core.take(hst, 10000)
    cfg = config.ProbeConfig(hst)
    want = config.ConsolePassthrough(port=0, wire_fn=WIRE, swdio=16, console_fn=CONSOLE, mechanism=DMSEQ)
    cfg.set([want])
    assert want in cfg.items()


def test_after_a_line_loss_rvswd_waits_for_the_condition_to_start_again():
    """§1.2: line loss closes it; rvswd (its wake may reset the target) is not tried again until the condition starts
    again - a session on the port and its end, or unset and set; swio tries again by itself."""
    start = time.monotonic()
    ep = endpoint.Endpoint(virtual_bench.p4_x035(), lambda: int((time.monotonic() - start) * 1000))
    hst = h.Host(ep.handle)
    core.take(hst, 10000)
    wire = core.find(hst, "oep.wire.rvswd")
    console = core.find(hst, "oep.target.console")
    (pair,) = [p for (f, p) in ep.targets if f == wire]
    cfg = config.ProbeConfig(hst)
    cfg.set([config.ConsolePassthrough(port=0, wire_fn=wire, swdio=pair[0], swclk=pair[1], console_fn=console,
                                       mechanism=DMSEQ)])
    hst.end()
    assert ep.console_passthroughs() == {0: (wire, pair)}
    ep.console_line_lost(0)
    assert ep.console_passthroughs() == {}                # not tried again by itself
    core.take(hst, 10000)
    cfg.unset([("console_passthrough", 0)])
    cfg.set([config.ConsolePassthrough(port=0, wire_fn=wire, swdio=pair[0], swclk=pair[1], console_fn=console,
                                       mechanism=DMSEQ)])
    hst.end()
    assert ep.console_passthroughs() == {0: (wire, pair)}  # unset and set: the condition started again
