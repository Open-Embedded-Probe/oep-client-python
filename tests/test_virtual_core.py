"""The current virtual core must pass the independent checker before hardware migration."""
import copy
import struct
import subprocess
import sys

import pytest

from oep_client import endpoint, virtual_bench
from oep_client.conformance import Checks
from oep_client.conformance_tcp import TcpWire
from test_conformance import REG


def build(monkeypatch):
    now = [0]
    monkeypatch.setattr('oep_client.conformance.time.sleep',
                        lambda seconds: now.__setitem__(0, now[0] + int(seconds * 1000)))
    return endpoint.Endpoint(virtual_bench.core_v1(), lambda: now[0], boot_id=17), now


def test_virtual_current_core_passes_every_message_contract(monkeypatch):
    ep, now = build(monkeypatch)
    report = Checks(ep.handle, copy.deepcopy(REG), 'virtual-core-1').run()
    failures = [(x['id'], x.get('error')) for x in report['checks'] if x['status'] != 'passed']
    assert not failures
    assert len(report['checks']) == 47
    assert report['status'] == 'passed' and not report['full_conformance']
    assert report['observed']['interfaces'] == []
    assert ep.current_core.holder is None


def request(corr, op, sid, payload=b''):
    return struct.pack('<BHHBI', 1, corr, 0, op, sid) + payload


def test_reboot_invalidates_session_and_replay(monkeypatch):
    ep, now = build(monkeypatch)
    opened = request(1, 16, 123, struct.pack('<IB', 3000, 0))
    assert ep.handle(opened)[3:5] == b'\x01\0'
    ep.reboot(18)
    assert ep.handle(request(2, 18, 123))[3:5] == b'\0\x07'
    assert struct.unpack_from('<I', ep.handle(request(3, 4, 0)), 5)[0] == 18


def test_request_and_response_retention_limits_return_lost(monkeypatch):
    ep, _ = build(monkeypatch)
    ep.remember_max = 10
    opened = request(1, 16, 123, struct.pack('<IB', 3000, 0))
    assert ep.handle(opened)[3:5] == b'\x01\0'
    assert ep.handle(opened)[3:5] == b'\0\x0c'
    assert ep.current_core.holder == 123


def test_core_profile_identity_override_keeps_current_contract(monkeypatch):
    ep, now = build(monkeypatch)
    probe = virtual_bench.with_unit_id(ep.probe, 'alternate-unit')
    ep = endpoint.Endpoint(probe, lambda: now[0], boot_id=17)
    check = Checks(ep.handle, copy.deepcopy(REG), 'ALTERNATE-UNIT')
    check.confirm()
    check.identity()
    check.declarations()


def test_current_virtual_tcp_passes_core_and_tcp_faults():
    # Real loopback socket and existing virtual-bench CLI/server; no private bench imports.
    proc = subprocess.Popen([sys.executable, '-m', 'oep_client.virtual_bench_serve',
                             '--tcp', '0', '--profile', 'core-v1', '--once'],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    wire = None
    try:
        line = proc.stdout.readline().split()
        assert line and line[0] == 'PORT', proc.stderr.read() if proc.poll() is not None else line
        wire = TcpWire.open('tcp://127.0.0.1:' + line[1])
        check = Checks(wire.send, copy.deepcopy(REG), 'virtual-core-1')
        check.tcp_wire = wire
        report = check.run()
        assert report['status'] == 'passed', [(x['id'], x.get('error')) for x in report['checks'] if x['status'] != 'passed']
        assert len(report['checks']) == 54
        assert report['framing_backend'] == 'independent TCP'
        assert not report['full_conformance']
        proc.wait(timeout=5)
    finally:
        if wire:
            wire.close()
        proc.stdin.close()
        if proc.poll() is None:
            proc.terminate()
        proc.wait(timeout=5)


@pytest.mark.skipif(sys.platform != 'linux', reason='PTY exclusive-port lifecycle requires Linux')
def test_current_virtual_serial_passes_faults_and_reconnect():
    from oep_client import link
    from oep_client.conformance_serial import SerialWire
    proc = subprocess.Popen([sys.executable, '-m', 'oep_client.virtual_bench_serve',
                             '--pty', '--profile', 'core-v1'],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    wire = None
    try:
        line = proc.stdout.readline().split()
        assert line and line[0] == 'PTY', proc.stderr.read() if proc.poll() is not None else line
        wire = SerialWire(link.open_serial(line[1]))
        check = Checks(wire.send, copy.deepcopy(REG), 'virtual-core-1')
        check.wire = wire
        def reopen():
            link._exclusive_off(wire.stream)
            wire.stream.close()
            wire.stream = link.open_serial(line[1])
        check.reopen = reopen
        report = check.run()
        assert report['status'] == 'passed', [(x['id'], x.get('error')) for x in report['checks'] if x['status'] != 'passed']
        assert len(report['checks']) == 59 and not report['full_conformance']
        assert report['framing_backend'] == 'independent serial'
    finally:
        if wire:
            link._exclusive_off(wire.stream)
            wire.stream.close()
        proc.stdin.close()
        if proc.poll() is None:
            proc.terminate()
        proc.wait(timeout=5)


def test_current_core_profile_cannot_silently_add_old_interfaces():
    with pytest.raises(ValueError, match='legacy interfaces'):
        virtual_bench.with_stand_in(virtual_bench.core_v1())
