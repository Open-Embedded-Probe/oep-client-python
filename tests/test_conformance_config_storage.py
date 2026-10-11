from oep_client.conformance import Checks
from oep_client.conformance_config_storage import ConfigStorageChecks
from oep_client.conformance_config_storage_sample import SampleConfigStorageAdapter
from oep_client.virtual_config_storage_model import ConfigStorageModel
from test_conformance_registered_ops import setup as base_setup
import pytest


def setup():
    _,base=base_setup();model=ConfigStorageModel()
    return model,Checks(model.handle,base.reg,'virtual-resource-1')


def test_identity_aware_save_and_capacity_boundaries():
    model,c=setup();r=ConfigStorageChecks(c,SampleConfigStorageAdapter(model)).run()
    assert r['status']=='passed',[(x['id'],x.get('error')) for x in r['checks']]
    assert len(r['checks'])==12 and len(r['state_observations'])==28 and not r['full_conformance']


def test_common_declarations():
    from oep_client.conformance_interfaces import InterfaceChecks
    _,c=setup();r=InterfaceChecks(c).run();assert r['status']=='passed',r['checks']


@pytest.mark.parametrize('case',ConfigStorageChecks.cases)
def test_bad_save_result_is_detected(case):
    model,c=setup();a=SampleConfigStorageAdapter(model);prepare=a.prepare;send=c.send;index=-1
    def fresh():
        nonlocal index
        index+=1;return prepare()
    def bad(req):
        out=send(req)
        if index>=0 and ConfigStorageChecks.cases[index]==case and req[3:6]==b'\4\0\x12':return out[:3]+b'\1\1'+out[5:]
        return out
    a.prepare=fresh;c.send=bad;r=ConfigStorageChecks(c,a).run();row=next(x for x in r['checks'] if x['id']=='OEP-CONFIG-STORAGE-'+case)
    assert row['status']=='failed' and row['error'].startswith('Violation:'),row


@pytest.mark.parametrize('fault,case',[
    ('always_write','SAME'),('no_write','SAME'),('skip_changed','CHANGE'),('raw_fn_equality','REMAP'),
    ('hash_equality','REMAP'),('ignore_identity','IDENTITY'),('insertion_order_equality','EDIT-RETURN'),
    ('auto_save_set','EDIT-RETURN'),('erase_keeps_blob','ERASE'),('erase_clears_items','ERASE'),
    ('skip_after_erase','ERASE'),('capacity63','LIMIT-64'),('capacity65','LIMIT-65'),
    ('wrong_cause','LIMIT-65'),('overflow_writes','LIMIT-65'),('save_changes_pin','SAME'),
    ('wrong_saved_items','CHANGE'),('rewrite_on_boot','SAME'),('lose_on_boot','CHANGE')])
def test_semantic_faults_are_detected(fault,case):
    model,c=setup();a=SampleConfigStorageAdapter(model);prepare=a.prepare;restart=model.restart_fixture;index=-1
    def fresh():
        nonlocal index
        index+=1;r=prepare()
        if ConfigStorageChecks.cases[index]!=case:return r
        e=model.extension;persist=model.persist_config;dispatch=e.dispatch_request;write=model.write_config;stored_hash=None
        def wrong_persist(items,ids):
            nonlocal stored_hash
            if fault=='always_write':return write(items,ids)
            if fault=='insertion_order_equality':
                if model.saved is None or list(items.items())!=list(model.saved.items()):return write(items,ids)
                return
            if fault=='no_write':return
            if fault=='skip_changed' and model.saved is not None:return
            if fault=='raw_fn_equality':
                if items!=model.saved:return write(items,ids)
                return
            if fault=='hash_equality':
                current=model.extension.hash()
                if current!=stored_hash:stored_hash=current;return write(items,ids)
                return
            if fault=='ignore_identity':
                if items==model.saved:return
            if fault=='skip_after_erase' and model.storage.erases:return
            if fault=='wrong_saved_items' and model.saved is not None:
                items=dict(items);items[(2,b'\4\0')]=b'\4\0wrong'
                return write(items,ids)
            return persist(items,ids)
        model.persist_config=wrong_persist
        def bad(fn,op,payload,tags):
            from oep_client.virtual_core import Reject,tlv
            total=sum(map(len,e.canonical()))
            if fn==4 and op==18 and fault=='capacity63' and total==64:raise Reject(4,tlv(1,b'\3'))
            if fn==4 and op==18 and fault=='capacity65' and total==65:
                write(e.items,{2:model.catalog[2]});model.saved_hash=e.hash();return e.hash().to_bytes(4,'little')
            try:out=dispatch(fn,op,payload,tags)
            except Reject as exc:
                if fn==4 and op==18 and fault=='wrong_cause':exc.payload=tlv(1,b'\4')
                if fn==4 and op==18 and fault=='overflow_writes':write(e.items,{2:model.catalog[2]})
                raise
            if fn==4:
                if fault=='auto_save_set' and op==17:write(e.items,{2:model.catalog[2]})
                if fault=='save_changes_pin' and op==18:e.pins[1]=(0,0)
                if fault=='erase_keeps_blob' and op==19:model.storage.blob=model.storage.writes[-1]
                if fault=='erase_clears_items' and op==19:e.items={}
            return out
        e.dispatch_request=bad
        return r
    def broken_boot():
        r=restart()
        if index>=0 and ConfigStorageChecks.cases[index]==case:
            if fault=='rewrite_on_boot' and model.saved is not None:model.write_config(model.saved,model.saved_identities)
            if fault=='lose_on_boot':model.extension.items={};model.saved_hash=0
        return r
    a.prepare=fresh;model.restart_fixture=broken_boot
    r=ConfigStorageChecks(c,a).run();row=next(x for x in r['checks'] if x['id']=='OEP-CONFIG-STORAGE-'+case)
    assert row['status']=='failed' and row['error'].startswith('Violation:'),row


@pytest.mark.parametrize('missing',['state','seed','restart_fixture','prepare','storage_snapshot','change_catalog'])
def test_missing_controls_do_not_pass(missing):
    model,c=setup();a=SampleConfigStorageAdapter(model);setattr(a,missing,None)
    r=ConfigStorageChecks(c,a).run();assert r['checks'][2]['status']=='failed'
