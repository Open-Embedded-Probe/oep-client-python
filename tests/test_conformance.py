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


@pytest.mark.parametrize('mode, fn, op, sid, corr, payload, reason, response_tail', [
    ('revision-order', 0, 1, 0, 1001, b'OEP?\x02\x01', 3, b''),
    ('confirm-short', 0, 1, 0, 1001, b'OEP?\x01', 3, b''),
    ('confirm-magic', 0, 1, 0, 1001, b'BAD?\x01\x01', 3, b''),
    ('tlv-header', 0, 1, 0, 1001, b'OEP?\x01\x01\x7f\0', 3, b''),
    ('tlv-value', 0, 1, 0, 1001, b'OEP?\x01\x01\x7f\x02\0x', 3, b''),
    ('tlv-critical-zero', 0, 1, 0, 1001, b'OEP?\x01\x01\x80\0\0', 3, b''),
    ('unknown-critical', 0, 1, 0, 1001, b'OEP?\x01\x01\xff\x01\0x', 11, b'\xff'),
    ('zero-priority', 65535, 255, 0xffffffff, 0, b'x', 3, b''),
    ('fn-priority', 65535, 255, 0xffffffff, 1, b'x', 1, b''),
    ('op-priority', 0, 0, 0xffffffff, 1, b'x', 2, b''),
    ('session-priority', 0, 18, 0, 1001, b'x', 9, b''),
    ('session-before-payload', 0, 18, 42, 1, b'x', 7, b''),
])
@pytest.mark.parametrize('wrong', [False, True])
def test_core_negative_predicates_use_spec_request_and_reason(monkeypatch, mode, fn, op, sid, corr, payload, reason, response_tail, wrong):
    monkeypatch.setattr('oep_client.conformance.secrets.randbelow', lambda _: 41)
    expected = struct.pack('<BHHBI', 1, corr, fn, op, sid) + payload

    def send(req):
        assert req == expected
        if wrong:
            return struct.pack('<BHBB', 2, corr, 1, 0)
        return struct.pack('<BHBB', 2, corr, 0, reason) + response_tail

    check = Checks(send, REG, 'unit')
    check.check('predicate', 'core', lambda: check.invalid_request(mode))
    assert check.results[0]['status'] == ('failed' if wrong else 'passed')


def test_all_core_predicates_have_individual_results(monkeypatch):
    from oep_client.conformance import CORE_CASES
    ep, unit = legacy(monkeypatch)
    report = Checks(ep.handle, REG, unit).run()
    ids = [r['id'] for r in report['checks'] if r['level'] == 'core']
    assert set(ids) == set(CORE_CASES)
    assert len(ids) == len(set(ids))


def test_valid_open_end_keepalive_response_extensions_are_accepted(monkeypatch):
    monkeypatch.setattr('oep_client.conformance.time.sleep', lambda _: None)
    extension = b'\x7f\x01\0x'

    def send(req):
        p = extension
        if req[5] == 16:
            p = struct.pack('<II', 3000, 17) + extension
        elif req[5] == 19:
            p = b'\0' * 5 + extension
        return struct.pack('<BHBB', 2, struct.unpack_from('<H', req, 1)[0], 1, 0) + p

    check = Checks(send, REG, 'unit')
    check.boot = 17
    with check.holding() as sid:
        check.success(check.request(18, session=sid))
    assert check.session is None


def test_force_is_not_sent_when_initial_acquisition_is_denied():
    sent = []

    def send(req):
        sent.append(req)
        return struct.pack('<BHBBI', 2, struct.unpack_from('<H', req, 1)[0], 0, 8, 1000)

    check = Checks(send, REG, 'unit')
    check.check('force', 'core', check.force_owned)
    assert check.results[0]['status'] == 'failed'
    assert len(sent) == 1 and sent[0][14] == 0


def test_bad_confirm_never_sends_discovery_or_session_requests():
    sent = []

    def send(req):
        sent.append(req)
        return struct.pack('<BHBB', 2, struct.unpack_from('<H', req, 1)[0], 1, 0) + b'NOT-OEP'

    report = Checks(send, REG, 'unit').run()
    assert len(sent) == 1 and sent[0][5] == 1
    assert report['checks'][0]['status'] == 'failed'
    assert all(row['status'] == 'blocked' for row in report['checks'][1:])


