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
