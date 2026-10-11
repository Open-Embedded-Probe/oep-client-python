"""Reader verdicts tested with independently injected decoder defects."""
import pytest
from oep_client.conformance_host_tlv import HostTlvChecks
from oep_client.conformance_host_tlv_sample import decode


# Unit tests inject SPEC identity; real checkout selection is verified separately.
SPEC = '/explicit/spec-input'


@pytest.fixture(autouse=True)
def selected_spec(monkeypatch):
    def identity(path):
        assert path == SPEC
        return {}, {'commit': 'unit-test-fixture', 'dirty': False, 'registry_sha256': 'fixture'}
    monkeypatch.setattr('oep_client.conformance_host_tlv.spec_identity', identity)


def test_sample_reader_contracts_pass():
    report = HostTlvChecks(decode, spec=SPEC).run()
    assert report['status'] == 'passed', report['checks']
    assert len(report['checks']) == 6 and not report['full_conformance']


@pytest.mark.parametrize('defect,case', [
    ('critical_alias', 'CRITICAL'), ('reject_unknown', 'UNKNOWN'),
    ('last_duplicate', 'DUPLICATES'), ('sort_repeated', 'DUPLICATES'),
    ('accept_short', 'BROKEN'), ('accept_zero', 'BROKEN'),
    ('accept_bad_width', 'KNOWN_WIDTH'), ('reject_large', 'SIZES'),
    ('reject_127', 'UNKNOWN'), ('skip_unknown_length', 'BROKEN'),
])
def test_faults_are_detected(defect, case):
    def broken(raw):
        if defect == 'critical_alias':
            raw = raw.replace(b'\x81\x01\x00\x63', b'\x01\x01\x00\x63')
        if defect == 'reject_unknown' and b'unknown' in raw: raise ValueError('unknown tag')
        if defect == 'last_duplicate' and raw.endswith(b'\x01\x01\x00\x09'):
            result = decode(raw); result['first'] = 9; return result
        if defect == 'sort_repeated':
            result = decode(raw); result['repeated'].sort(); return result
        if defect == 'accept_short':
            try: return decode(raw)
            except ValueError: return {'fixed': 0x1234, 'blob': b'abc', 'first': None, 'repeated': []}
        if defect == 'accept_zero' and raw[-3:] in (b'\x00\x00\x00', b'\x80\x00\x00'):
            return decode(raw[:-3])
        if defect == 'accept_bad_width' and raw.endswith(b'\x01\x02\x00\x07\x08'):
            return {'fixed': 0x1234, 'blob': b'abc', 'first': 7, 'repeated': []}
        if defect == 'reject_127' and b'\x7f\x07\x00unknown' in raw: raise ValueError('old reserved tag')
        if defect == 'skip_unknown_length' and raw.endswith(b'\x02\x02\x00x'):
            return decode(raw[:-4])
        if defect == 'reject_large' and len(raw) > 256: raise ValueError('too large')
        return decode(raw)
    report = HostTlvChecks(broken, spec=SPEC).run()
    assert report['status'] == 'failed'
    row = next(r for r in report['checks'] if r['id'] == 'HOST-TLV-' + case)
    assert row['status'] == 'failed'
    assert row['error'].startswith('Violation:'), row


def test_broken_known_value_may_be_ignored_without_using_it():
    def tolerant(raw):
        try: return decode(raw)
        except ValueError as exc:
            if str(exc) != 'known u8 TLV width': raise
            return {'fixed': 0x1234, 'blob': b'abc', 'first': None, 'repeated': []}
    assert HostTlvChecks(tolerant, spec=SPEC).run()['status'] == 'passed'
