"""Optional explicitly configured probe contract; independent of the legacy board table."""
import json
import os
from pathlib import Path

import pytest

from oep_client import hardware


@pytest.mark.equipment
def test_probe_declarations(capsys, request):
    path = os.environ.get('OEP_HW_CONFIG')
    if not path:
        pytest.skip('OEP_HW_CONFIG is not set: optional equipment contract')
    equipment = hardware.load(path)  # explicit invalid input is an error, never a skip
    virtual = all(node['transport']['kind'] == 'virtual' for node in equipment.data['probes'])
    command = 'smoke' if virtual else 'preflight'
    if not os.environ.get('OEP_HW_RESULTS'):
        pytest.fail('equipment contract requires OEP_HW_RESULTS for evidence')
    exit_code = hardware.main([command, '--config', path])
    output = capsys.readouterr()
    assert exit_code == 0, output.err + output.out
    artifact = Path(output.out.strip())
    report = json.loads(artifact.read_text())
    record_property = request.getfixturevalue('record_property')
    record_property('oep_contract', 'PROBE-DECLARE')
    record_property('oep_scope', report['scope'])
    record_property('oep_evidence', str(artifact))
    assert report['status'] == 'passed'
    assert report['probes'] and all(p['session_released'] for p in report['probes'])
