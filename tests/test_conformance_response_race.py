import pytest
from oep_client.conformance_response_race import ResponseRaceChecks
from oep_client.conformance_response_race_sample import SampleResponseRaceAdapter

SPEC='/explicit/response-race-spec'
@pytest.fixture(autouse=True)
def selected_spec(monkeypatch):
    monkeypatch.setattr('oep_client.conformance_response_race.reviewed_identity',lambda path:{'commit':'unit-fixture'} if path==SPEC else None)

def test_competing_writer_completion():
    r=ResponseRaceChecks(SampleResponseRaceAdapter(),spec=SPEC).run()
    assert r['status']=='passed',r['checks']
    assert len(r['checks'])==13 and len(r['observations'])==12

@pytest.mark.parametrize('fault,kind',[
    ('old_corr','FORCE'),('old_result','REJECTED'),('old_token','FORCE'),('early_renew','FORCE'),
    ('stale_lease','FORCE'),('stale_resources','FORCE'),('stale_cache','FORCE'),
    ('refusal_token','NO-FORCE'),('refusal_result','NO-FORCE'),('no_valid_renew','NO-FORCE'),
    ('replay_renews','NO-FORCE'),('force_resources','DOUBLE-FORCE'),('reset_id','FORCE'),
    ('middle_renews','DOUBLE-FORCE'),('expiry_owner','EXPIRE-NEW'),('same_id_epoch','RETURN-ID'),
    ('new_request_sid','FORCE'),('expiry_early','EXPIRE-NEW'),('force_reply','FORCE'),('middle_reply','DOUBLE-FORCE')])
def test_response_race_observation_faults(fault,kind):
    a=SampleResponseRaceAdapter();run=a.run_case
    def broken(case):
        r=run(case);events=r['events'];old=next(e for e in events if e['action']=='prepare' and e['request_hex'].startswith('010300'))
        late=next(e for e in events if e['action']=='finish' and e['request_hex'].startswith('010300'))
        if fault=='old_corr':old['request_hex']='010400'+old['request_hex'][6:]
        if fault=='old_result':old['reply_hex']='02030001000200'
        if fault=='old_token':old['token']=[8,2]
        if fault=='early_renew':old['after']['deadline']+=1
        if fault=='stale_lease':late['after']['deadline']+=1
        if fault=='stale_resources':late['after']['live']['1']=[1,1]
        if fault=='stale_cache':r['state']['cache']['3']='old'
        if fault=='refusal_token':next(e for e in events if e['action']=='prepare' and e['route']==1)['token']=[7,1]
        if fault=='refusal_result':next(e for e in events if e['action']=='prepare' and e['route']==1)['reply_hex']='0201000007'
        if fault=='no_valid_renew':late['after']['deadline']=late['before']['deadline']
        if fault=='replay_renews':next(e for e in events if e['action']=='replay')['after']['deadline']+=1
        if fault=='force_resources':next(e for e in events if e['action']=='prepare' and e['route']==1)['after']['live']={'1':[1,1]}
        if fault=='reset_id':r['state']['next_id']=1
        if fault=='middle_renews':next(e for e in events if e['action']=='finish' and e['route']==1)['after']['deadline']+=1
        if fault=='expiry_owner':next(e for e in events if e['action']=='expire')['after']['holder']=8
        if fault=='same_id_epoch':r['state']['generation']=1
        if fault=='new_request_sid':e=next(e for e in events if e['action']=='prepare' and e['route']==1);e['request_hex']=e['request_hex'][:12]+'09000000'+e['request_hex'][20:]
        if fault=='expiry_early':next(e for e in events if e['action']=='expire')['before']['now_ms']-=1
        if fault=='force_reply':next(e for e in events if e['action']=='prepare' and e['route']==1)['reply_hex']='0201000100'
        if fault=='middle_reply':next(e for e in events if e['action']=='finish' and e['route']==1)['reply_hex']='0201000100'
        return r
    a.run_case=broken;c=ResponseRaceChecks(a,spec=SPEC);c.check('FAULT',lambda:c.case(dict(kind=kind,lease_ms=1000)))
    assert c.checks[-1]['status']=='failed' and c.checks[-1]['error'].startswith('Violation:'),c.checks

@pytest.mark.parametrize('fault,kind',[
    ('renew_stale','FORCE'),('ignore_epoch','RETURN-ID'),('reset_ids','FORCE'),('renew_replay','NO-FORCE')])
def test_actual_completion_faults(monkeypatch,fault,kind):
    from oep_client.virtual_core import Core
    from oep_client.virtual_resource_model import Resources
    if fault in ('renew_stale','ignore_epoch'):
        def bad_sent(core,token,**kwargs):
            core.tick()
            if token is not None and (fault=='renew_stale' or token[0]==core.holder):core.deadline=core.ep.now()+core.lease
        monkeypatch.setattr(Core,'response_sent',bad_sent)
    if fault=='reset_ids':
        release=Resources.release
        def bad_release(e):release(e);e.next_id=1
        monkeypatch.setattr(Resources,'release',bad_release)
    if fault=='renew_replay':
        handle=Core.handle
        def bad_handle(core,data,*args,**kwargs):
            replay=data[3:6]==b'\1\0\x10' and int.from_bytes(data[1:3],'little') in core.cache
            result=handle(core,data,*args,**kwargs)
            if replay:core.deadline=core.ep.now()+core.lease
            return result
        monkeypatch.setattr(Core,'handle',bad_handle)
    c=ResponseRaceChecks(SampleResponseRaceAdapter(),spec=SPEC);c.check('FAULT',lambda:c.case(dict(kind=kind,lease_ms=1000)))
    assert c.checks[-1]['status']=='failed',c.checks

@pytest.mark.parametrize('missing',['run_case','source_sha256'])
def test_missing_completion_controls(missing):
    a=SampleResponseRaceAdapter();setattr(a,missing,None)
    assert ResponseRaceChecks(a,spec=SPEC).run()['checks'][0]['status']=='failed'