@pytest.mark.parametrize('changed', [False, True])
def test_list_stability_checks_fields_without_repeating_interface_case_ids(changed):
    expected = dict(fn=1, instance=0, revision=1, name='x.y')

    def send(req):
        first = struct.unpack_from('<H', req, 10)[0]
        p = struct.pack('<HB', 1, 0 if first else 1)
        if not first:
            p += struct.pack('<HHBBB', 2 if changed else 1, 0, 1, 0, 3) + b'x.y'
        return struct.pack('<BHBB', 2, struct.unpack_from('<H', req, 1)[0], 1, 0) + p

    check = Checks(send, REG, 'unit')
    check.observed['interfaces'] = [{**expected, 'describe': []}]
    check.check('list', 'core §7.2', check.list_stability)
    assert check.results[0]['status'] == ('failed' if changed else 'passed')
    assert len(check.results) == 1


@pytest.mark.parametrize('reexecuted', [False, True])
def test_old_unseen_corr_is_result_lost_not_a_new_operation(monkeypatch, reexecuted):
    def send(req):
        corr = struct.unpack_from('<H', req, 1)[0]
        if req[5] == 4 and corr == 2 and not reexecuted:
            return struct.pack('<BHBB', 2, corr, 0, 12)
        p = b''
        if req[5] == 16:
            p = struct.pack('<II', 3000, 17)
        elif req[5] == 4:
            p = struct.pack('<IQ', 17, 100)
        elif req[5] == 19:
            p = b'\0' * 5
        return struct.pack('<BHBB', 2, corr, 1, 0) + p

    check = Checks(send, REG, 'unit')
    check.boot = 17
    check.check('old', 'core §5.2', check.unseen_old)
    assert check.results[0]['status'] == ('failed' if reexecuted else 'passed')
    assert check.session is None


def test_interface_failures_are_reported_together_not_stopped_at_first(monkeypatch):
    from oep_client.pytest_conformance import assert_interfaces
    monkeypatch.setenv('OEP_CONFORMANCE_OUT', 'evidence.json')
    report = {'spec': {'commit': 'test'}, 'checks': [
        {'id': 'IF-1', 'level': 'interface', 'status': 'failed', 'error': 'one'},
        {'id': 'IF-2', 'level': 'interface', 'status': 'failed', 'error': 'two'},
        {'id': 'CORE-LIST', 'level': 'core', 'status': 'passed'}]}
    with pytest.raises(AssertionError) as failure:
        assert_interfaces(report, lambda *_: None)
    assert 'IF-1: one' in str(failure.value) and 'IF-2: two' in str(failure.value)


def test_framing_only_configuration_is_incomplete_not_equipment_skip(monkeypatch):
    from oep_client.pytest_conformance import oep_conformance_report
    for key in ('OEP_CONFORMANCE_ADDRESS', 'OEP_CONFORMANCE_UNIT_ID', 'OEP_CONFORMANCE_SPEC', 'OEP_CONFORMANCE_OUT', 'OEP_HW_LOCK'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('OEP_CONFORMANCE_FRAMING', 'serial')
    with pytest.raises(pytest.fail.Exception, match='incomplete'):
        oep_conformance_report.__wrapped__()


@pytest.mark.parametrize('attribute,key', [('wire','serial_wire'), ('tcp_wire','tcp_wire'), ('usb_wire','usb_wire')])
def test_sender_failure_does_not_attach_previous_wire_exchange(attribute, key):
    from types import SimpleNamespace
    def fail_before_wire(req):
        raise OSError('failed before any transfer')
    check = Checks(fail_before_wire, REG, 'unit')
    setattr(check, attribute, SimpleNamespace(last_exchange={'writes': [{'hex': 'previous'}]}))
    check.check('failure', 'core', lambda: check.exchange(check.request(4)))
    exchange = check.results[0]['exchanges'][0]
    assert check.abort and 'failed before any transfer' in exchange['error']
    assert key not in exchange
