"""Subscription verdicts and raw event records, independent from the probe model."""
import copy
import struct

import pytest

from oep_client.conformance import Checks
from oep_client.conformance_subscriptions import SubscriptionChecks, SUBSCRIPTION_CASES
from oep_client.conformance_subscription_sample import SampleSubscriptionAdapter
from oep_client.virtual_notification_model import NotificationModel
from oep_client.conformance_resources import ResourceChecks
from oep_client.conformance_resource_sample import SampleResourceAdapter
from test_conformance import REG


def build():
    now = [0]
    model = NotificationModel(lambda: now[0], boot_id=17)
    c = Checks(model.handle, copy.deepcopy(REG), 'virtual-notify-1')
    adapter = SampleSubscriptionAdapter(model, functions=(1, 2), observation_ms=50,
                                        wait=lambda seconds: now.__setitem__(0, now[0] + int(seconds * 1000)))
    runner = SubscriptionChecks(c, adapter, wait=lambda seconds: now.__setitem__(0, now[0] + int(seconds * 1000)))
    return model, c, adapter, runner


def test_common_subscriptions_pass_virtual_extension():
    model, c, adapter, runner = build()
    report = runner.run()
    assert report['status'] == 'passed', [(x['id'], x.get('error')) for x in report['checks']]
    assert len(report['checks']) == 18  # identity + 15 cases + two declaration checks
    assert not report['full_conformance']
    assert model.ep.current_core.holder is None
    assert not model.extension.subscriptions
    assert 'response priority' in report['unchecked']
    assert runner.notifications and all('elapsed_ns' in row for row in runner.notifications)


@pytest.mark.parametrize('defect,case', [
    ('retain_release', 'IF-SUB-END'),
    ('retain_release', 'IF-SUB-EXPIRY'),
    ('retain_release', 'IF-SUB-FORCE'),
    ('no_reset', 'IF-SUB-REPLACE'),
    ('duplicate', 'IF-SUB-REPLACE'),
    ('global_seq', 'IF-SUB-FN'),
    ('ignore_unsubscribe', 'IF-SUB-UNSUBSCRIBE'),
    ('threshold_events', 'IF-SUB-EVENT-THRESHOLD'),
    ('reexecute_subscribe', 'IF-SUB-REPLAY'),
    ('reexecute_unsubscribe', 'IF-SUB-UNSUBSCRIBE-REPLAY'),
    ('release_same_open', 'IF-SUB-OPEN'),
    ('release_open_replay', 'IF-SUB-OPEN-REPLAY'),
    ('clear_before_validation', 'IF-SUB-INVALID'),
])
def test_checker_detects_subscription_mutants(defect, case):
    model, c, adapter, runner = build()
    extension = model.extension
    dispatch = extension.dispatch
    emit = extension.emit
    handle = model.ep.current_core.handle
    if defect == 'retain_release':
        extension.release = lambda: extension.live.clear()
    elif defect in ('no_reset', 'ignore_unsubscribe'):
        def altered(fn, op, payload):
            if op == 1 and defect == 'no_reset' and fn in extension.subscriptions:
                return b''
            if op == 2 and defect == 'ignore_unsubscribe':
                return b''
            return dispatch(fn, op, payload)
        extension.dispatch = altered
    elif defect in ('duplicate', 'global_seq', 'threshold_events'):
        def altered(fn, marker):
            if defect == 'global_seq' and fn in extension.subscriptions:
                extension.subscriptions[fn] = max(extension.subscriptions.values())
            if defect == 'threshold_events' and extension.conditions.get(fn, (0, 0)) != (0, 0):
                return
            emit(fn, marker)
            if defect == 'duplicate' and extension.conditions.get(fn, (0, 0)) != (0, 0):
                extension.pending.append(extension.pending[-1])
        extension.emit = altered
    else:
        def altered(request, transport):
            _, corr, fn, op, sid = struct.unpack_from('<BHHBI', request)
            core = model.ep.current_core
            if defect == 'clear_before_validation' and fn and op == 1:
                extension.subscriptions.pop(fn, None)
            replay = sid == core.last and corr in core.cache
            if replay and fn and ((op == 1 and defect == 'reexecute_subscribe') or
                                  (op == 2 and defect == 'reexecute_unsubscribe')):
                core.cache.pop(corr)
                core.high = corr - 1
            if fn == 0 and op == 16 and sid == core.holder and (
                (defect == 'release_same_open' and not replay) or
                (defect == 'release_open_replay' and replay)):
                extension.subscriptions.clear()
            return handle(request, transport)
        model.ep.current_core.handle = altered
    report = runner.run()
    row = next(x for x in report['checks'] if x['id'] == case)
    assert row['status'] == 'failed', row
    assert row['notification_observations']


