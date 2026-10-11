from oep_client.conformance_config_fallback import ConfigFallbackChecks
from oep_client.conformance_config_fallback_sample import SampleConfigFallbackAdapter
from test_conformance_config import setup
import pytest


def test_saved_identity_and_whole_config_fallback():
    model,c=setup();r=ConfigFallbackChecks(c,SampleConfigFallbackAdapter(model)).run()
    assert r['status']=='passed',[(row['id'],row.get('error')) for row in r['checks']]
    assert len(r['checks'])==11 and len(r['state_observations'])==24 and not r['full_conformance']


@pytest.mark.parametrize('case',ConfigFallbackChecks.cases)
def test_wrong_storage_result_is_detected(case):
    model,c=setup();a=SampleConfigFallbackAdapter(model);active=None;perturb=a.perturb;send=c.send
    def change(name):
        nonlocal active
        active=name;perturb(name)
    def broken(req):
        out=send(req)
        if active==case and req[3:6]==b'\4\0\x15':return out[:3]+b'\1\1'+out[5:]
        return out
    a.perturb=change;c.send=broken;r=ConfigFallbackChecks(c,a).run()
    row=next(x for x in r['checks'] if x['id']=='OEP-CONFIG-FALLBACK-'+case)
    assert row['status']=='failed' and row['error'].startswith('Violation:'),row


@pytest.mark.parametrize('fault,case',[
    ('wrong_reason','FORMAT'),('wrong_state','MISSING'),('wrong_hash','REVISION'),
    ('partial_idle','APPLY'),('partial_disable','NAME'),('partial_items','INSTANCE'),
    ('rewrite_storage','FORMAT'),('old_fn','REMAP'),('old_hash','REMAP'),
    ('execute_preset','REMAP'),('fail_unrelated','UNRELATED'),('no_new_boot','FORMAT'),
    ('set_clears_error','MISSING'),('save_keeps_error','APPLY'),('erase_keeps_error','REVISION')])
def test_semantic_faults_are_detected(fault,case):
    model,c=setup();a=SampleConfigFallbackAdapter(model);active=None;perturb=a.perturb;restart=model.restart_fixture
    def change(name):
        nonlocal active
        active=name;perturb(name)
    def broken_boot():
        old=model.ep.boot_id;r=restart()
        if active!=case:return r
        e=model.extension
        if fault=='wrong_reason':model.storage_reason=3
        if fault=='wrong_state':model.storage_reason=0
        if fault in ('wrong_hash','old_hash'):model.saved_hash=123
        if fault=='partial_idle':e.pins[1]=(4,0)
        if fault=='partial_items':e.items[(2,b'\4\0')]=b'\4\0nrst'
        if fault=='rewrite_storage':model.saved[(2,b'\4\0')]=b'\4\0rewritten'
        if fault=='old_fn':
            e.items.pop((1,b'\3\0\1\3\0'));e.items[(1,b'\2\0\1\3\0')]=b'\2\0\1\3\0'
        if fault=='execute_preset':e.plans={3:[3]}
        if fault=='fail_unrelated':e.items={};model.storage_reason=2;model.saved_hash=0
        if fault=='no_new_boot':model.ep.boot_id=old;r['boot_id']=old
        original=e.dispatch_request
        def bad(fn,op,payload,tags):
            if fault=='partial_disable' and fn==1 and op==16:
                from oep_client.virtual_core import Reject,tlv
                raise Reject(4,tlv(1,b'\5'))
            out=original(fn,op,payload,tags)
            if fn==4:
                if fault=='set_clears_error' and op==17:model.storage_reason=0
                if fault=='save_keeps_error' and op==18:model.storage_reason=3
                if fault=='erase_keeps_error' and op==19:model.storage_reason=2
            return out
        e.dispatch_request=bad
        return r
    a.perturb=change;model.restart_fixture=broken_boot
    r=ConfigFallbackChecks(c,a).run();row=next(x for x in r['checks'] if x['id']=='OEP-CONFIG-FALLBACK-'+case)
    assert row['status']=='failed' and row['error'].startswith('Violation:'),row


@pytest.mark.parametrize('missing',['state','seed','restart_fixture','prepare','perturb','storage_snapshot'])
def test_missing_controls_do_not_pass(missing):
    model,c=setup();a=SampleConfigFallbackAdapter(model);setattr(a,missing,None)
    r=ConfigFallbackChecks(c,a).run();assert r['checks'][2]['status']=='failed'


def test_common_declarations():
    from oep_client.conformance_interfaces import InterfaceChecks
    _,c=setup();r=InterfaceChecks(c).run();assert r['status']=='passed'


@pytest.mark.parametrize('shape',['utf8','plan_length','plan_key','missing_identity','identity_shape','extra_identity'])
def test_distinct_unreadable_storage_shapes(shape):
    model,c=setup();a=SampleConfigFallbackAdapter(model);perturb=a.perturb
    def malformed(name):
        perturb(name)
        if name!='FORMAT':return
        model.saved[(2,b'\4\0')]=b'\4\0nrst'
        if shape=='utf8':model.saved[(2,b'\4\0')]=b'\4\0\xff'
        if shape=='plan_length':model.saved[(1,b'\2\0\1\3\0')]=b'\2\0\1\3'
        if shape=='plan_key':model.saved[(1,b'\2\0\1\3\0')]=b'\2\0\1\4\0'
        if shape=='missing_identity':model.saved_identities={}
        if shape=='identity_shape':model.saved_identities[2]=('oep.fixture.gpio',0)
        if shape=='extra_identity':model.saved_identities[3]=('oep.fixture.gpio',1,1)
    a.perturb=malformed;r=ConfigFallbackChecks(c,a).run()
    assert r['status']=='passed',[(row['id'],row.get('error')) for row in r['checks']]
