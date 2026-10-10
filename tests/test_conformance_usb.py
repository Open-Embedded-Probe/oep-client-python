"""Literal USB bytes, bad probe output, and an independent virtual core endpoint."""
from collections import deque
import copy

import pytest

from oep_client import endpoint, virtual_bench
from oep_client.conformance import Checks
from oep_client.conformance_serial import WireError
from oep_client.conformance_usb import UsbWire, frame
from oep_client.conformance_usb_cases import run
from oep_client.virtual_bench_usb import Usb
from test_conformance import REG


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class Raw:
    def __init__(self, clock, chunks=()):
        self.clock, self.chunks, self.writes = clock, deque(chunks), []
        self.closed = False

    def write(self, data, method='out'):
        self.writes.append((data, method))
        return len(data)

    def read(self, timeout):
        if self.chunks:
            return self.chunks.popleft()
        self.clock.sleep(timeout)
        return None

    def close(self):
        self.closed = True


RESULT = b'\x02\x01\0\x01\0'


def wire(clock, raw, kind='bulk', **kwargs):
    return UsbWire(raw, kind, out_packet_size=8, input_size=9, output_size=11,
                   report_id=kwargs.pop('report_id', 6), clock=clock, sleep=clock.sleep, **kwargs)


@pytest.mark.parametrize('split', range(1, 7))
def test_bulk_response_transfer_boundaries_do_not_define_frames(split):
    clock = Clock()
    data = b'\x05\0' + RESULT  # literal expected wire, not encoder-derived
    backend = Raw(clock, [data[:split], b'', data[split:]])
    assert wire(clock, backend).send(b'request') == RESULT


@pytest.mark.parametrize('data,error', [
    (b'\x05\0\x02', 'USB replies'),
    (b'\x41\0', 'oversized USB'),
    (b'\x05\0' + RESULT + b'\x05', 'partial'),
    ((b'\x05\0' + RESULT) * 2, 'observed 2'),
])
def test_corrupt_or_duplicate_bulk_probe_output_is_not_recovered(data, error):
    clock = Clock()
    with pytest.raises((WireError, TimeoutError), match=error):
        wire(clock, Raw(clock, [data]), timeout=0.1).send(b'request')


def test_bulk_length_zero_and_zlp_are_distinct_and_ignored():
    clock = Clock()
    backend = Raw(clock, [b'', b'\0\0\x05\0' + RESULT])
    inspector = wire(clock, backend)
    assert inspector.send(b'123456') == RESULT  # prefix + 6 = exactly one OUT packet
    assert backend.writes == [(b'\x06\x00123456', 'out'), (b'', 'out')]
    assert inspector.last_exchange['reads'][0]['hex'] == ''


@pytest.mark.parametrize('report_id', [0, 6])
def test_hid_response_crosses_count_and_length_boundaries(report_id):
    clock = Clock()
    prefix = bytes((report_id,)) if report_id else b''
    size = 9 - len(prefix)
    def report(data):
        return prefix + len(data).to_bytes(2, 'little') + data + bytes(size - 2 - len(data))
    backend = Raw(clock, [report(b'\x05'), report(b''), report(b'\0' + RESULT)])
    inspector = wire(clock, backend, 'hid', report_id=report_id)
    assert inspector.send(b'request') == RESULT
    assert all(len(data) == 11 for data, _ in backend.writes)
    joined = b''.join(data[len(prefix) + 2:len(prefix) + 2 +
                          int.from_bytes(data[len(prefix):len(prefix) + 2], 'little')]
                      for data, _ in backend.writes)
    assert joined == b'\x07\0request'


@pytest.mark.parametrize('data,error', [
    (b'\x06\x07\0abcdef', 'oversized HID count'),
    (b'\x05\0\0' + b'\0' * 6, 'undeclared'),
    (b'\x06\x01\0x' + b'\xff' * 5, 'nonzero HID padding'),
    (b'\x06\0\0', 'wrong size'),
])
def test_hid_bad_probe_reports_fail_with_raw_evidence(data, error):
    clock = Clock()
    inspector = wire(clock, Raw(clock, [data]), 'hid')
    with pytest.raises(WireError, match=error):
        inspector.send(b'request')
    assert inspector.last_exchange['reads'][0]['hex'] == data.hex()
    assert inspector.last_exchange['error']


