import pytest
from oep_client.conformance_probe_input import ProbeInputChecks
from oep_client.conformance_probe_input_sample import SampleProbeInputAdapter

SPEC='/explicit/probe-input-spec'
@pytest.fixture(autouse=True)
def selected_spec(monkeypatch):
    monkeypatch.setattr('oep_client.conformance_probe_input.reviewed_identity',lambda path:{'commit':'unit-fixture'} if path==SPEC else None)

def test_raw_probe_role_and_header_rules():
    r=ProbeInputChecks(SampleProbeInputAdapter(),spec=SPEC).run()
    assert r['status']=='passed',r['checks']
    assert len(r['checks'])==4 and len(r['observations'])==781
    assert sum(o['group']=='role' for o in r['observations'])==765
    assert sum(o['group']=='short' for o in r['observations'])==10

@pytest.mark.parametrize('fault,method',[
    ('role_reply','discard'),('role_renew','discard'),('role_cache','discard'),('role_free','discard'),
    ('short_reply','discard'),('short_mutates','discard'),('header_swallowed','controls'),
    ('valid_wrong_id','controls'),('expiry_skipped','expiry'),('expiry_cache','expiry'),
    ('early_expiry','expiry'),('old_restored','expiry'),('role_owner','discard')])
def test_discard_observation_faults(fault,method):
    c=ProbeInputChecks(SampleProbeInputAdapter(),spec=SPEC);observe=c.observe
    def broken(group,data):
        row=observe(group,data)
        if group=='role':
            if fault=='role_reply':row['reply_hex']='0203000003'
            if fault=='role_renew':row['after']['deadline']+=1
            if fault=='role_cache':row['after']['cache']['3']='junk'
            if fault=='role_free':row['after']['live']={}
            if fault=='role_owner':row['after']['owner_hex']='00'
        if group=='short':
            if fault=='short_reply':row['reply_hex']='0203000003'
            if fault=='short_mutates':row['after']['high']=3
        if group=='valid-header-short-payload' and fault=='header_swallowed':row['reply_hex']=None
        if group=='valid-request' and fault=='valid_wrong_id':row['reply_hex']='02040001000300'
        if group=='at-expiry':
            if fault=='expiry_skipped':row['after']['holder']=7
            if fault=='expiry_cache':row['after']['cache']={}
        if group=='before-expiry' and fault=='early_expiry':row['after']['holder']=None
        if group=='old-session-after-expiry' and fault=='old_restored':row['after']['holder']=7
        return row
    c.observe=broken;c.check('FAULT',getattr(c,method))
    assert c.checks[-1]['status']=='failed' and c.checks[-1]['error'].startswith('Violation:'),c.checks

@pytest.mark.parametrize('fault,method',[
    ('accept_other_roles','discard'),('reply_to_short','discard'),('timer_starved','expiry'),('drop_valid_payload','controls')])
def test_actual_raw_core_faults(monkeypatch,fault,method):
    from oep_client.virtual_core import Core
    if fault=='timer_starved':monkeypatch.setattr(Core,'tick',lambda core:None)
    else:
        handle=Core.handle
        def bad_handle(core,data,*args,**kwargs):
            if fault=='accept_other_roles' and data and data[0]!=1:data=b'\1'+data[1:]
            if fault=='reply_to_short' and len(data)<10:return b'\2\3\0\0\3'
            if fault=='drop_valid_payload' and data==ProbeInputChecks.request[:10]:return None
            return handle(core,data,*args,**kwargs)
        monkeypatch.setattr(Core,'handle',bad_handle)
    c=ProbeInputChecks(SampleProbeInputAdapter(),spec=SPEC);c.check('FAULT',getattr(c,method))
    assert c.checks[-1]['status']=='failed',c.checks

@pytest.mark.parametrize('missing',['prepare','advance_to','raw','state','source_sha256'])
def test_missing_raw_input_controls(missing):
    a=SampleProbeInputAdapter();setattr(a,missing,None)
    assert ProbeInputChecks(a,spec=SPEC).run()['checks'][0]['status']=='failed'
