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
    assert len(report['checks']) == 21
    assert not report['full_conformance']
    assert model.ep.current_core.holder is None


@pytest.mark.parametrize('defect', ['unsigned_forward', 'no_wrap', 'reject_max', 'zero_after_replay', 'free_high', 'new_session_high'])
def test_corr_mutants_fail(defect):
    """Each probe defect against the cyclic corr16 rules (core §4.1, §5.1) is caught by some check."""
    model, runner = setup()
    core = model.ep.current_core
    handle = core.handle
    def altered(data, transport, *, admission_reason=None, defer_send=False):
        _, corr, fn, op, sid = struct.unpack_from('<BHHBI', data)
        if defect == 'unsigned_forward' and sid == core.last and corr > core.high and (corr - core.high) % 65536 >= 32768:
            core.high = corr - 1                                  # takes half the space or more ahead as new
        if defect == 'no_wrap' and sid == core.last and corr and core.high and corr < core.high and op != 16:
            return struct.pack('<BHBB', 2, corr, 0, 12)           # never wraps: a smaller corr is always old
        if defect == 'reject_max' and corr == 65535 and sid:
            return struct.pack('<BHBB', 2, corr, 0, 3)
        if defect == 'zero_after_replay' and corr == 0 and sid == core.last:
            return struct.pack('<BHBB', 2, corr, 0, 12)
        old_high, old_last = core.high, core.last
        result = handle(data, transport, admission_reason=admission_reason, defer_send=defer_send)
        if defect == 'free_high' and not sid and corr == 65535: core.high = corr
        if defect == 'new_session_high' and sid and old_last and sid != old_last and op == 16 and old_high:
            core.high = old_high                                  # a new id's open keeps the old last corr
        return result
    core.handle = altered
    report = runner.run()
    assert report['status'] == 'failed'
    expected = 'CORE-RETENTION-SESSION-ZERO' if defect == 'free_high' else 'CORE-'
    assert any(row['id'].startswith(expected) and row['status'] == 'failed' for row in report['checks']), report


@pytest.mark.parametrize('session', [0, 17])
def test_the_generator_wraps_past_65535_to_1(session):
    """core §4.1: corr cycles, 65535 is followed by 1 and 0 is never used; nothing is sent by building requests."""
    model, runner = setup()
    c = runner.c
    attribute = 'corr' if session else 'zero_corr'
    setattr(c, attribute, 65534)
    assert struct.unpack_from('<H', c.request(c.core['clock'], session=session), 1)[0] == 65535
    assert struct.unpack_from('<H', c.request(c.core['clock'], session=session), 1)[0] == 1
    assert getattr(c, attribute) == 1 and c.trace == [] and model.ep.current_core.last is None


def test_invalid_header_does_not_consume_automatic_corr():
    _, runner = setup()
    with pytest.raises(struct.error): runner.c.request(256, session=17)
    assert runner.c.corr == 0
