import pytest
from oep_client.conformance_notification_retention import NotificationRetentionChecks
from oep_client.conformance_notification_retention_sample import SampleNotificationRetentionAdapter

SPEC='/explicit/notification-retention-spec'
@pytest.fixture(autouse=True)
def selected_spec(monkeypatch):
    monkeypatch.setattr('oep_client.conformance_notification_retention.reviewed_identity',lambda path:{'commit':'unit-fixture'} if path==SPEC else None)

def test_retained_notification_quarter_span():
    report=NotificationRetentionChecks(SampleNotificationRetentionAdapter(),spec=SPEC).run()
    assert report['status']=='passed',report['checks']
    assert len(report['checks'])==5 and len(report['observations'])==4

@pytest.mark.parametrize('fault,active',[
    ('old_pending',False),('old_active',True),('early_drop',False),('early_close',True),
    ('replace_active',True),('keep_route',True),('drop_peer',False),('counter',False),
    ('refund',True),('holder',False),('size',False),('boundary',False),
    ('frame_role',False),('reopen',True)])
def test_retention_observation_faults(fault,active):
    case=dict(seed=65530,active=active)
    adapter=SampleNotificationRetentionAdapter();run=adapter.run_case
    def broken(c):
        r=run(c);initial,before,boundary,after=[o['state'] for o in r['observations']]
        if fault=='old_pending':boundary['pending_hex'].append(initial['pending_hex'][0])
        if fault=='old_active':boundary['active']=initial['active']
        if fault=='early_drop':before['pending_hex'].remove(initial['pending_hex'][0])
        if fault=='early_close':before['open']=False
        if fault=='replace_active':before['active']=dict(before['active'],offset=2)
        if fault=='keep_route':boundary['open']=True
        if fault=='drop_peer':boundary['pending_hex'].remove(initial['pending_hex'][1])
        if fault=='counter':after['next_seq']['2']=2
        if fault=='refund':after['next_seq']['1']-=1
        if fault=='holder':boundary['holder']=None
        if fault=='size':boundary['bytes_held']=129
        if fault=='boundary':r['observations'][2]['produced']-=1
        if fault=='frame_role':initial['pending_hex'][0]='02'+initial['pending_hex'][0][2:]
        if fault=='reopen':after['open']=True
        return r
    adapter.run_case=broken;checker=NotificationRetentionChecks(adapter,spec=SPEC)
    checker.check('FAULT',lambda:checker.case(case))
    assert checker.checks[-1]['status']=='failed' and checker.checks[-1]['error'].startswith('Violation:'),checker.checks

@pytest.mark.parametrize('fault,active',[
    ('no_eviction',False),('late_eviction',False),('discard_partial',True),('global_eviction',False)])
def test_actual_retention_faults(monkeypatch,fault,active):
    from oep_client.virtual_route_model import RouteModel
    offer=RouteModel.offer
    if fault=='no_eviction':
        def bad_offer(model,route,frame):
            if route in model.pipes and model.pipes[route]['open'] and sum(map(len,model.queue(route)))+len(frame)<=model.ep.probe.max_frame*2:
                model.pipes[route]['push'].append(frame)
        monkeypatch.setattr(RouteModel,'offer',bad_offer)
    if fault=='late_eviction':
        def bad_offer(model,route,frame):
            fn=int.from_bytes(frame[1:3],'little');seq=model.extension.subscriptions[fn]
            model.extension.subscriptions[fn]=(seq-1)%65536
            try:return offer(model,route,frame)
            finally:model.extension.subscriptions[fn]=seq
        monkeypatch.setattr(RouteModel,'offer',bad_offer)
    if fault=='discard_partial':
        def bad_close(model,route):model.pipes[route]['active']=None;model.pipes[route]['push'].clear()
        monkeypatch.setattr(RouteModel,'close',bad_close)
    if fault=='global_eviction':
        def bad_offer(model,route,frame):
            result=offer(model,route,frame)
            if int.from_bytes(frame[1:3],'little')==1 and model.extension.subscriptions[1]==16378:
                model.pipes[route]['push'].clear()
            return result
        monkeypatch.setattr(RouteModel,'offer',bad_offer)
    checker=NotificationRetentionChecks(SampleNotificationRetentionAdapter(),spec=SPEC)
    checker.check('FAULT',lambda:checker.case(dict(seed=65530,active=active)))
    assert checker.checks[-1]['status']=='failed',checker.checks

@pytest.mark.parametrize('missing',['run_case','source_sha256'])
def test_missing_retention_controls(missing):
    adapter=SampleNotificationRetentionAdapter();setattr(adapter,missing,None)
    assert NotificationRetentionChecks(adapter,spec=SPEC).run()['checks'][0]['status']=='failed'
