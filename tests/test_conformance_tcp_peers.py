"""Real TCP sockets plus mutated peer responses: test the checker, not just a happy path."""
import copy
from contextlib import contextmanager
import subprocess
import sys

import pytest

from oep_client.conformance import Checks, TCP_PEER_CASES
from oep_client.conformance_tcp import TcpWire
from oep_client.conformance_tcp_peers import Peers
from test_conformance import REG


@contextmanager
def runner():
    proc = subprocess.Popen([sys.executable, '-m', 'oep_client.virtual_bench_serve',
                             '--tcp', '0', '--profile', 'core-v1'],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    wire = None
    try:
        where = proc.stdout.readline().split()
        assert where and where[0] == 'PORT', proc.stderr.read() if proc.poll() is not None else where
        address = 'tcp://127.0.0.1:' + where[1]
        wire = TcpWire.open(address)
        check = Checks(wire.send, copy.deepcopy(REG), 'virtual-core-1')
        check.tcp_wire = wire
        check.tcp_peer_open = lambda: TcpWire.open(address)
        def reopen():
            wire.stream = TcpWire.open(address).stream
        check.tcp_reopen = reopen
        yield check
    finally:
        if wire:
            wire.close()
        proc.stdin.close()
        if proc.poll() is None:
            proc.terminate()
        proc.wait(timeout=5)


def discover(check):
    check.confirm()
    check.identity()
    check.interfaces()


def test_virtual_core_passes_all_tcp_peer_contracts():
    with runner() as check:
        report = check.run()
        assert report['status'] == 'passed', [(r['id'], r.get('error')) for r in report['checks'] if r['status'] != 'passed']
        assert len(report['checks']) == 60 and not report['full_conformance']
        assert check.session is None
        assert {r['id'] for r in report['checks']} >= {name for name, _ in TCP_PEER_CASES}
        assert all(r['exchanges'] for r in report['checks'] if r['id'].startswith('CORE-TCP-PEER-'))


@pytest.mark.parametrize('fault', ['confirm', 'boot', 'unit', 'transport', 'list'])
def test_invalid_peer_identity_stops_before_session_mutation(fault):
    with runner() as check:
        discover(check)
        original_open = check.tcp_peer_open
        sent = []
        def open_peer():
            wire = original_open()
            send = wire.send
            def mutate(req):
                sent.append(req)
                reply = send(req)
                if req[5] == 1:
                    if fault == 'confirm':
                        return reply[:5] + b'broken'
                    if fault == 'boot':
                        out = bytearray(reply)
                        out[18:22] = (int.from_bytes(out[18:22], 'little') ^ 1).to_bytes(4, 'little')
                        return bytes(out)
                    if fault == 'transport':
                        return reply[:-1] + b'\xff'
                if req[5] == 3 and fault == 'unit':
                    return reply.replace(b'virtual-core-1', b'wrong---core-1')
                if req[5] == 2 and fault == 'list':
                    return reply[:5] + b'\x01\0\0'  # non-progressing list
                return reply
            wire.send = mutate
            return wire
        check.tcp_peer_open = open_peer
        check.check('peer', 'transports', Peers(check).identity)
        assert check.results[-1]['status'] == 'failed' and check.abort
        assert all(req[5] in (1, 2, 3) for req in sent)
        if fault == 'confirm':
            assert [req[5] for req in sent] == [1]


def test_checker_catches_response_leak_to_other_connection():
    with runner() as check:
        discover(check)
        original_open = check.tcp_peer_open
        def open_peer():
            wire = original_open()
            exchange = wire.exchange
            def mutate(chunks, count, **kwargs):
                replies = exchange(chunks, count, **kwargs)
                return [b'\x02\x01\0\x01\0'] if not chunks else replies
            wire.exchange = mutate
            return wire
        check.tcp_peer_open = open_peer
        check.check('peer', 'transports', Peers(check).route)
        assert check.results[-1]['status'] == 'failed'
        assert 'wrong TCP connection' in check.results[-1]['error']


def test_checker_catches_separate_lock_on_peer():
    with runner() as check:
        discover(check)
        original_open = check.tcp_peer_open
        def open_peer():
            wire = original_open()
            send = wire.send
            def mutate(req):
                reply = send(req)
                return reply[:5] + b'\0' * 5 if req[5] == 19 else reply
            wire.send = mutate
            return wire
        check.tcp_peer_open = open_peer
        check.check('peer', 'core', Peers(check).lock)
        assert check.results[-1]['status'] == 'failed'
        assert 'shared lock/owner' in check.results[-1]['error']
        assert check.session is None


def test_only_tcp_peer_setting_is_incomplete_not_skipped(monkeypatch):
    from oep_client.pytest_conformance import oep_conformance_report
    for key in ('OEP_CONFORMANCE_ADDRESS', 'OEP_CONFORMANCE_UNIT_ID', 'OEP_CONFORMANCE_SPEC',
                'OEP_CONFORMANCE_OUT', 'OEP_HW_LOCK', 'OEP_CONFORMANCE_FRAMING'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('OEP_CONFORMANCE_TCP_PEER', 'tcp://127.0.0.1:1')
    with pytest.raises(pytest.fail.Exception, match='incomplete'):
        oep_conformance_report.__wrapped__()


@pytest.mark.parametrize('fault', ['state', 'reopen', 'foreign_owner', 'takeover'])
def test_failed_reopen_never_ends_old_session_on_new_connection(fault):
    with runner() as check:
        discover(check)
        original_open = check.tcp_peer_open
        old_session = []
        sent = []
        original_send = check.send
        def primary_send(req):
            sent.append(req)
            if req[5] == 16:
                old_session.append(int.from_bytes(req[6:10], 'little'))
                if fault == 'takeover' and len(old_session) == 2:
                    raise TimeoutError('takeover result lost')
            return original_send(req)
        check.send = primary_send
        def open_peer():
            wire = original_open()
            send = wire.send
            def mutate(req):
                reply = send(req)
                if req[5] == 19:
                    if fault == 'state':
                        return reply[:5] + b'bad'
                    if fault == 'foreign_owner':
                        return reply.replace(b'tcp-', b'bad-')
                return reply
            wire.send = mutate
            return wire
        check.tcp_peer_open = open_peer
        if fault == 'reopen':
            def fail_reopen():
                raise OSError('cannot reconnect')
            check.tcp_reopen = fail_reopen
        check.check('peer', 'core', Peers(check).close)
        assert check.results[-1]['status'] == 'failed' and check.abort
        assert check.session is None
        assert not any(req[5] == 18 for req in sent)
        assert len(old_session) == (2 if fault == 'takeover' else 1)


def test_failure_with_partial_input_aborts_without_followup_requests():
    with runner() as check:
        discover(check)
        original_open = check.tcp_peer_open
        def open_peer():
            wire = original_open()
            send = wire.send
            def fail_clock(req):
                if req[5] == 4:
                    raise TimeoutError('peer blocked by partial primary input')
                return send(req)
            wire.send = fail_clock
            return wire
        check.tcp_peer_open = open_peer
        check.check('peer', 'transports', Peers(check).partial)
        assert check.results[-1]['status'] == 'failed' and check.abort
        assert 'partial primary' in check.results[-1]['error']



def test_response_to_partial_header_aborts_without_followup_requests():
    with runner() as check:
        discover(check)
        exchange = check.tcp_wire.exchange
        def mutate(chunks, count, **kwargs):
            replies = exchange(chunks, count, **kwargs)
            return [b"\x02\x01\0\x01\0"] if count == 0 else replies
        check.tcp_wire.exchange = mutate
        check.check('peer', 'transports', Peers(check).partial)
        assert check.results[-1]['status'] == 'failed' and check.abort
        assert 'partial TCP header' in check.results[-1]['error']
