"""Independent inspector checked against shared wire vectors and injected response faults."""
from collections import deque
import json
from pathlib import Path

import pytest

from oep_client.conformance_serial import SerialWire, WireError, encode, decode, frame, unframe


def test_independent_codec_matches_shared_spec():
    vectors = json.loads((Path(__file__).parent / 'vectors/cobs.json').read_text())
    for row in vectors['encode']:
        raw = bytes.fromhex(row['data_hex'])
        assert encode(raw).hex() == row['encoded_hex'], row['name']
        assert decode(bytes.fromhex(row['encoded_hex'])) == raw
    for row in vectors['decode_also_accepts']:
        assert decode(bytes.fromhex(row['encoded_hex'])) == bytes.fromhex(row['data_hex'])
    for row in vectors['frames']:
        message = bytes.fromhex(row['message_hex'])
        assert frame(message).hex() == row['frame_hex']
        assert unframe(bytes.fromhex(row['frame_hex'])[1:-1]) == message


class Stream:
    def __init__(self, chunks, monkeypatch):
        self.chunks, self.writes = deque(chunks), []
        self.now, self.timeout = 0, 0.01
        monkeypatch.setattr('oep_client.conformance_serial.time.monotonic', lambda: self.now)

    def write(self, data):
        self.writes.append(data)
        return len(data)

    def read(self, _):
        self.now += self.timeout
        return self.chunks.popleft() if self.chunks else b''


RESULT = b'\x02\x01\0\x01\0'


@pytest.mark.parametrize('split', range(1, len(frame(RESULT))))
def test_every_response_split_is_read_without_losing_bytes(monkeypatch, split):
    wire = frame(RESULT)
    stream = Stream([wire[:split], wire[split:]], monkeypatch)
    inspector = SerialWire(stream)
    assert inspector.send(b'request') == RESULT
    assert b''.join(bytes.fromhex(x['hex']) for x in inspector.last_exchange['reads']) == wire


def test_duplicate_results_are_not_filtered_out(monkeypatch):
    stream = Stream([frame(RESULT) * 2], monkeypatch)
    with pytest.raises(WireError, match='observed 2'):
        SerialWire(stream).send(b'request')


@pytest.mark.parametrize('wire', [frame(RESULT, corrupt_crc=True), b'\0\x05\x01\0',
                                   b'\0\x01\0', frame(b'x' * 65)])
def test_broken_probe_frame_is_failure(monkeypatch, wire):
    stream = Stream([wire], monkeypatch)
    inspector = SerialWire(stream)
    with pytest.raises(WireError):
        inspector.send(b'request')
    assert inspector.last_exchange['error']


def test_no_result_is_timeout_not_empty_success(monkeypatch):
    with pytest.raises(TimeoutError):
        SerialWire(Stream([], monkeypatch), timeout=0.1).send(b'request')


def test_partial_frame_after_result_is_not_hidden(monkeypatch):
    with pytest.raises(WireError, match='partial trailing'):
        SerialWire(Stream([frame(RESULT) + b'\0\x03x'], monkeypatch)).send(b'request')


@pytest.mark.parametrize('illegal_response', [False, True])
def test_oversize_probe_can_discard_until_gap_without_false_failure(monkeypatch, illegal_response):
    import struct
    from oep_client.conformance import Checks
    from test_conformance import REG

    class GapProbeStream(Stream):
        def __init__(self):
            super().__init__([], monkeypatch)
            self.resume_at = 0

        def write(self, data):
            self.writes.append((self.now, data))
            request = unframe(data[1:-1])
            corr = struct.unpack_from('<H', request, 1)[0]
            if len(request) > 64:
                self.resume_at = self.now + 0.2
                if illegal_response:
                    self.chunks.append(frame(struct.pack('<BHBB', 2, corr, 0, 3)))
                return len(data)
            assert self.now >= self.resume_at, 'recovery request sent before required frame gap'
            payload = b''
            if request[5] == 16:
                payload = struct.pack('<II', 60000, 17)
            elif request[5] == 4:
                payload = struct.pack('<IQ', 17, 100)
            elif request[5] == 19:
                payload = b'\0' * 5
            self.chunks.append(frame(struct.pack('<BHBB', 2, corr, 1, 0) + payload))
            return len(data)

    stream = GapProbeStream()
    wire = SerialWire(stream)
    check = Checks(wire.send, REG, 'unit')
    check.wire, check.boot = wire, 17
    check.check('oversize', 'transport', lambda: check.serial_case('oversized'))
    assert check.results[0]['status'] == ('failed' if illegal_response else 'passed')
    assert check.session is None


def test_silent_discard_interval_is_observed_without_waiting_for_a_reply(monkeypatch):
    stream = Stream([], monkeypatch)
    wire = SerialWire(stream)
    assert wire.exchange([b'bad-input'], 0, silence=0.3) == []
    assert stream.now >= 0.3
