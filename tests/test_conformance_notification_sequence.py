import copy
import pytest
from oep_client.conformance import Checks
from oep_client.conformance_notification_sequence import NotificationSequenceChecks
from oep_client.conformance_notification_sequence_sample import SampleNotificationSequenceAdapter
from oep_client.virtual_route_model import RouteModel
from test_conformance import REG


def build():
    model = RouteModel(lambda: 0, boot_id=17)
    adapter = SampleNotificationSequenceAdapter(model)
    c = Checks(lambda req: adapter.exchange(adapter.primary, req), copy.deepcopy(REG), 'virtual-routes-1')
    return model, adapter, NotificationSequenceChecks(c, adapter)


def test_notification_sequence_contracts_pass():
    model, _, runner = build(); report = runner.run()
    assert report['status'] == 'passed', [(x['id'], x.get('error')) for x in report['checks']]
    assert len(report['checks']) == 9 and all(x['level'] == 'interface' for x in report['checks'])
    assert not report['full_conformance'] and model.ep.current_core.holder is None


@pytest.mark.parametrize('fault,case', [
    ('no_wrap', 'WRAP'), ('event_separate', 'WRAP'), ('global_seq', 'WRAP'),
    ('drop_refund', 'DROP-WRAP'), ('keep_route', 'REPLACE'), ('keep_seq', 'REPLACE'),
    ('replay_route', 'REPLAY'), ('replay_seq', 'REPLAY'), ('reject_route', 'REJECTED'),
    ('replay_bytes', 'REPLAY'),
])
def test_independent_notification_faults_are_detected(fault, case):
    model, adapter, runner = build()
    # Run a fresh case, installing the fault on each reset without replacing
    # the reset method's evidence or boot semantics.
    reset = model.restart_fixture
    def faulty_reset():
        value = reset(); extension = model.extension
        emit, dispatch, offer = extension.emit, extension.dispatch, model.offer
        if fault in ('no_wrap', 'event_separate', 'global_seq'):
            def corrupt_emit(fn, marker):
                if fn not in extension.subscriptions: return emit(fn, marker)
                seq = extension.subscriptions[fn]
                if fault == 'global_seq': extension.subscriptions[fn] = max(extension.subscriptions.values())
                emit(fn, marker)
                if fault == 'no_wrap': extension.subscriptions[fn] = seq + 1
                elif fault == 'event_separate': extension.subscriptions[fn] = seq
            extension.emit = corrupt_emit
        elif fault == 'drop_refund':
            def corrupt_offer(route, frame):
                before = model.queue(route); offer(route, frame)
                if before == model.queue(route):
                    fn = int.from_bytes(frame[1:3], 'little')
                    extension.subscriptions[fn] = (extension.subscriptions[fn] - 1) & 65535
            model.offer = corrupt_offer
        elif fault in ('keep_route', 'keep_seq'):
            def corrupt_dispatch(fn, op, payload):
                old_route = extension.routes.get(fn); old_seq = extension.subscriptions.get(fn)
                result = dispatch(fn, op, payload)
                if op == 1 and old_route is not None:
                    if fault == 'keep_route': extension.routes[fn] = old_route
                    else: extension.subscriptions[fn] = old_seq
                return result
            extension.dispatch = corrupt_dispatch
        elif fault in ('replay_route', 'replay_seq', 'reject_route', 'replay_bytes'):
            handle = model.ep.current_core.handle
            def corrupt_handle(request, *args, **kwargs):
                replay = len(request) >= 10 and int.from_bytes(request[1:3], 'little') in model.ep.current_core.cache
                response = handle(request, *args, **kwargs)
                if request[3:6] == bytes((1, 0, 1)) and 1 in extension.subscriptions:
                    if fault == 'replay_route' and replay: extension.routes[1] = model.active_route
                    elif fault == 'replay_seq' and replay: extension.subscriptions[1] = 0
                    elif fault == 'reject_route' and response[3] == 0: extension.routes[1] = model.active_route
                    elif fault == 'replay_bytes' and replay: response += b'\x7f\x01\0x'
                return response
            model.ep.current_core.handle = corrupt_handle
        return value
    model.restart_fixture = faulty_reset
    # Constructor reset replaces model fields, but intentionally installed
    # instance hooks survive and install fresh faults for the isolated case.
    method = {'WRAP': runner.wrap, 'DROP-WRAP': runner.dropped, 'REPLACE': runner.replace,
              'REPLAY': runner.replay, 'REJECTED': runner.rejected}[case]
    runner.c.check('IF-NOTIFY-SEQ-'+case, 'core §11', method)
    assert runner.c.results[-1]['status'] == 'failed', runner.c.results


@pytest.mark.parametrize('invalid', ['scope', 'reference', 'capability'])
def test_bad_sequence_fixture_blocks_before_wire(invalid):
    _, adapter, runner = build()
    if invalid == 'scope': adapter.reset_scope = 'physical'
    elif invalid == 'reference': adapter.reference = ''
    else: adapter.seed_sequence = None
    report = runner.run()
    assert report['checks'][0]['status'] == 'failed' and not runner.c.trace and not adapter.trace
    assert all(x['status'] == 'blocked' for x in report['checks'][1:])