def test_receive_only_observes_window_and_raw_empty_reports():
    clock = Clock()
    inspector = wire(clock, Raw(clock, [b'\x06\0\0' + b'\0' * 6]), 'hid')
    assert inspector.exchange([], 0, silence=0.3) == []
    assert clock.now >= 0.3
    assert inspector.last_exchange['silence_ms'] == 300


@pytest.mark.parametrize('kind,report_id,report_size', [('bulk', 0, 9), ('hid', 0, 9),
                                                       ('hid', 6, 9), ('hid', 255, 511)])
def test_current_virtual_core_passes_usb_contracts(monkeypatch, kind, report_id, report_size):
    clock = Clock()
    monkeypatch.setattr('oep_client.conformance.time.sleep', clock.sleep)
    ep = endpoint.Endpoint(virtual_bench.core_v1(), lambda: int(clock() * 1000), boot_id=17)
    backend = Usb(ep, kind, clock=clock, sleep=clock.sleep, report_id=report_id,
                  input_size=report_size, output_size=report_size + 2)
    inspector = UsbWire(backend, kind, clock=clock, sleep=clock.sleep, report_id=report_id,
                        input_size=report_size, output_size=report_size + 2, out_packet_size=8)
    check = Checks(inspector.send, copy.deepcopy(REG), 'virtual-core-1')
    check.usb_wire = inspector
    report = check.run()
    assert report['status'] == 'passed', [(r['id'], r.get('error')) for r in report['checks'] if r['status'] == 'failed']
    assert len(report['checks']) == (55 if kind == 'bulk' else 60)
    assert report['framing_backend'] == 'independent USB ' + kind
    assert not report['full_conformance'] and check.session is None
    assert ep.current_core.holder is None
    not_applicable = [r['id'] for r in report['checks'] if r['status'] == 'not_applicable']
    assert not_applicable == (['CORE-HID-REPORT-ID'] if kind == 'hid' and report_id == 0 else [])


@pytest.mark.parametrize('fault', ['clip_count', 'padding', 'ignore_set_report', 'keep_partial', 'accept_oversize'])
def test_checker_detects_usb_probe_faults(monkeypatch, fault):
    clock = Clock()
    ep = endpoint.Endpoint(virtual_bench.core_v1(), lambda: int(clock() * 1000), boot_id=17)
    backend = Usb(ep, 'hid', clock=clock, sleep=clock.sleep)
    inspector = wire(clock, backend, 'hid')
    check = Checks(inspector.send, REG, 'virtual-core-1')
    check.usb_wire = inspector
    check.confirm()
    check.identity()
    original = backend.write
    def mutate(data, method='out'):
        if fault == 'clip_count' and data[1:3] == b'\x09\0':
            data = data[:1] + b'\x08\0' + data[3:]  # wrongly clip count to report capacity
        if fault == 'padding' and any(data[3 + int.from_bytes(data[1:3], 'little'):]):
            return len(data)  # wrongly reject nonzero receiver padding
        if fault == 'ignore_set_report' and method == 'set-report':
            return len(data)
        if fault == 'keep_partial':
            backend.last = None  # wrongly keep prefix through a gap
        if fault == 'accept_oversize' and data[3:5] == b'\x41\0':
            data = inspector.pack(data[5:3 + int.from_bytes(data[1:3], 'little')])
        return original(data, method)
    backend.write = mutate
    case = {'clip_count': 'count', 'padding': 'padding', 'ignore_set_report': 'set-report',
            'keep_partial': 'truncated', 'accept_oversize': 'oversized'}[fault]
    check.check('usb', 'transports', lambda: run(check, case))
    assert check.results[-1]['status'] == 'failed'
    assert check.results[-1]['exchanges']


def test_raw_usb_transport_errors_block_followups():
    clock = Clock()
    backend = Raw(clock)
    def fail_read(timeout):
        raise OSError('device disconnected')
    backend.read = fail_read
    inspector = wire(clock, backend)
    check = Checks(inspector.send, REG, 'virtual-core-1')
    check.usb_wire = inspector
    report = check.run()
    assert report['checks'][0]['status'] == 'failed'
    assert all(r['status'] == 'blocked' for r in report['checks'][1:])
    assert 'device disconnected' in report['checks'][0]['error']


@pytest.mark.parametrize('kind,kwargs', [('tcp', {}), ('bulk', {}), ('bulk', {'out_packet_size': 0}),
                                        ('hid', {'input_size': 2, 'output_size': 8}),
                                        ('hid', {'input_size': 8, 'output_size': 2})])
