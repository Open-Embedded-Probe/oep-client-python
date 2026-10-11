import pytest
from oep_client.conformance_host_notifications import HostNotificationChecks, event
from oep_client.conformance_host_notifications_sample import SampleHostNotificationReader

SPEC = '/explicit/spec-input'


@pytest.fixture(autouse=True)
def selected_spec(monkeypatch):
    def identity(path):
        assert path == SPEC
        return {}, {'commit': 'unit-fixture', 'dirty': False, 'registry_sha256': 'fixture'}
    monkeypatch.setattr('oep_client.conformance_host_notifications.spec_identity', identity)


def test_notification_reader_contracts_pass():
    report = HostNotificationChecks(SampleHostNotificationReader(), spec=SPEC).run()
    assert report['status'] == 'passed', report['checks']
    assert len(report['checks']) == 5 and not report['full_conformance']
    assert sum(len(row['observations']) for row in report['checks']) == 12


@pytest.mark.parametrize('defect,case', [
    ('notification_reply', 'DEMUX'), ('stale_reply', 'DEMUX'),
    ('unknown_not_counted', 'UNKNOWN-SEQ'), ('deliver_unknown', 'UNKNOWN-SEQ'),
    ('no_wrap', 'UNKNOWN-SEQ'), ('split_seq', 'UNKNOWN-SEQ'),
    ('conflate_fn', 'LOSS'), ('conceal_loss', 'LOSS'),
    ('extend_deadline', 'WAIT-DEADLINE'), ('infinite_wait', 'WAIT-DEADLINE'),
    ('infinite_pump', 'PUMP-DEADLINE'), ('skip_flood', 'PUMP-DEADLINE'),
])
def test_faults_are_detected(defect, case):
    class Broken(SampleHostNotificationReader):
        def run(self, receive, clock, *, deadline, corr, expected):
            seed = dict(expected)
            if defect == 'skip_flood':
                return {'reply': None, 'notifications': [], 'expected': expected, 'gaps': {fn: 0 for fn in expected}}
            def altered(limit):
                frame = receive(limit + 1 if defect == 'extend_deadline' else limit)
                if frame is None: return None
                if defect == 'unknown_not_counted' and frame[0] == 3 and frame[5] != 1:
                    return b'\xfa' + frame[1:]
                if defect == 'stale_reply' and frame[0] == 2 and frame[1:3] == b'\x08\x00':
                    return frame[:1] + b'\x07\x00' + frame[3:]
                return frame
            effective_clock = (lambda: 0) if (defect == 'infinite_wait' and corr is not None) or (defect == 'infinite_pump' and corr is None) else clock
            result = super().run(altered, effective_clock, deadline=deadline, corr=corr, expected=expected)
            if defect == 'notification_reply' and result['notifications']: result['reply'] = result['notifications'][0]
            if defect == 'deliver_unknown': result['notifications'].append(event(7, 65535, 127))
            if defect == 'no_wrap' and seed.get(7) == 65534: result['expected'][7] += 65536
            if defect == 'split_seq': result['expected'][7] = (result['expected'][7] - 1) & 65535
            if defect == 'conflate_fn' and 8 in expected: result['expected'][8] = result['expected'][7]
            if defect == 'conceal_loss': result['gaps'][7] = 0
            return result
    report = HostNotificationChecks(Broken(), spec=SPEC).run()
    row = next(r for r in report['checks'] if r['id'] == 'HOST-NOTIFY-' + case)
    assert report['status'] == 'failed' and row['status'] == 'failed'
    assert row['error'].startswith('Violation:'), row


def test_short_receive_budgets_do_not_have_to_equal_the_total_deadline():
    class Chunked(SampleHostNotificationReader):
        def run(self, receive, clock, **kwargs):
            return super().run(lambda limit: receive(min(limit, clock() + 2)), clock, **kwargs)
    report = HostNotificationChecks(Chunked(), spec=SPEC).run()
    assert report['status'] == 'passed', report['checks']
