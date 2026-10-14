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
    assert len(r['checks'])==8 and len(r['state_observations'])==7 and not r['full_conformance']


import pytest


def test_config_common_declarations():
    from oep_client.conformance_interfaces import InterfaceChecks
    model,c=setup();r=InterfaceChecks(c).run();assert r['status']=='passed',r['checks']


@pytest.mark.parametrize('case',ConfigChecks.cases)
def test_wrong_set_result_is_detected(case):
    model,c=setup();original=c.send
    def broken(req):
        out=original(req)
        if req[3:6]==b'\4\0\x11':return out[:3]+b'\1\1'+out[5:]
        return out
    c.send=broken;r=ConfigChecks(c,SampleConfigAdapter(model)).run()
    row=next(x for x in r['checks'] if x['id']=='OEP-CONFIG-'+case)
    assert row['status']=='failed' and row['error'].startswith('Violation:'),row


@pytest.mark.parametrize('fault,case',[
 ('erase_current','PERSISTENCE'),('no_save','PERSISTENCE'),('save_each_set','PERSISTENCE'),('wrong_saved_generation','PERSISTENCE'),
 ('count_same_contents','PERSISTENCE'),('boot_generation','PERSISTENCE'),('bit7_kept','PERSISTENCE'),
 ('idle_changes_live_pin','IDLE'),('idle_waits_free','IDLE'),('disable_drives','IDLE'),('unset_keeps_disable','IDLE'),('disabled_usable','IDLE'),
 ('refusal_changes_config','ATOMIC'),('refusal_counts','ATOMIC'),('accept_disabled_live','ATOMIC'),('normalized_tag','ATOMIC'),
 ('wrong_channel_tlv','ATOMIC'),('accept_duplicate','ATOMIC'),('tail_as_item','ATOMIC'),('ignore_critical_tail','ATOMIC'),
 ('wrap_generation','GENERATION'),('unset_missing_counts','GENERATION'),
 ('wrong_storage_cause','STORAGE-LIMIT'),('failed_save_changes_saved','STORAGE-LIMIT'),
 ('accept_item_zero','ATOMIC'),('accept_channel_past','ATOMIC'),('accept_wrong_length','ATOMIC'),('accept_unknown_mode','ATOMIC'),
 ('noop_touches_pins','ATOMIC')])
