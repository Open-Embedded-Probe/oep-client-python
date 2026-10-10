"""Optional pytest adapter for the independent checker (pytest is a test dependency)."""
import json
import os
from pathlib import Path

import pytest

from .conformance import main, CORE_CASES, SERIAL_CASES, RECONNECT_CASES, TCP_CASES, TCP_PEER_CASES

if os.environ.get("OEP_CONFORMANCE_FRAMING") == "serial":
    CORE_CASES += tuple(name for name, _ in SERIAL_CASES + RECONNECT_CASES)

elif os.environ.get('OEP_CONFORMANCE_FRAMING') == 'tcp':
    CORE_CASES += tuple(name for name, _ in TCP_CASES)
    if os.environ.get('OEP_CONFORMANCE_TCP_PEER'):
        CORE_CASES += tuple(name for name, _ in TCP_PEER_CASES)


@pytest.fixture(scope='module')
def oep_conformance_report():
    keys = ('OEP_CONFORMANCE_ADDRESS', 'OEP_CONFORMANCE_UNIT_ID', 'OEP_CONFORMANCE_SPEC',
            'OEP_CONFORMANCE_OUT', 'OEP_HW_LOCK')
    if not any(os.environ.get(key) for key in keys[:-1] + ('OEP_CONFORMANCE_FRAMING', 'OEP_CONFORMANCE_TCP_PEER')):
        pytest.skip('no explicit OEP conformance equipment/settings supplied')
    missing = [key for key in keys if not os.environ.get(key)]
    if missing:
        pytest.fail('incomplete explicit conformance configuration: ' + ', '.join(missing))
    main([])  # Preserve individual FAILs below; main always writes the reserved new artifact.
    report = json.loads(Path(os.environ['OEP_CONFORMANCE_OUT']).read_text())
    if 'checks' not in report:
        pytest.fail(report.get('error', 'runner produced no checks'))
    return report


def assert_case(report, case, record_property):
    rows = [row for row in report['checks'] if row['id'] == case]
    record_property('oep_evidence', os.environ['OEP_CONFORMANCE_OUT'])
    record_property('oep_spec_commit', report['spec']['commit'])
    record_property('oep_level', 'interface' if case.startswith('IF-') else 'core')
    assert len(rows) == 1, f'missing or duplicate contract result: {case}'
    if rows[0]['status'] == 'not_applicable':
        pytest.skip('not applicable: ' + rows[0]['reason'])
    assert rows[0]['status'] == 'passed', rows[0].get('error', rows[0]['status'])


def assert_interfaces(report, record_property):
    rows = [row for row in report['checks'] if row['level'] == 'interface']
    record_property('oep_evidence', os.environ['OEP_CONFORMANCE_OUT'])
    record_property('oep_spec_commit', report['spec']['commit'])
    record_property('oep_level', 'interface')
    failures = []
    for row in rows:
        record_property(row['id'], row['status'])
        if row['status'] != 'passed':
            failures.append(row['id'] + ': ' + row.get('error', row['status']))
    # Zero declared interfaces is valid; failed discovery is not an empty success.
    discovery = [row for row in report['checks'] if row['id'] == 'CORE-LIST']
    if len(discovery) != 1 or discovery[0]['status'] != 'passed':
        failures.append('CORE-LIST did not complete successfully')
    assert not failures, '\n'.join(failures)
