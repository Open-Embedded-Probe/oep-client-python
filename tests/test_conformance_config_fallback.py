from oep_client.conformance_config_fallback import ConfigFallbackChecks
from oep_client.conformance_config_fallback_sample import SampleConfigFallbackAdapter
from test_conformance_config import setup
import pytest


def test_saved_items_partial_and_unreadable_fallback():
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
    ('wrong_reason','FORMAT'),('partial_on_corrupt','FORMAT'),('leaked_disable','FORMAT'),('rewrite_storage','FORMAT'),('no_new_boot','FORMAT'),
    ('whole_refused','UNKNOWN'),('wrong_state','UNKNOWN'),('set_clears_error','CHANNEL'),
    ('saved_generation_partial','CHANNEL'),('remapped_channel','CHANNEL'),('remapped_mode','MODE'),('idle_not_parked','MODE'),
    ('kept_invalid_label','LABEL'),('idle_wins','CONFLICT'),('save_keeps_error','CONFLICT'),
    ('first_duplicate','DUPLICATE'),('erase_keeps_error','DUPLICATE'),
    ('label_before_disable','CAPACITY'),('item_reason_for_capacity','CAPACITY'),('boot_generation','CAPACITY')])
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
        if fault=='partial_on_corrupt':e.items[(2,4)]=b'\4\0nrst'
        if fault=='leaked_disable':e.items[(7,6)]=b'\6\0';e.apply()
        if fault=='rewrite_storage':model.saved=[(2,b'\4\0rewritten')]
        if fault=='no_new_boot':model.ep.boot_id=old;r['boot_id']=old
        if fault=='whole_refused':e.items={};e.apply()
        if fault=='wrong_state':model.storage_state,model.storage_reason,model.saved_generation=1,0,1
        if fault=='saved_generation_partial':model.saved_generation=1
        if fault=='remapped_channel':e.items[(2,1)]=b'\1\0far'
        if fault=='remapped_mode':e.items[(3,2)]=b'\2\0\4';e.apply()
        if fault=='idle_not_parked':e.pins[1]=0
        if fault=='kept_invalid_label':e.items[(2,5)]=b'\5\0?'
        if fault=='idle_wins':e.items.pop((7,1));e.items[(3,1)]=b'\1\0\4';e.apply()
        if fault=='first_duplicate':e.items[(2,4)]=b'\4\0nrst'
        if fault=='label_before_disable':e.items.pop((7,6));e.items[(2,4)]=b'\4\0nrst';e.apply()
        if fault=='item_reason_for_capacity':model.storage_reason=2
        if fault=='boot_generation':e.generation=2
        original=e.dispatch_request
        def bad(fn,op,payload,tags):
            out=original(fn,op,payload,tags)
            if fn==4:
                if fault=='set_clears_error' and op==17:model.storage_state,model.storage_reason=1,0
                if fault=='save_keeps_error' and op==18:model.storage_state,model.storage_reason=3,2
                if fault=='erase_keeps_error' and op==19:model.storage_state,model.storage_reason=3,2
            return out
        e.dispatch_request=bad
        return r
    a.perturb=change;model.restart_fixture=broken_boot
    r=ConfigFallbackChecks(c,a).run();row=next(x for x in r['checks'] if x['id']=='OEP-CONFIG-FALLBACK-'+case)
    assert row['status']=='failed' and row['error'].startswith('Violation:'),row


@pytest.mark.parametrize('missing',['state','seed','restart_fixture','generation_at','prepare','perturb','storage_snapshot'])
def test_missing_controls_do_not_pass(missing):
    model,c=setup();a=SampleConfigFallbackAdapter(model);setattr(a,missing,None)
    r=ConfigFallbackChecks(c,a).run();assert r['checks'][2]['status']=='failed'


def test_common_declarations():
    from oep_client.conformance_interfaces import InterfaceChecks
    _,c=setup();r=InterfaceChecks(c).run();assert r['status']=='passed'


@pytest.mark.parametrize('shape',['element','tag','value','container'])
def test_distinct_unreadable_container_shapes(shape):
    model,c=setup();a=SampleConfigFallbackAdapter(model);perturb=a.perturb
    def torn(name):
        perturb(name)
        if name!='FORMAT':return
        model.saved.pop()
        if shape=='element':model.saved.append((2,))
        if shape=='tag':model.saved.append((300,b'\4\0'))
        if shape=='value':model.saved.append((2,'nrst'))
        if shape=='container':model.saved=b''.join(bytes((t,len(v),0))+v for t,v in model.saved)
    a.perturb=torn;r=ConfigFallbackChecks(c,a).run()
    assert r['status']=='passed',[(row['id'],row.get('error')) for row in r['checks']]