def test_semantic_faults(fault,case):
    model,c=setup();restart=model.restart_fixture
    def inject():
        from oep_client.virtual_core import Reject
        from oep_client.virtual_gpio_model import Gpio
        e=model.extension;original=e.dispatch_request;tags_of=e.request_tags
        if fault=='boot_generation':e.generation=2
        def bad_tags(fn,op,data):
            if fn==4 and op==17 and fault=='tail_as_item' and data.endswith(b'\x40\1\0x'):raise Reject(11,b'\x40')
            if fn==4 and op==17 and fault=='ignore_critical_tail' and data[-3:]==b'\xc0\0\0':data=data[:-3]
            if fn==4 and op==17 and fault=='accept_item_zero' and len(data)>=4 and not data[1]&127:return []   # taken, not malformed
            return tags_of(fn,op,data)
        e.request_tags=bad_tags
        def bad(fn,op,payload,tags):
            # probe-config §1: an item number 0, a channel past `channels`, a known item of another length and an
            # unknown idle mode are refused - these faults take them as a change instead
            sizes={2:None,3:3,7:2}
            if fn==4 and op==17 and (fault=='accept_channel_past' and any(len(v)>=2 and int.from_bytes(v[:2],'little')>=model.channels for raw,v in tags)
                    or fault=='accept_wrong_length' and any(sizes.get(raw&127) not in (None,len(v)) for raw,v in tags)
                    or fault=='accept_unknown_mode' and any(raw&127==3 and len(v)==3 and v[2]>4 for raw,v in tags)):
                e.generation+=1;return e.generation.to_bytes(4,'little')
            if fn==4 and op in (17,20) and fault=='noop_touches_pins':
                before=(dict(e.items),e.generation);out=original(fn,op,payload,tags)
                if (dict(e.items),e.generation)==before:e.pins={ch:0 for ch in e.pins}   # re-parked all the same (§2)
                return out
            if fn==4 and op==17 and fault=='accept_duplicate' and len(tags)==2 and tags[0][1][:2]==tags[1][1][:2] and tags[0][0]&127==tags[1][0]&127==2:tags=tags[1:]
            if fn==4 and op==17 and fault=='bit7_kept' and any(raw&128 for raw,v in tags):
                for raw,v in tags:e.items[raw,int.from_bytes(v[:2],'little')]=v
                e.generation+=1;return e.generation.to_bytes(4,'little')
            try:out=original(fn,op,payload,tags)
            except Exception as exc:
                p=getattr(exc,'payload',b'')
                if fn==4 and fault=='refusal_changes_config':e.items={}
                if fn==4 and fault=='refusal_counts':e.generation+=1
                if fn==4 and op==17 and fault=='accept_disabled_live' and p[:4]==b'\1\1\0\1':return b'\0'*4
                if fn==4 and fault=='normalized_tag' and getattr(exc,'reason',None)==11:exc.payload=bytes((p[0]&127,))+p[1:]
                if fn==4 and fault=='wrong_channel_tlv' and p[:4]==b'\1\1\0\5':exc.payload=p[:4]+b'\2\2\0\7\0'
                if fn==1 and fault=='disabled_usable':return Gpio.dispatch_request(e,fn,op,payload,tags)
                if fn==4 and fault=='wrap_generation' and p[:4]==b'\1\1\0\2':e.generation=1;return b'\1\0\0\0'
                if fn==4 and op==18 and fault=='wrong_storage_cause':exc.payload=b'\1\1\0\4'
                if fn==4 and op==18 and fault=='failed_save_changes_saved':model.saved=[(t,v) for (t,ch),v in e.items.items()]
                raise
            if fn==4:
                if fault=='erase_current' and op==19:e.items={};e.apply()
                if fault=='no_save' and op==18:model.saved=None;model.storage_state=model.saved_generation=0
                if fault=='save_each_set' and op==17:model.saved=[(t,v) for (t,ch),v in sorted(e.items.items())]
                if fault=='wrong_saved_generation' and op==21:out=out[:1]+b'\xff'*4+out[5:]
                if fault=='count_same_contents' and op==17:e.generation+=1;out=e.generation.to_bytes(4,'little')
                if fault=='unset_missing_counts' and op==20:e.generation+=1;out=e.generation.to_bytes(4,'little')
                if fault=='idle_changes_live_pin' and op==17 and any(raw&127==3 for raw,v in tags):e.pins[1]=3
                if fault=='idle_waits_free' and op==17:e.pins[2]=0
                if fault=='disable_drives' and op==17:e.pins[3]=0
                if fault=='unset_keeps_disable' and op==20:e.pins[3]=None
            return out
        e.dispatch_request=bad
    def fresh():
        r=restart();inject();return r
    model.restart_fixture=fresh
    r=ConfigChecks(c,SampleConfigAdapter(model)).run();row=next(x for x in r['checks'] if x['id']=='OEP-CONFIG-'+case)
    assert row['status']=='failed' and row['error'].startswith('Violation:'),row


@pytest.mark.parametrize('missing',['state','seed','restart_fixture','generation_at'])
def test_missing_controls_do_not_pass(missing):
    model,c=setup();a=SampleConfigAdapter(model);setattr(a,missing,None)
    r=ConfigChecks(c,a).run();assert r['checks'][2]['status']=='failed'


def test_unavailable_without_channel_tlv_passes():
    # core §4.3: the unavailable TLVs are attached as far as the probe knows them; only the cause is checked as present.
    model,c=setup();restart=model.restart_fixture
    def fresh():
        r=restart();e=model.extension;original=e.dispatch_request
        def bare(fn,op,payload,tags):
            try:return original(fn,op,payload,tags)
            except Exception as exc:
                if getattr(exc,'reason',None)==4:exc.payload=exc.payload[:4]
                raise
        e.dispatch_request=bare;return r
    model.restart_fixture=fresh;r=ConfigChecks(c,SampleConfigAdapter(model)).run()
    assert r['status']=='passed',[(x['id'],x.get('error')) for x in r['checks']]
