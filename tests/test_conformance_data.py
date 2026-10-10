"""SPEC-derived data verdicts must reject independently injected violations."""
import copy
import struct

import pytest

from oep_client.conformance import Checks
from oep_client.conformance_data import DataChecks
from oep_client.conformance_data_sample import SampleDataAdapter
from oep_client.virtual_data_model import DataModel
from test_conformance import REG


def build():
    now = [0]
    wait = lambda seconds: now.__setitem__(0, now[0] + int(seconds * 1000))
    model = DataModel(lambda: now[0], boot_id=17)
    checks = Checks(model.handle, copy.deepcopy(REG), 'virtual-stream-1')
    adapter = SampleDataAdapter(model, functions=(1, 2), observation_ms=50, wait=wait, loss_free=True)
    return model, checks, adapter, DataChecks(checks, adapter, wait=wait, clock_ms=lambda: now[0])


def test_data_contracts_and_subscription_regression_pass():
    model, checks, adapter, runner = build()
    report = runner.run()
    assert report['status'] == 'passed', [(x['id'], x.get('error')) for x in report['checks']]
    assert len(report['checks']) == 33
    assert not report['full_conformance']
    assert model.ep.current_core.holder is None
    assert not model.extension.buffers


@pytest.mark.parametrize('defect,case', [
    ('zero_min', 'IF-DATA-DELAY'),
    ('zero_delay', 'IF-DATA-MINIMUM'),
    ('and_conditions', 'IF-DATA-EITHER'),
    ('reset_first', 'IF-DATA-FIRST-BYTE'),
    ('separate_seq', 'IF-DATA-SHARED-SEQ'),
    ('event_flush', 'IF-DATA-EVENT-BYTES'),
    ('stale_timer', 'IF-DATA-NEXT-BATCH'),
    ('no_split', 'IF-DATA-SPLIT'),
    ('wrong_position', 'IF-DATA-IMMEDIATE'),
    ('duplicate_bytes', 'IF-DATA-IMMEDIATE'),
    ('reexecute_subscribe', 'IF-DATA-REPLAY'),
    ('retain_buffer', 'IF-DATA-END'),
    ('release_same_open', 'IF-DATA-OPEN'),
    ('release_open_replay', 'IF-DATA-OPEN-REPLAY'),
])
def test_mutants(defect, case):
    model, checks, adapter, runner = build()
    extension = model.extension
    pump, feed, flush = extension.pump, extension.feed, extension.flush
    emit = extension.emit
    handle = model.ep.current_core.handle
    if defect in ('zero_min', 'zero_delay', 'and_conditions'):
        def altered():
            for fn in list(extension.buffers):
                minimum, delay = extension.conditions[fn]
                position, data, first = extension.buffers[fn]
                age = extension.now() - first
                if defect == 'zero_min':
                    ready = len(data) >= minimum or (delay and age >= delay)
                elif defect == 'zero_delay':
                    ready = (minimum and len(data) >= minimum) or age >= delay
                else:
                    ready = (not minimum or len(data) >= minimum) and (not delay or age >= delay)
                if ready:
                    flush(fn)
        extension.pump = altered
    elif defect == 'reset_first':
        def altered(fn, position, data):
            feed(fn, position, data)
            if fn in extension.buffers:
                start, buffered, first = extension.buffers[fn]
                extension.buffers[fn] = (start, buffered, extension.now())
        extension.feed = altered
    elif defect == 'event_flush':
        def altered(fn, marker):
            emit(fn, marker)
            if fn in extension.buffers:
                flush(fn)
        extension.emit = altered
    elif defect == 'separate_seq':
        data_seq = {}
        def altered(fn):
            saved = extension.subscriptions[fn]
            extension.subscriptions[fn] = data_seq.get(fn, 0)
            flush(fn)
            data_seq[fn] = extension.subscriptions[fn]
            extension.subscriptions[fn] = saved
        extension.flush = altered
    elif defect == 'stale_timer':
        firsts = {}
        def altered(fn, position, data):
            feed(fn, position, data)
            if fn in extension.buffers:
                start, buffered, first = extension.buffers[fn]
                firsts.setdefault(fn, first)
                extension.buffers[fn] = (start, buffered, firsts[fn])
                pump()
        extension.feed = altered
    elif defect in ('no_split', 'wrong_position', 'duplicate_bytes'):
        def altered(fn):
            start = len(extension.pending)
            if defect == 'no_split':
                extension.frame_size = 65535
            flush(fn)
            for at in range(start, len(extension.pending)):
                frame = bytearray(extension.pending[at])
                if defect == 'wrong_position':
                    frame[5:13] = b'\xff' * 8
                elif defect == 'duplicate_bytes':
                    frame[15:] *= 2
                    struct.pack_into('<H', frame, 13, len(frame) - 15)
                extension.pending[at] = bytes(frame)
        extension.flush = altered
    elif defect == 'retain_buffer':
        release = extension.release
        def altered():
            saved = dict(extension.buffers)
            release()
            extension.buffers.update(saved)
        extension.release = altered
        dispatch = extension.dispatch
        def retained_subscription(fn, op, payload):
            saved = extension.buffers.get(fn)
            result = dispatch(fn, op, payload)
            if op == 1 and saved is not None:
                extension.buffers[fn] = saved
            return result
        extension.dispatch = retained_subscription
    else:
        def altered(request, transport):
            _, corr, fn, op, sid = struct.unpack_from('<BHHBI', request)
            core = model.ep.current_core
            replay = sid == core.last and corr in core.cache
            if fn == 0 and op == 16 and sid == core.holder and (
                    (defect == 'release_same_open' and not replay) or
                    (defect == 'release_open_replay' and replay)):
                extension.buffers.clear()
            if fn and op == 1 and sid == core.last and corr in core.cache:
                core.cache.pop(corr)
                core.high = corr - 1
            return handle(request, transport)
        model.ep.current_core.handle = altered
    report = runner.run()
    row = next(x for x in report['checks'] if x['id'] == case)
    assert row['status'] == 'failed', row
    assert row['notification_observations']


