"""Challenge the checker with invalid results and the independent legacy probe."""
import copy
import struct

import pytest

from oep_client.conformance import Checks, Violation, ops, tlvs
from oep_client import endpoint, virtual_bench, cobs

# Deliberately independent of the runtime registry, which still describes the old SPEC.
REG = {
    'roles': {'request': 1, 'result': 2},
    'resolutions': {'rejected': 0, 'completed': 1},
    'outcomes': {'success': 0, 'failed': 1, 'partial': 2},
    'reject_reasons': {'unknown_function': 1, 'unknown_operation': 2, 'malformed': 3,
                      'unavailable': 4, 'window_exceeded': 6, 'no_session': 7, 'locked': 8,
                      'session_required': 9, 'no_resource': 10, 'unsupported': 11, 'result_lost': 12},
    'describe_common': {'ops': 7},
    'core': {'op': [{'name': n, 'code': c} for n, c in
                    [('confirm', 1), ('list', 2), ('describe', 3), ('clock', 4),
                     ('open', 16), ('end', 17), ('keepalive', 18), ('lock_state', 19)]],
             'tlv': {'confirm_answer': {'transport': 1},
                     'describe': {'firmware': 64, 'model': 65, 'unit_id': 66, 'channels': 67,
                                  'transport': 73, 'chip': 76, 'max_op_ms': 77}}},
    'interface': [],
}


@pytest.mark.parametrize('data', [b'\0\0\0', b'\x80\0\0', b'\x01', b'\x01\x02\0x', b'\xff\x01\0x'])
def test_truncated_or_zero_tlv_rejected(data):
    with pytest.raises(Violation):
        tlvs(data)


@pytest.mark.parametrize('data', [b'', b'\0', b'\xf9\x01', b'\xff\0'])
def test_invalid_ops_rejected(data):
    with pytest.raises(Violation):
        ops(data)


def test_ops_top_and_tlv_extension_are_valid():
    assert ops(b'\xf8\x80') == {255}
    assert tlvs(b'\x7f\x01\0x') == [(127, b'x')]


@pytest.mark.parametrize('reply', [b'', b'\x02\x01\0', b'\x03\x01\0\x01\0',
                                   b'\x02\x02\0\x01\0', b'\x02\x01\0\x02\0',
                                   b'\x02\x01\0\x01\x03', b'\x02\x01\0\0\x05',
                                   b'\x02\x01\0\0\x03x', b'\x02\x01\0\0\x08',
                                   b'\x02\x01\0\0\x0b'])
def test_bad_result_is_failure_with_byte_evidence(reply):
    check = Checks(lambda _: reply, REG, 'test-unit')
    check.check('bad', 'core §4', lambda: check.exchange(check.request(4, corr=1)))
    assert check.results[0]['status'] == 'failed'
    assert check.results[0]['exchanges'][0]['response_hex'] == reply.hex()


@pytest.mark.parametrize('failure', [TimeoutError('lost result'), cobs.CorruptFrame('bad CRC')])
def test_transport_error_blocks_following_checks_without_recovery(failure):
    calls = []

    def send(req):
        calls.append(req)
        raise failure

    check = Checks(send, REG, 'test-unit')
    report = check.run()
    assert report['status'] == 'failed'
    assert len(calls) == 1
    assert report['checks'][0]['status'] == 'failed'
    assert all(x['status'] == 'blocked' for x in report['checks'][1:])


def legacy(monkeypatch):
    now = [1000]
    monkeypatch.setattr('oep_client.conformance.time.sleep', lambda seconds: now.__setitem__(0, now[0] + int(seconds * 1000)))
    probe = virtual_bench.PROFILES['p4-bench']()
    ep = endpoint.Endpoint(probe, lambda: now[0])
    return ep, virtual_bench.unit_id_of(probe)


def test_old_probe_does_not_pass_new_core_contract(monkeypatch):
    ep, unit = legacy(monkeypatch)
    report = Checks(ep.handle, copy.deepcopy(REG), unit).run()
    results = {r['id']: r for r in report['checks']}
    assert report['status'] == 'failed'
    for name in ('CORE-DECLARE', 'CORE-ZERO-CORR', 'CORE-TLV-ZERO', 'CORE-OPEN-AFTER-END',
                 'CORE-OPEN-HISTORY', 'CORE-ALTERED-REPLAY', 'CORE-CORR-U16'):
        assert results[name]['status'] == 'failed', name
        if name != 'CORE-DECLARE':
            assert results[name]['exchanges'], name
    # Even when the probe resurrects a session, the runner releases its own lock.
    assert results['CORE-LEASE']['status'] == 'passed'
    assert results['CORE-END']['status'] == 'failed'


def test_identity_mismatch_prevents_all_session_mutation(monkeypatch):
    ep, _ = legacy(monkeypatch)
    sent = []

    def send(req):
        sent.append(req)
        return ep.handle(req)

    report = Checks(send, copy.deepcopy(REG), 'different-unit').run()
    assert report['status'] == 'failed'
    assert all(struct.unpack_from('<I', req, 6)[0] == 0 for req in sent)
    assert all(req[5] not in (16, 17, 18) for req in sent)


@pytest.mark.parametrize('lost, changed, across_open', [(False, False, False), (True, False, False),
                                                      (False, True, False), (False, False, True),
                                                      (False, True, True)])
def test_replay_accepts_cached_or_lost_result_but_catches_reexecution(monkeypatch, lost, changed, across_open):
    monkeypatch.setattr('oep_client.conformance.time.sleep', lambda _: None)
    clocks = [0]

    def send(req):
        corr = struct.unpack_from('<H', req, 1)[0]
        p = b''
        resolution, detail = 1, 0
        if req[5] == 16:
            p = struct.pack('<II', 3000, 17)
        elif req[5] == 4:
            clocks[0] += 1
            if clocks[0] == 2 and lost:
                resolution, detail = 0, 12
            else:
                p = struct.pack('<IQ', 17, 100 + (1 if clocks[0] == 2 and changed else 0))
        elif req[5] == 19:
            p = b'\0' * 5
        return struct.pack('<BHBB', 2, corr, resolution, detail) + p

    check = Checks(send, REG, 'unit')
    check.boot = 17
    check.check('replay', 'core §5.2', lambda: check.replay(across_open))
    assert check.results[0]['status'] == ('failed' if changed else 'passed')
    assert check.session is None
