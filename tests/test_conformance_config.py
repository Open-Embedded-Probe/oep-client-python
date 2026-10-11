from oep_client.conformance import Checks
from oep_client.conformance_config import ConfigChecks
from oep_client.conformance_config_sample import SampleConfigAdapter
from oep_client.virtual_config_model import ConfigModel
from test_conformance_registered_ops import setup as base_setup


def setup():
    _,base=base_setup();model=ConfigModel(boot_id=17)
    return model,Checks(model.handle,base.reg,'virtual-resource-1')


def test_current_config_storage_contract():
    model,c=setup();r=ConfigChecks(c,SampleConfigAdapter(model)).run()
    assert r['status']=='passed',[(row['id'],row.get('error')) for row in r['checks']]
    assert len(r['checks'])==7 and not r['full_conformance']


import pytest


def test_config_common_declarations():
    from oep_client.conformance_interfaces import InterfaceChecks
    model,c=setup();r=InterfaceChecks(c).run();assert r['status']=='passed',r['checks']


@pytest.mark.parametrize('case',ConfigChecks.cases)
def test_wrong_hash_or_result_is_detected(case):
    model,c=setup();original=c.send
    def broken(req):
        out=original(req)
        if req[3:6]==b'\4\0\x11':return out[:3]+b'\1\1'+out[5:]
        return out
    c.send=broken;r=ConfigChecks(c,SampleConfigAdapter(model)).run()
    row=next(x for x in r['checks'] if x['id']=='OEP-CONFIG-'+case)
    assert row['status']=='failed' and row['error'].startswith('Violation:'),row


@pytest.mark.parametrize('fault,case',[
 ('erase_current','PERSISTENCE'),('no_save','PERSISTENCE'),('save_each_set','PERSISTENCE'),
 ('wrong_storage_hash','PERSISTENCE'),('preset_applies','PRESET'),('unset_releases','PRESET'),
 ('idle_changes_live_pin','PRESET'),('refusal_changes_config','ATOMIC'),('accept_disabled_live','ATOMIC'),
 ('wrong_storage_cause','STORAGE-LIMIT'),('failed_save_changes_saved','STORAGE-LIMIT')])
def test_semantic_faults(fault,case):
    model,c=setup();restart=model.restart_fixture
    def inject():
        e=model.extension;original=e.dispatch_request
        def bad(fn,op,payload,tags):
            try:out=original(fn,op,payload,tags)
            except Exception as exc:
                if fn==4 and fault=='refusal_changes_config':e.items={}
                if fn==4 and op==17 and fault=='accept_disabled_live' and getattr(exc,'reason',None)==4:return b'\0'*4
                if fn==4 and op==18 and fault=='wrong_storage_cause':exc.payload=b'\1\1\0\4'
                if fn==4 and op==18 and fault=='failed_save_changes_saved':model.saved=dict(e.items)
                raise
            if fn==4:
                if fault=='erase_current' and op==19:e.items={};e.apply()
                if fault=='no_save' and op==18:model.saved=None;model.saved_hash=0
                if fault=='save_each_set' and op==17:model.saved=dict(e.items);model.saved_hash=e.hash()
                if fault=='wrong_storage_hash' and op==21:out=out[:1]+b'\xff'*4+out[5:]
                if fault=='preset_applies' and op==17 and any(raw&127==1 for raw,v in tags):e.plans={2:[3]}
                if fault=='unset_releases' and op==20:e.release()
                if fault=='idle_changes_live_pin' and op==17 and any(raw&127==3 for raw,v in tags):e.pins[1]=(3,0)
            return out
        e.dispatch_request=bad
    def fresh():
        r=restart();inject();return r
    model.restart_fixture=fresh
    r=ConfigChecks(c,SampleConfigAdapter(model)).run();row=next(x for x in r['checks'] if x['id']=='OEP-CONFIG-'+case)
    assert row['status']=='failed' and row['error'].startswith('Violation:'),row


@pytest.mark.parametrize('missing',['state','seed','restart_fixture'])
def test_missing_controls_do_not_pass(missing):
    model,c=setup();a=SampleConfigAdapter(model);setattr(a,missing,None)
    r=ConfigChecks(c,a).run();assert r['checks'][2]['status']=='failed'