def test_literal_data_header_uses_u64_position_and_u16_length():
    model, checks, adapter, runner = build()
    checks.max_frame = 512
    data = bytes(range(256))
    frame = bytes.fromhex('04 01 00 ff ff 01 00 00 00 01 00 00 00 00 01') + data
    assert runner.decode(frame) == ('data', 1, 65535, 0x100000001, data)


@pytest.mark.parametrize('corrupt', ['fn_zero', 'unknown_fn', 'seq', 'role', 'length', 'truncated',
                                   'critical_tlv', 'position', 'bytes'])
def test_raw_data_corruption_remains_in_report(corrupt):
    model, checks, adapter, runner = build()
    receive = adapter.receive
    def altered(timeout_ms):
        frames = receive(timeout_ms)
        if frames and frames[0][0] == 4:
            frame = bytearray(frames[0])
            if corrupt == 'fn_zero':
                frame[1:3] = b'\0\0'
            elif corrupt == 'unknown_fn':
                frame[1:3] = b'\xff\xff'
            elif corrupt == 'seq':
                frame[3:5] = b'\xff\xff'
            elif corrupt == 'role':
                frame[0] = 2
            elif corrupt == 'length':
                frame[13:15] = b'\xff\xff'
            elif corrupt == 'truncated':
                frame = frame[:14]
            elif corrupt == 'critical_tlv':
                frame += b'\xfe\0\0'
            elif corrupt == 'position':
                frame[5:13] = b'\xff' * 8
            else:
                frame[15:] = b'!' * len(frame[15:])
            return [bytes(frame)] + frames[1:]
        return frames
    adapter.receive = altered
    report = runner.run()
    row = next(x for x in report['checks'] if x['id'] == 'IF-DATA-IMMEDIATE')
    assert row['status'] == 'failed'
    assert any(x.get('frames_hex') for x in row['notification_observations'])


def test_missing_explicit_data_capability_blocks_all_stimulus():
    model, checks, adapter, runner = build()
    adapter.feed = None
    report = runner.run()
    assert report['checks'][0]['status'] == 'failed'
    assert not runner.notifications
    assert model.ep.current_core.holder is None


