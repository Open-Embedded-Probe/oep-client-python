import copy
from oep_client.conformance import Checks
from oep_client.conformance_lease import LeaseChecks
from oep_client.conformance_lease_sample import SampleLeaseAdapter
from oep_client.virtual_pipeline_model import PipelineModel
from test_conformance import REG


def build():
    now = [0]
    model = PipelineModel(lambda: now[0], boot_id=17)
    adapter = SampleLeaseAdapter(model, clock_ms=lambda: now[0], wait_ms=lambda ms: now.__setitem__(0, now[0] + ms))
    checks = Checks(lambda req: adapter.exchange(adapter.primary, req), copy.deepcopy(REG), 'virtual-routes-1')
    return now, model, adapter, LeaseChecks(checks, adapter)


def test_lease_writer():
    now, model, adapter, runner = build()
    report = runner.run()
    assert report['status'] == 'passed', [(x['id'], x.get('error')) for x in report['checks']]
    assert len(report['checks']) == 22
    assert not report['full_conformance']
    assert model.ep.current_core.holder is None

import pytest
import struct
import secrets


@pytest.mark.parametrize('defect', ['send_time', 'partial', 'replay', 'no_rejected', 'freeze_wait', 'stale'])
def test_lease_mutants_fail(defect):
    """core §6.1 (a09fa45) mutants: the lease restarted at the write instead of the answer's generation (send_time),
    renewed by a partial write or by the write's completion (stale), by a replay, not by a rejected answer, or
    an expiry frozen while an answer waits for the writer - each is caught by a CORE-LEASE check."""
    now, model, adapter, runner = build()
    core = model.ep.current_core
    handle, write, sent, tick = core.handle, model.write, core.response_sent, core.tick
    if defect in ('send_time', 'replay', 'no_rejected'):
        def altered(data, transport, *, admission_reason=None, defer_send=False):
            sid = struct.unpack_from('<I', data, 6)[0]
            corr = struct.unpack_from('<H', data, 1)[0]
            cached = sid != 0 and sid == core.last and corr in core.cache
            before = core.deadline
            response = handle(data, transport, admission_reason=admission_reason, defer_send=defer_send)
            if defect == 'send_time' and core.renewal is not None and defer_send:
                core.deadline = before                     # the pre-a09fa45 rule: counted from the write instead
            elif defect == 'replay' and cached:
                core.deadline = model.ep.now() + core.lease
            elif defect == 'no_rejected' and response and response[3] == 0 and core.renewal is not None:
                core.deadline = before
            return response
        core.handle = altered
        if defect == 'send_time':
            def renew_at_write(token, *, check_expiry=True):
                sent(token, check_expiry=check_expiry)
                if token is not None and token == (core.holder, core.generation):
                    core.deadline = model.ep.now() + core.lease
            core.response_sent = renew_at_write
    elif defect == 'partial':
        def altered(route):
            write(route)
            active = model.pipes[route]['active']
            if active and active[0][0] == 2 and core.holder is not None:
                core.deadline = model.ep.now() + core.lease
        model.write = altered
    elif defect == 'freeze_wait':
        core.tick = lambda: None if any(model.completions.values()) else tick()
    else:
        def altered(token, *, check_expiry=True):
            sent(token, check_expiry=check_expiry)
            if token is not None: core.deadline = model.ep.now() + core.lease
        core.response_sent = altered
    report = runner.run()
    failed = [x for x in report['checks'] if x['status'] == 'failed']
    assert any(x['id'].startswith(('CORE-LEASE-', 'CORE-PRESSURE-')) for x in failed), failed


