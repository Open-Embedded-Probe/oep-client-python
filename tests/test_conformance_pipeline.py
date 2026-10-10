import copy
from oep_client.conformance import Checks
from oep_client.conformance_pipeline import PipelineChecks
from oep_client.conformance_route_sample import SampleRouteAdapter
from oep_client.virtual_route_model import RouteModel
from test_conformance import REG


def test_unbounded_model_fails_pipeline_preflight():
    model = RouteModel(lambda: 0, boot_id=17)
    adapter = SampleRouteAdapter(model)
    c = Checks(lambda req: adapter.exchange(adapter.primary, req), copy.deepcopy(REG), 'virtual-routes-1')
    report = PipelineChecks(c, adapter).run()
    assert report['status'] == 'failed'
    assert not any(row['method'] == 'hold' for row in adapter.trace)

from oep_client.virtual_pipeline_model import PipelineModel
from oep_client.conformance_pipeline_sample import SamplePipelineAdapter
import pytest


def build():
    model = PipelineModel(lambda: 0, boot_id=17)
    adapter = SamplePipelineAdapter(model)
    c = Checks(lambda req: adapter.exchange(adapter.primary, req), copy.deepcopy(REG), 'virtual-routes-1')
    runner = PipelineChecks(c, adapter)
    return model, adapter, runner


def test_pipeline_passes():
    model, adapter, runner = build()
    report = runner.run()
    assert report['status'] == 'passed', [(x['id'], x.get('error')) for x in report['checks']]
    assert len(report['checks']) == 9
    assert all(not model.pending(route) for route in model.pipes)
    assert not report['full_conformance']


@pytest.mark.parametrize('defect', ['count', 'bytes', 'early_release', 'shared', 'leak', 'order'])
def test_admission_mutants_fail(defect):
    model, adapter, runner = build()
    if defect in ('count', 'bytes'):
        submit = model.submit
        def altered(requests):
            old = model.limits.copy()
            model.limits['primary'] = (80, 255) if defect == 'count' else (10000, 3)
            try: submit(requests)
            finally: model.limits = old
        model.submit = altered
    elif defect == 'early_release':
        service = model.service
        def altered():
            service()
            for route in model.unresolved: model.unresolved[route].clear()
        model.service = altered
    elif defect == 'shared':
        model.unresolved['peer'] = model.unresolved['primary']
    elif defect == 'leak':
        hold = adapter.hold
        def altered(route, budget):
            model.write = lambda route: RouteModel.write(model, route)
            return hold(route, budget)
        adapter.hold = altered
    else:
        submit = model.submit
        def altered(requests): submit(list(reversed(requests)))
        model.submit = altered
    report = runner.run()
    assert report['status'] == 'failed'
    failures = [x for x in report['checks'] if x['status'] == 'failed']
    assert any(x['id'] != 'CORE-PIPELINE-IDENTITY' for x in failures), failures


@pytest.mark.parametrize('defect', ['policy', 'unit', 'route', 'pending', 'geometry', 'budget'])
def test_explicit_preflight(defect):
    model, adapter, runner = build()
    if defect == 'policy': adapter.admission_policy = 'silent-drop'
    elif defect == 'unit': runner.c.unit = 'other-unit'
    elif defect == 'route': adapter.peer = adapter.primary
    elif defect == 'pending': adapter.pending = None
    elif defect == 'geometry': model.limits['primary'] = (96, 3)
    else: adapter.service_budget_ms = 0
    report = runner.run()
    assert report['status'] == 'failed'
    assert not any(row['method'] == 'hold' for row in adapter.trace)


def test_current_core_remains_valid(monkeypatch):
    now = [0]
    monkeypatch.setattr('oep_client.conformance.time.sleep', lambda seconds: now.__setitem__(0, now[0] + int(seconds*1000)))
    model = PipelineModel(lambda: now[0], boot_id=17)
    adapter = SamplePipelineAdapter(model)
    report = Checks(lambda req: adapter.exchange(adapter.primary, req), copy.deepcopy(REG), 'virtual-routes-1').run()
    assert report['status'] == 'passed', [(x['id'], x.get('error')) for x in report['checks']]
    assert len(report['checks']) == 49