def test_data_stimulus_disconnect_blocks_following_checks():
    model, checks, adapter, runner = build()
    adapter.feed = lambda fn, data: (_ for _ in ()).throw(OSError('source disconnected'))
    report = runner.run()
    row = next(x for x in report['checks'] if x['id'] == 'IF-DATA-IMMEDIATE')
    assert row['status'] == 'failed'
    assert 'source disconnected' in row['notification_observations'][0]['error']
    assert all(x['status'] == 'blocked' for x in report['checks']
               if x['id'].startswith('IF-DATA-') and x['id'] not in ('IF-DATA-ADAPTER', 'IF-DATA-IMMEDIATE'))


def test_model_actual_seq_wrap_with_65538_mixed_notifications():
    # Actual notification generation, not a seeded counter or hardware claim.
    model, checks, adapter, runner = build()
    checks.confirm()
    with checks.holding(60000) as sid:
        runner.subscribe(sid, 1)
        for index in range(65538):
            if index % 2:
                model.emit(1, index)
                expected = ('event', 1, index & 65535, index)
            else:
                position = adapter.feed(1, b'x')
                expected = ('data', 1, index & 65535, position, b'x')
            frames = model.drain()  # in-process delivery, no transport/real-clock claim
            assert len(frames) == 1
            assert runner.decode(frames[0]) == expected


def test_data_model_preserves_existing_core_and_resource_contracts(monkeypatch):
    from oep_client.conformance_resources import ResourceChecks
    from oep_client.conformance_resource_sample import SampleResourceAdapter
    now = [0]
    wait = lambda seconds: now.__setitem__(0, now[0] + int(seconds * 1000))
    monkeypatch.setattr('oep_client.conformance.time.sleep', wait)
    model = DataModel(lambda: now[0], boot_id=17)
    checks = Checks(model.handle, copy.deepcopy(REG), 'virtual-stream-1')
    core = checks.run()
    assert core['status'] == 'passed', [(x['id'], x.get('error')) for x in core['checks']]
    assert len(core['checks']) == 49
    checks = Checks(model.handle, copy.deepcopy(REG), 'virtual-stream-1')
    adapter = SampleResourceAdapter(checks, functions=(1, 2), kinds=(1, 2), capacity=8)
    resources = ResourceChecks(checks, adapter, wait=wait).run()
    assert resources['status'] == 'passed', [(x['id'], x.get('error')) for x in resources['checks']]
    assert len(resources['checks']) == 16


def test_reboot_discards_pending_and_unflushed_data():
    model, checks, adapter, runner = build()
    checks.confirm()
    with checks.holding(60000) as sid:
        runner.subscribe(sid, 1)
        adapter.feed(1, b'queued')  # already generated but not observed
        runner.subscribe(sid, 2, 20, 0)
        adapter.feed(2, b'buffered')
        model.reboot(18)
        checks.session = None
        assert model.drain() == []
        assert not model.extension.buffers
        checks.confirm()
        assert checks.boot == 18
        with checks.holding(60000) as new_sid:
            runner.subscribe(new_sid, 1)
            position = runner.feed(1, b'fresh')
            runner.expect_data(1, position, b'fresh', 0)


def test_late_observation_must_not_claim_first_byte_deadline_pass():
    model, checks, adapter, runner = build()
    checks.confirm()
    # Notifications are correct, but a late observer cannot separate the two deadlines.
    times = iter((0, 230, 650))
    runner.clock_ms = lambda: next(times)
    checks.check('IF-DATA-FIRST-BYTE', 'core §11.3', runner.first_byte)
    assert checks.results[-1]['status'] == 'failed'
    assert 'cannot distinguish' in checks.results[-1]['error']
    assert runner.notifications[-1]['last_age_ms'] == 420


def test_loss_free_fixture_condition_must_be_explicit():
    model, checks, adapter, runner = build()
    adapter.loss_free = False
    report = runner.run()
    assert report['checks'][0]['status'] == 'failed'
    assert 'loss-free' in report['checks'][0]['error']
    assert not runner.notifications
    assert model.ep.current_core.holder is None