@pytest.mark.parametrize('corrupt', ['fn_zero', 'unknown_fn', 'wrong_seq', 'role', 'kind', 'marker',
                                   'truncated', 'oversized', 'critical_tlv'])
def test_raw_notification_corruption_is_retained(corrupt):
    model, c, adapter, runner = build()
    receive = adapter.receive
    def altered(timeout_ms):
        frames = receive(timeout_ms)
        if frames:
            frame = bytearray(frames[0])
            if corrupt == 'fn_zero':
                frame[1:3] = b'\0\0'
            elif corrupt == 'unknown_fn':
                frame[1:3] = b'\xff\xff'
            elif corrupt == 'wrong_seq':
                frame[3:5] = b'\xff\xff'
            elif corrupt == 'role':
                frame[0] = 2
            elif corrupt == 'kind':
                frame[5] = 127
            elif corrupt == 'marker':
                frame[6:10] = b'\0\0\0\0'
            elif corrupt == 'truncated':
                frame = frame[:4]
            elif corrupt == 'oversized':
                frame += b'\0' * 65
            else:
                frame += b'\x81\0\0'  # producer must not emit critical-bit TLV
            return [bytes(frame)]
        return frames
    adapter.receive = altered
    report = runner.run()
    row = next(x for x in report['checks'] if x['id'] == 'IF-SUB-OWNER')
    assert row['status'] == 'failed'
    observation = row['notification_observations'][-1]
    assert observation['frames_hex'] and observation['error']


def test_disconnect_blocks_remaining_checks_and_retains_observation():
    model, c, adapter, runner = build()
    adapter.receive = lambda ms: (_ for _ in ()).throw(OSError('disconnected'))
    report = runner.run()
    first = next(x for x in report['checks'] if x['id'] == 'IF-SUB-NONE')
    assert first['status'] == 'failed'
    assert 'disconnected' in first['notification_observations'][0]['error']
    assert all(x['status'] == 'blocked' for x in report['checks'] if x['id'] in
               {name for name, _ in SUBSCRIPTION_CASES[1:]})


def test_wrong_unit_blocks_subscription_operations():
    model, c, adapter, runner = build()
    c.unit = 'different-unit'
    report = runner.run()
    assert report['checks'][0]['status'] == 'failed'
    assert model.ep.current_core.holder is None
    assert not runner.notifications


def test_notification_extension_keeps_core_and_resource_contracts(monkeypatch):
    now = [0]
    wait = lambda seconds: now.__setitem__(0, now[0] + int(seconds * 1000))
    monkeypatch.setattr('oep_client.conformance.time.sleep', wait)
    model = NotificationModel(lambda: now[0], boot_id=17)
    c = Checks(model.handle, copy.deepcopy(REG), 'virtual-notify-1')
    core = c.run()
    assert core['status'] == 'passed', [(x['id'], x.get('error')) for x in core['checks']]
    assert len(core['checks']) == 49
    c = Checks(model.handle, copy.deepcopy(REG), 'virtual-notify-1')
    adapter = SampleResourceAdapter(c, functions=(1, 2), kinds=(1, 2), capacity=8)
    resources = ResourceChecks(c, adapter, wait=wait).run()
    assert resources['status'] == 'passed', [(x['id'], x.get('error')) for x in resources['checks']]
    assert len(resources['checks']) == 16


def test_reboot_invalidates_subscription_and_old_cached_subscribe():
    model, c, adapter, runner = build()
    c.confirm()
    with c.holding() as sid:
        request = runner.subscribe(sid, 1)
        runner.event(1, 0)
        model.reboot(18)
        c.session = None
        c.rejected(request, 'no_session')
        runner.event(1)
        c.confirm()
        assert c.boot == 18
        with c.holding() as new_sid:
            runner.event(1)
            runner.subscribe(new_sid, 1)
            runner.event(1, 0)


@pytest.mark.parametrize('bad_config', ['missing_pair', 'absent_fn', 'window', 'unit'])
def test_explicit_bad_configuration_blocks_stimulus_and_subscribe(bad_config):
    model, c, adapter, runner = build()
    if bad_config == 'missing_pair':
        original = model.extension.describe
        model.extension.describe = lambda fn: (b'\x07\x02\0\x10\x0f',) if fn == 1 else original(fn)
    elif bad_config == 'absent_fn':
        adapter.functions = (1, 65535)
    elif bad_config == 'window':
        adapter.observation_ms = 0
    else:
        c.unit = 'other'
    report = runner.run()
    row = next(x for x in report['checks'] if x['id'] == 'IF-SUB-IDENTITY')
    assert row['status'] == 'failed'
    assert not runner.notifications
    assert model.ep.current_core.holder is None
