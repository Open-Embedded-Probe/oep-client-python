import copy
import pytest
from oep_client.conformance import Checks
from oep_client.conformance_routes import RouteChecks
from oep_client.conformance_route_sample import SampleRouteAdapter
from oep_client.virtual_route_model import RouteModel
from test_conformance import REG


def build():
    model = RouteModel(lambda: 0, boot_id=17)
    adapter = SampleRouteAdapter(model)
    checks = Checks(lambda req: adapter.exchange(adapter.primary, req), copy.deepcopy(REG), 'virtual-routes-1')
    return model, adapter, RouteChecks(checks, adapter)


def test_routes_pass():
    model, adapter, runner = build()
    report = runner.run()
    assert report['status'] == 'passed', [(x['id'], x.get('error')) for x in report['checks']]
    assert len(report['checks']) == 9
    assert not report['full_conformance']
    assert model.ep.current_core.holder is None


@pytest.mark.parametrize('defect,method', [('unbounded','drop'),('priority','priority'),
                                         ('wrong_route','notify'),('release_close','close')])
def test_mutants(defect, method):
    model, adapter, runner = build()
    runner.c.confirm(); runner.peer.confirm()
    if defect == 'unbounded':
        model.offer = lambda route, frame: model.pipes[route]['push'].append(frame)
    elif defect == 'wrong_route':
        offer = model.offer
        model.offer = lambda route, frame: offer('peer', frame)
    elif defect == 'release_close':
        close = model.close
        def altered(route):
            close(route)
            model.ep.current_core.release()
        model.close = altered
    else:
        service = model.service
        def altered():
            pipe=model.pipes['primary']
            if pipe['budget'] is None and pipe['push'] and pipe['active'] is None:
                pipe['active']=(pipe['push'].popleft(),0)
            service()
        model.service=altered
    runner.c.check('IF-ROUTE-MUTANT','core §11.4',getattr(runner,method))
    assert runner.c.results[-1]['status']=='failed'
    assert adapter.trace


def test_dropped_notifications_must_consume_seq():
    model, adapter, runner = build()
    runner.c.confirm(); runner.peer.confirm()
    offer = model.offer
    def altered(route, frame):
        before = list(model.queue(route))
        offer(route, frame)
        if model.queue(route) == before:
            fn = int.from_bytes(frame[1:3], 'little')
            model.extension.subscriptions[fn] = (model.extension.subscriptions[fn] - 1) & 65535
    model.offer = altered
    runner.c.check('IF-ROUTE-MUTANT', 'core §11.2', runner.drop)
    assert runner.c.results[-1]['status'] == 'failed'


def test_partial_suffix_and_result_priority():
    model, adapter, runner = build()
    runner.c.confirm(); runner.peer.confirm()
    runner.c.check('IF-ROUTE-PARTIAL', 'core §11.4', runner.partial)
    assert runner.c.results[-1]['status'] == 'passed'
    queues = [row['result'] for row in adapter.trace if row['method'] == 'queue']
    assert any(sum(len(bytes.fromhex(part['hex'])) for part in row) == 57 for row in queues)
    assert model.ep.current_core.holder is None


@pytest.mark.parametrize('invalid', ['unit', 'same_route', 'budget', 'capability', 'fn'])
def test_bad_explicit_config_blocks_mutation(invalid):
    model, adapter, runner = build()
    if invalid == 'unit': runner.c.unit = 'other-unit'
    elif invalid == 'same_route': adapter.peer = adapter.primary
    elif invalid == 'budget': adapter.service_budget_ms = 0
    elif invalid == 'capability': adapter.queue = None
    else: adapter.functions = (0,)
    report = runner.run()
    identity = next(x for x in report['checks'] if x['id'] == 'IF-ROUTE-IDENTITY')
    assert identity['status'] == 'failed'
    assert model.ep.current_core.holder is None
    assert not any(row['method'] in ('emit','feed') for row in adapter.trace)


def test_service_timeout_is_not_nonblocking_pass():
    import time
    model, adapter, runner = build()
    adapter.service_budget_ms = 1
    service = model.service
    def delayed():
        time.sleep(.01)
        service()
    model.service = delayed
    runner.c.check('IF-ROUTE-SERVICE', 'core §11.4; fixture budget', runner.service)
    assert runner.c.results[-1]['status'] == 'failed'
    assert 'service observation budget' in runner.c.results[-1]['error']


def test_current_core_also_passes_route_writer(monkeypatch):
    now = [0]
    monkeypatch.setattr('oep_client.conformance.time.sleep', lambda seconds: now.__setitem__(0,now[0]+int(seconds*1000)))
    model = RouteModel(lambda: now[0], boot_id=17)
    adapter = SampleRouteAdapter(model)
    report = Checks(lambda req: adapter.exchange(adapter.primary,req),copy.deepcopy(REG),'virtual-routes-1').run()
    assert report['status']=='passed',[(x['id'],x.get('error')) for x in report['checks']]
    assert len(report['checks'])==49


def test_unexpected_exchange_output_is_retained_without_filtering():
    model, adapter, runner = build()
    receive = model.receive
    model.receive = lambda route: receive(route) + [b'\x03\x01\0\0\0\x01\0\0\0\0']
    runner.c.check('IF-ROUTE-EXCHANGE', 'core §4/11', runner.c.confirm)
    assert runner.c.results[-1]['status'] == 'failed'
    raw = next(row for row in reversed(adapter.trace) if row['method']=='receive')
    assert len(raw['result'])==2 and raw['result'][1]['hex'].startswith('03')
