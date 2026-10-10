import struct
import pytest
from oep_client.conformance_corr import CorrChecks
from test_conformance_retention import build


def setup():
    now, model, adapter, runner = build()
    return model, CorrChecks(runner.c, adapter)


def test_corr_boundaries_pass():
    model, runner = setup()
    report = runner.run()
    assert report['status'] == 'passed', [(row['id'], row.get('error')) for row in report['checks']]
    assert len(report['checks']) == 17
    assert not report['full_conformance']
    assert model.ep.current_core.holder is None


@pytest.mark.parametrize('defect', ['signed_forward', 'wrapped_old', 'reject_max', 'zero_after_replay', 'free_high', 'new_session_high'])
def test_corr_mutants_fail(defect):
    model, runner = setup()
    core = model.ep.current_core
    handle = core.handle
    def altered(data, transport, *, admission_reason=None, defer_send=False):
        _, corr, fn, op, sid = struct.unpack_from('<BHHBI', data)
        if defect == 'signed_forward' and sid == core.last and corr > core.high and corr-core.high >= 32768:
            return struct.pack('<BHBB', 2, corr, 0, 12)
        if defect == 'wrapped_old' and sid == core.last and corr and corr < core.high and core.high-corr > 32768:
            core.cache.pop(corr, None); core.high = corr-1
        if defect == 'reject_max' and corr == 65535 and sid:
            return struct.pack('<BHBB', 2, corr, 0, 3)
        if defect == 'zero_after_replay' and corr == 0 and sid == core.last:
            return struct.pack('<BHBB', 2, corr, 0, 12)
        old_high, old_last = core.high, core.last
        result = handle(data, transport, admission_reason=admission_reason, defer_send=defer_send)
        if defect == 'free_high' and not sid and corr == 65535: core.high = corr
        if defect == 'new_session_high' and sid and old_last and sid != old_last and op == 16 and old_high == 65535:
            core.high = max(core.high, old_high)
        return result
    core.handle = altered
    report = runner.run()
    assert report['status'] == 'failed'
    assert any(row['id'].startswith('CORE-CORR-') and row['status'] == 'failed' for row in report['checks']), report


@pytest.mark.parametrize('session', [0, 17])
def test_generator_exhaustion_never_wraps_or_changes_counter(session):
    model, runner = setup()
    c = runner.c
    attribute = 'corr' if session else 'zero_corr'
    setattr(c, attribute, 65534)
    last = c.request(c.core['end'] if session else c.core['clock'], session=session)
    assert struct.unpack_from('<H', last, 1)[0] == 65535
    for _ in range(2):
        with pytest.raises(ValueError, match='corr exhausted'): c.request(c.core['clock'], session=session)
        assert getattr(c, attribute) == 65535
    assert c.trace == [] and model.ep.current_core.last is None


def test_invalid_header_does_not_consume_automatic_corr():
    _, runner = setup()
    with pytest.raises(struct.error): runner.c.request(256, session=17)
    assert runner.c.corr == 0