@pytest.mark.parametrize('invalid', ['wait', 'clock', 'timing', 'no_advance'])
def test_lease_clock_preconditions(invalid):
    now, model, adapter, runner = build()
    if invalid == 'wait': adapter.wait_ms = None
    elif invalid == 'clock': adapter.clock_ms = None
    elif invalid == 'timing': adapter.timing_state = lambda: {}
    else: adapter._wait = lambda ms: None
    report = runner.run()
    assert report['status'] == 'failed'
    assert any(x['id'].startswith('CORE-LEASE-') and x['status'] == 'failed' for x in report['checks'])
    assert model.ep.current_core.holder is None


@pytest.mark.parametrize('deferred', [False, True])
def test_long_execution_pauses_lease(deferred):
    now, model, adapter, runner = build()
    runner.c.confirm()
    with runner.c.holding(1000) as sid:
        dispatch = model.extension.dispatch
        def long_dispatch(fn, op, payload):
            now[0] += 1500
            return dispatch(fn, op, payload)
        model.extension.dispatch = long_dispatch
        request = runner.create(sid)
        if deferred:
            adapter.hold(adapter.primary, 0)
            adapter.submit([(adapter.primary, request)]); adapter.service()
            generated = now[0]                              # the answer exists once the long op is done
            assert model.ep.current_core.deadline == generated + 1000
            now[0] += 100
            adapter.hold(adapter.primary, None); adapter.service()
            frames = runner.frames(adapter.primary, [request])
            assert model.ep.current_core.deadline == generated + 1000   # the write does not restart it (§6.1)
        else:
            frames = [model.ep.current_core.handle(request, 0)]
            assert model.ep.current_core.deadline == now[0] + 1000
        assert len(runner.created(frames)) == 1
        assert model.ep.current_core.holder == sid
        model.extension.dispatch = dispatch


def test_close_discards_renewal_and_expires():
    """core §6.1 (a09fa45): the keepalive's answer restarts the lease when it is generated; the route closing and
    dropping that answer changes nothing - the lease runs out lease_ms after the generation."""
    now, model, adapter, runner = build()
    sid = secrets.randbelow(0xffffffff) + 1
    runner.c.success(runner.c.request(runner.c.core['open'], struct.pack('<IB', 1000, 0), session=sid))
    runner.setup(sid)
    now[0] += 300
    request = runner.c.request(runner.c.core['keepalive'], session=sid)
    adapter.hold(adapter.primary, 0); adapter.submit([(adapter.primary, request)]); adapter.service()
    assert model.ep.current_core.deadline == 1300
    adapter.close(adapter.primary)
    assert not model.completions[adapter.primary]
    assert model.ep.current_core.deadline == 1300
    now[0] += 999
    adapter.service()
    assert model.ep.current_core.holder == sid
    now[0] += 1
    adapter.service()
    assert model.ep.current_core.holder is None
    assert not model.extension.live and not model.extension.subscriptions
    assert model.ep.current_core.cache


def test_same_session_open_renews_at_generation_and_replay_does_not():
    """core §6.1, §6.2 (a09fa45): the holder's open again restarts the lease at its answer's generation (lease_ms
    2000), not when the writer completes; its replay changes nothing."""
    now, model, adapter, runner = build()
    runner.c.confirm()
    with runner.c.holding(1000) as sid:
        runner.setup(sid)
        resources = model.extension.live.copy()
        generation = model.ep.current_core.generation
        now[0] += 100
        request = runner.c.request(runner.c.core['open'], struct.pack('<IB', 2000, 0), session=sid)
        adapter.hold(adapter.primary, 0); adapter.submit([(adapter.primary, request)]); adapter.service()
        assert model.ep.current_core.deadline == 2100
        now[0] += 100
        adapter.hold(adapter.primary, None); adapter.service()
        saved = adapter.receive(adapter.primary)[0]
        assert struct.unpack_from('<I', saved, 5)[0] == 2000
        assert model.ep.current_core.deadline == 2100
        now[0] += 150
        assert runner.c.exchange(request) == saved
        assert model.ep.current_core.deadline == 2100
        assert model.extension.live == resources and model.ep.current_core.generation == generation