def test_raw_backend_requires_explicit_valid_shape(kind, kwargs):
    with pytest.raises(ValueError):
        UsbWire(Raw(Clock()), kind, **kwargs)


@pytest.mark.parametrize('failure', ['missing_lock', 'existing_output', 'bad_shape', 'read_error', 'bad_identity', 'close_error'])
def test_raw_usb_api_reserves_evidence_locks_and_closes_backend(monkeypatch, tmp_path, failure):
    import json
    from oep_client.conformance_usb import inspect
    monkeypatch.setattr('oep_client.conformance.spec_identity', lambda path: (REG, {'commit': 'test', 'dirty': False}))
    lock = tmp_path / 'equipment.lock'
    out = tmp_path / 'evidence.json'
    if failure != 'missing_lock':
        lock.touch()
    if failure == 'existing_output':
        out.write_text('previous evidence')
    opened = []
    import time
    origin = time.monotonic()
    ep = endpoint.Endpoint(virtual_bench.core_v1(), lambda: int((time.monotonic() - origin) * 1000), boot_id=17)
    backend = Usb(ep, 'bulk')
    if failure == 'read_error':
        def fail_read(timeout):
            raise OSError('device removed')
        backend.read = fail_read
    def factory():
        opened.append(True)
        return backend
    if failure == 'close_error':
        def fail_close():
            backend.closed = True
            raise OSError('close failed')
        backend.close = fail_close
    kwargs = dict(kind='bulk', unit='wrong-unit', spec='explicit-spec', out=out, lock=lock,
                  adapter='explicit test raw adapter', out_packet_size=None if failure == 'bad_shape' else 8)
    if failure == 'existing_output':
        with pytest.raises(FileExistsError):
            inspect(factory, **kwargs)
        assert not opened and out.read_text() == 'previous evidence'
        return
    report = inspect(factory, **kwargs)
    assert report['status'] == 'failed'
    assert json.loads(out.read_text())['status'] == 'failed'
    if failure in ('missing_lock', 'bad_shape'):
        assert not opened
    else:
        assert opened and backend.closed
    if failure == 'close_error':
        assert 'close failed' in report['error']
    if failure == 'read_error':
        assert 'device removed' in report['checks'][0]['error']
    if failure == 'bad_identity':
        assert all(bytes.fromhex(r['request_hex'])[5] not in (16, 17, 18)
                   for row in report['checks'] for r in row['exchanges'] if 'request_hex' in r)



def test_hid_count_uses_both_bytes_and_sender_zero_padding():
    clock = Clock()
    message = b'\x02\x01\0\x01\0' + b'x' * 300
    encoded = len(message).to_bytes(2, 'little') + message
    raw = b'\x06' + len(encoded).to_bytes(2, 'little') + encoded + bytes(511 - 3 - len(encoded))
    backend = Raw(clock, [raw])
    inspector = UsbWire(backend, 'hid', input_size=511, output_size=511, report_id=6,
                        clock=clock, sleep=clock.sleep)
    inspector.max_frame = 512
    assert inspector.send(message) == message
    assert backend.writes == [(raw, 'out')]


@pytest.mark.parametrize('kind,report_id', [('bulk', 6), ('hid', 0), ('hid', 6)])
def test_software_model_cli_passes_explicit_settings_to_reserved_runner(monkeypatch, kind, report_id):
    from oep_client import conformance_usb_model
    calls = []
    def inspect(factory, **kwargs):
        calls.append(kwargs)
        model = factory()
        assert model.kind == kind and model.endpoint.probe.core_contract
        assert model.endpoint.transports[model.transport] == (4 if kind == 'bulk' else 5)
        if kind == 'hid':
            assert model.report_id == report_id
        model.close()
        return {'status': 'passed'}
    monkeypatch.setattr(conformance_usb_model, 'inspect', inspect)
    result = conformance_usb_model.main(['--kind', kind, '--report-id', str(report_id),
        '--unit', 'virtual-core-1', '--spec', '/explicit/spec', '--out', '/explicit/new.json',
        '--lock', '/explicit/virtual.lock'])
    assert result == 0
    assert calls[0]['spec'] == '/explicit/spec' and calls[0]['out'] == '/explicit/new.json'
    assert calls[0]['lock'] == '/explicit/virtual.lock'
    assert 'no physical USB' in calls[0]['adapter']
