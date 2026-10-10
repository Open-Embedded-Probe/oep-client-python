"""Challenge length inspection with literal split, corrupt and missing responses."""
from collections import deque
import socket

import pytest

from oep_client.conformance_tcp import TcpWire, address, frame
from oep_client.conformance_serial import WireError


class Socket:
    def __init__(self, chunks, monkeypatch):
        self.chunks, self.writes, self.now = deque(chunks), [], 0
        self.timeout = 0.01
        monkeypatch.setattr('oep_client.conformance_tcp.time.monotonic', lambda: self.now)

    def sendall(self, data):
        self.writes.append(data)

    def settimeout(self, value):
        self.timeout = value

    def recv(self, _):
        self.now += self.timeout
        if self.chunks:
            item = self.chunks.popleft()
            if isinstance(item, Exception):
                raise item
            return item
        raise socket.timeout()


RESULT = b'\x02\x01\0\x01\0'


@pytest.mark.parametrize('split', range(1, 7))
def test_every_length_response_split(monkeypatch, split):
    encoded = b'\x05\0' + RESULT
    stream = Socket([encoded[:split], encoded[split:]], monkeypatch)
    assert TcpWire(stream).send(b'request') == RESULT
    assert stream.writes == [b'\x07\0request']


@pytest.mark.parametrize('incoming', [b'\0\0' + frame(RESULT), frame(RESULT) + b'\0\0'])
def test_zero_length_is_ignored(monkeypatch, incoming):
    assert TcpWire(Socket([incoming], monkeypatch)).send(b'request') == RESULT


@pytest.mark.parametrize('incoming,error', [(frame(RESULT) * 2, 'observed 2'),
                                           (b'\x41\0', 'oversized'),
                                           (frame(RESULT) + b'\x05', 'partial')])
def test_bad_response_is_not_filtered(monkeypatch, incoming, error):
    with pytest.raises(WireError, match=error):
        TcpWire(Socket([incoming], monkeypatch)).send(b'request')


@pytest.mark.parametrize('close', [b'', ConnectionResetError()])
def test_oversize_requires_actual_close(monkeypatch, close):
    wire = TcpWire(Socket([close], monkeypatch))
    assert wire.exchange([b'\x41\0'], 0, expect_close=True) == []
    assert wire.last_exchange['closed']


@pytest.mark.parametrize('incoming', [[frame(RESULT), b''], [b'\x05', b''], []])
def test_response_before_close_partial_close_or_no_close_fails(monkeypatch, incoming):
    with pytest.raises((WireError, TimeoutError)):
        TcpWire(Socket(incoming, monkeypatch)).exchange([b'\x41\0'], 0, expect_close=True)


def test_unexpected_close_is_not_an_empty_response(monkeypatch):
    with pytest.raises(WireError, match='unexpected TCP close'):
        TcpWire(Socket([b''], monkeypatch)).send(b'request')


@pytest.mark.parametrize('value', ['tcp:unit', 'tcp://localhost', 'http://localhost:1',
                                   'tcp://u:p@localhost:1', 'tcp://localhost:1/path',
                                   'tcp://localhost:1?x', 'tcp://localhost:1#x'])
def test_tcp_requires_explicit_unambiguous_address(value):
    with pytest.raises(ValueError):
        address(value)


def test_explicit_ipv6_and_length_boundaries():
    assert address('tcp://[::1]:9000') == ('::1', 9000)
    assert frame(b'') == b'\0\0'
    assert frame(b'x' * 65535)[:2] == b'\xff\xff'
    with pytest.raises(OverflowError):
        frame(b'x' * 65536)


def test_tcp_wire_fault_stops_after_corrupt_output(monkeypatch):
    from oep_client.conformance import Checks
    from test_conformance import REG
    wire = TcpWire(Socket([b'\x41\0'], monkeypatch))
    check = Checks(wire.send, REG, 'unit')
    check.tcp_wire = wire
    check.check('tcp', 'transports', lambda: check.tcp_case('role'))
    assert check.results[-1]['status'] == 'failed' and check.abort


def test_maximum_u16_length_cannot_have_an_oversize_length_stimulus():
    from oep_client.conformance import Checks
    from test_conformance import REG
    check = Checks(lambda _: pytest.fail('sent an unrepresentable stimulus'), REG, 'unit')
    check.max_frame = 65535
    check.check('tcp', 'transports', lambda: check.tcp_case('oversized'))
    assert check.results[-1]['status'] == 'not_applicable'
    assert 'u16' in check.results[-1]['reason']


@pytest.mark.parametrize('incoming', [[], [frame(RESULT)], [b"\0\0"]])
def test_receive_only_observation_waits_full_silence_window(monkeypatch, incoming):
    stream = Socket(incoming, monkeypatch)
    wire = TcpWire(stream)
    replies = wire.exchange([], 0, silence=0.1)
    assert wire.last_exchange['silence_ms'] == 100
    assert wire.last_exchange['ended_monotonic_ns'] >= wire.last_exchange['started_monotonic_ns']
    assert stream.now >= 0.1
    assert replies == ([RESULT] if incoming == [frame(RESULT)] else [])
    assert stream.writes == []


def test_receive_only_partial_output_is_not_silence(monkeypatch):
    with pytest.raises(WireError, match='partial'):
        TcpWire(Socket([b"\x05"], monkeypatch)).exchange([], 0, silence=0.1)
