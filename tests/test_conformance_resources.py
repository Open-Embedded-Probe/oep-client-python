"""Common resource verdicts must reject independently injected probe defects."""
import copy
import struct

import pytest

from oep_client.conformance import Checks
from oep_client.conformance_resources import ResourceChecks
from oep_client.conformance_resource_sample import SampleResourceAdapter
from oep_client.virtual_resource_model import ResourceModel
from oep_client.virtual_core import Reject, tlv
from test_conformance import REG


def build():
    now = [0]
    model = ResourceModel(lambda: now[0], boot_id=17)
    c = Checks(model.handle, copy.deepcopy(REG), 'virtual-resource-1')
    a = SampleResourceAdapter(c, functions=(1, 2), kinds=(1, 2), capacity=8)
    runner = ResourceChecks(c, a, wait=lambda seconds: now.__setitem__(0, now[0] + int(seconds * 1000)))
    return model, c, a, runner


def test_resource_contracts_pass_independent_minimal_extension():
    model, c, a, runner = build()
    report = runner.run()
    assert report['status'] == 'passed', [(x['id'], x.get('error')) for x in report['checks']]
    assert len(report['checks']) == 16  # 14 resource contracts + two interface declarations
    assert not report['full_conformance']
    assert model.ep.current_core.holder is None
    assert not model.extension.live
    assert 'subscriptions and notifications' in report['unchecked']


@pytest.mark.parametrize('defect,case', [
    ('retain_release', 'RESOURCE-END'),
    ('retain_release', 'RESOURCE-EXPIRY'),
    ('retain_release', 'RESOURCE-FORCE'),
    ('reuse', 'RESOURCE-ID'),
    ('wrong_cause', 'RESOURCE-WRONG-FN-KIND'),
    ('close_wrong', 'RESOURCE-WRONG-FN-KIND'),
    ('leak_failure', 'RESOURCE-CAPACITY'),
    ('reexecute_create', 'RESOURCE-CREATE-REPLAY'),
    ('reexecute_close', 'RESOURCE-CLOSE-REPLAY'),
    ('release_same_open', 'RESOURCE-OPEN-RETAIN'),
    ('release_open_replay', 'RESOURCE-OPEN-REPLAY'),
])
def test_common_checker_detects_resource_mutants(defect, case):
    model, c, a, runner = build()
    extension = model.extension
    original_dispatch = extension.dispatch
    original_handle = model.ep.current_core.handle
    original_release = extension.release
    if defect == 'retain_release':
        extension.release = lambda: None
    elif defect in ('reuse', 'wrong_cause', 'close_wrong', 'leak_failure'):
        def dispatch(fn, op, payload):
            if defect == 'reuse' and op == 16:
                extension.next_id = 1
            if defect == 'close_wrong' and op == 17:
                rid = struct.unpack_from('<H', payload, 1)[0]
                if rid in extension.live:
                    del extension.live[rid]
                    return b''
            try:
                return original_dispatch(fn, op, payload)
            except Reject as exc:
                if defect == 'wrong_cause' and exc.payload == tlv(1, b'\x06'):
                    raise Reject(10)
                if defect == 'leak_failure' and op == 16:
                    extension.live[60000] = (fn, payload[0])
                raise
        extension.dispatch = dispatch
    else:
        def handle(request, transport):
            _, corr, fn, op, sid = struct.unpack_from('<BHHBI', request)
            core = model.ep.current_core
            replay = sid == core.last and corr in core.cache
            if replay and ((defect == 'reexecute_create' and fn and op == 16) or
                           (defect == 'reexecute_close' and fn and op == 17)):
                core.cache.pop(corr)
                core.high = corr - 1
            if ((defect == 'release_same_open' and not replay) or
                (defect == 'release_open_replay' and replay)) and fn == 0 and op == 16:
                if sid == core.holder:
                    original_release()
            return original_handle(request, transport)
        model.ep.current_core.handle = handle
    report = runner.run()
    row = next(x for x in report['checks'] if x['id'] == case)
    assert row['status'] == 'failed', row
    assert row['exchanges']


def test_reboot_changes_boot_and_invalidates_resources_and_history():
    model, c, a, runner = build()
    c.confirm()
    with c.holding() as sid:
        rid, request = runner.create(sid, a.slots[0])
        model.reboot(18)
        c.session = None  # never end an old session on the rebooted endpoint
        c.rejected(request, 'no_session')
        runner.empty()
        c.confirm()
        assert c.boot == 18
        with c.holding() as new_sid:
            runner.c.rejected(a.request('use', new_sid, a.slots[0], rid), 'no_resource')
            assert runner.create(new_sid, a.slots[0])[0] == 1


def test_model_id_boundary_does_not_claim_65535_real_allocations():
    model, c, a, runner = build()
    c.confirm()
    with c.holding() as sid:
        model.extension.next_id = 65535  # explicit model-only boundary stimulus
        rid, _ = runner.create(sid, a.slots[0])
        assert rid == 65535
        runner.cause(a.request('create', sid, a.slots[1]), 2)
        runner.use(sid, a.slots[0], rid)
        runner.close(sid, a.slots[0], rid)
        runner.cause(a.request('create', sid, a.slots[0]), 2)
        runner.empty()


def test_wrong_explicit_unit_blocks_all_resource_operations():
    model, c, a, runner = build()
    c.unit = 'other-unit'
    report = runner.run()
    assert report['checks'][0]['status'] == 'failed'
    assert all(x['status'] == 'blocked' for x in report['checks'][1:])
    assert model.ep.current_core.holder is None


def test_invalid_interface_declaration_blocks_mutation():
    model, c, a, runner = build()
    model.extension.describe = lambda fn: (tlv(7, b'\x00\x08'),)  # unassigned common op 3
    report = runner.run()
    identity = next(x for x in report['checks'] if x['id'] == 'RESOURCE-IDENTITY')
    assert identity['status'] == 'failed'
    assert all(x['status'] == 'blocked' for x in report['checks']
               if x['id'].startswith('RESOURCE-') and x['id'] != 'RESOURCE-IDENTITY')
    assert model.ep.current_core.holder is None


def test_adapter_without_cross_fn_coverage_is_configuration_failure():
    model, c, a, runner = build()
    a.slots = ((1, 1), (1, 2), (1, 3))
    report = runner.run()
    identity = next(x for x in report['checks'] if x['id'] == 'RESOURCE-IDENTITY')
    assert identity['status'] == 'failed'
    assert model.ep.current_core.holder is None


def test_sample_extension_also_passes_existing_core_contracts(monkeypatch):
    now = [0]
    monkeypatch.setattr('oep_client.conformance.time.sleep',
                        lambda seconds: now.__setitem__(0, now[0] + int(seconds * 1000)))
    model = ResourceModel(lambda: now[0], boot_id=17)
    report = Checks(model.handle, copy.deepcopy(REG), 'virtual-resource-1').run()
    assert report['status'] == 'passed', [(x['id'], x.get('error')) for x in report['checks']]
    assert len(report['checks']) == 49
    assert model.ep.current_core.holder is None
