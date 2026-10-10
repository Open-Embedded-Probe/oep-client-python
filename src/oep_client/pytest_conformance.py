"""Optional pytest adapter for the independent checker (pytest is a test dependency)."""
import json
import os
from pathlib import Path

import pytest

from .conformance import main

CORE_CASES = (
    'CORE-CONFIRM', 'CORE-IDENTITY', 'CORE-DECLARE', 'CORE-LIST', 'CORE-CLOCK',
    'CORE-ZERO-CORR', 'CORE-ZERO-SESSION', 'CORE-SESSION-REQUIRED', 'CORE-TLV-ZERO',
    'CORE-KEEPALIVE-LOCK', 'CORE-LEASE-MIN', 'CORE-LEASE-MAX', 'CORE-OPEN-AFTER-END',
    'CORE-REPLAY', 'CORE-OPEN-HISTORY', 'CORE-ALTERED-REPLAY', 'CORE-CORR-U16',
    'CORE-CORR-MAX', 'CORE-END', 'CORE-LEASE', 'CORE-REPLAY-LEASE',
)


@pytest.fixture(scope='module')
def oep_conformance_report():
    keys = ('OEP_CONFORMANCE_ADDRESS', 'OEP_CONFORMANCE_UNIT_ID', 'OEP_CONFORMANCE_SPEC',
            'OEP_CONFORMANCE_OUT', 'OEP_HW_LOCK')
    if not any(os.environ.get(key) for key in keys[:-1]):
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
    assert rows[0]['status'] == 'passed', rows[0].get('error', rows[0]['status'])
