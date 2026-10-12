"""Legacy checks must refuse the new draft before observing any equipment."""
import copy

import pytest

from oep_client import conformance, link
from test_conformance import REG


def test_cyclic_contract_is_refused_before_exchange():
    registry = copy.deepcopy(REG)
    registry['limits'] = {'corr_half_range': 32768}
    calls = []
    with pytest.raises(conformance.Violation, match='checker migration required'):
        conformance.Checks(lambda request: calls.append(request), registry, 'unit')
    assert calls == []


def test_unreviewed_core_is_refused_before_cli_opens_equipment(monkeypatch, tmp_path):
    monkeypatch.setattr(conformance, 'spec_identity', lambda path: (REG, {'core_sha256': 'unreviewed'}))
    calls = []
    monkeypatch.setattr(link, 'open_serial', lambda address: calls.append(address))
    output = tmp_path / 'report.json'
    with pytest.raises(SystemExit) as exc:
        conformance.main(['--address', '/dev/never-open', '--unit', 'unit', '--spec', 'unused',
                          '--out', str(output), '--lock', str(tmp_path / 'unused.lock')])
    assert exc.value.code == 2 and calls == [] and not output.exists()


def test_core_metadata_does_not_require_standard_oep_interface_files(monkeypatch, tmp_path):
    (tmp_path / 'registry').mkdir(); (tmp_path / 'docs').mkdir()
    (tmp_path / 'registry/oep-v1.toml').write_text('schema = 1\n')
    (tmp_path / 'docs/oep-core.ja.md').write_text('metadata fixture\n')
    monkeypatch.setattr(conformance.subprocess, 'check_output',
                        lambda command, **kwargs: 'fixture\n' if 'rev-parse' in command else '')
    registry, identity = conformance.spec_identity(tmp_path)
    assert registry == {'schema': 1} and identity['interface_sha256'] == {}
