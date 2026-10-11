import pytest
from oep_client.conformance import Checks
from oep_client.conformance_config_paging import ConfigPagingChecks
from oep_client.conformance_config_paging_sample import SampleConfigPagingAdapter
from oep_client.virtual_config_paging_model import ConfigPagingModel
from test_conformance_registered_ops import setup as base_setup


def setup(frame=64):
    _,base=base_setup();model=ConfigPagingModel(max_frame=frame)
    return model,Checks(model.handle,base.reg,'virtual-resource-1')


@pytest.mark.parametrize('frame',[64,128,256])
def test_config_paging_and_hash_recovery(frame):
    model,c=setup(frame);r=ConfigPagingChecks(c,SampleConfigPagingAdapter(model)).run()
    assert r['status']=='passed',[(x['id'],x.get('error')) for x in r['checks']]
    assert len(r['checks'])==8 and len(r['state_observations'])==5 and not r['full_conformance']
    import json
    json.dumps(r)  # The all-model runner must be able to archive this evidence.


@pytest.mark.parametrize('frame',[64,128,256])
def test_common_declarations(frame):
    from oep_client.conformance_interfaces import InterfaceChecks
    _,c=setup(frame);r=InterfaceChecks(c).run();assert r['status']=='passed',r['checks']


@pytest.mark.parametrize('frame',[64,128,256])
@pytest.mark.parametrize('case',ConfigPagingChecks.cases)
def test_wrong_page_result_is_detected(frame,case):
    model,c=setup(frame);a=SampleConfigPagingAdapter(model);prepare=a.prepare;send=c.send;index=-1
    def fresh():
        nonlocal index
        index+=1;return prepare()
    def broken(req):
        out=send(req)
        if index>=0 and ConfigPagingChecks.cases[index]==case and req[3:6]==b'\4\0\x10':return out[:3]+b'\1\1'+out[5:]
        return out
    a.prepare=fresh;c.send=broken;r=ConfigPagingChecks(c,a).run()
    row=next(x for x in r['checks'] if x['id']=='OEP-CONFIG-PAGING-'+case)
    assert row['status']=='failed' and row['error'].startswith('Violation:'),row


@pytest.mark.parametrize('fault,case',[
    ('byte_key_order','DENSE'),('byte_offset','FIRST'),('skip_item','DENSE'),('duplicate_item','DENSE'),
    ('early_end','DENSE'),('empty_more','EMPTY'),('nonboolean_more','EMPTY'),('changing_hash','DENSE'),
    ('old_hash','CHANGED'),('split_tlv','DENSE'),('critical_bit','DENSE'),('frame_overflow','DENSE'),
    ('read_changes_config','READONLY'),('read_changes_pin','READONLY'),('read_changes_lease','READONLY'),('read_changes_cache','READONLY')])
def test_semantic_faults_are_detected(fault,case):
    model,c=setup();a=SampleConfigPagingAdapter(model);prepare=a.prepare;index=-1
    def fresh():
        nonlocal index
        index+=1;r=prepare()
        if ConfigPagingChecks.cases[index]!=case:return r
        e=model.extension;original=e.dispatch_request;old_hash=None
        if fault=='byte_key_order':
            from oep_client.virtual_core import tlv
            e.canonical=lambda:[tlv(tag,v) for (tag,k),v in sorted(e.items.items())]
        def bad(fn,op,payload,tags):
            nonlocal old_hash
            if fn==4 and op==16 and fault=='byte_offset':payload=(int.from_bytes(payload,'little')//37).to_bytes(2,'little')
            out=original(fn,op,payload,tags)
            if fn!=4 or op!=16:return out
            from oep_client.conformance import tlvs
            from oep_client.conformance_config import item
            rows=tlvs(out[5:]);first=int.from_bytes(payload,'little')
            if fault=='skip_item' and rows:out=out[:5]+b''.join(item(t,v) for t,v in rows[1:])
            if fault=='duplicate_item' and len(rows)>1:out=out[:5]+b''.join(item(t,v) for t,v in [rows[0],rows[0],*rows[2:]])
            if fault=='early_end':out=b'\0'+out[1:]
            if fault=='empty_more':out=b'\1'+out[1:]
            if fault=='nonboolean_more':out=b'\2'+out[1:]
            if fault=='changing_hash' and first:out=out[:1]+((int.from_bytes(out[1:5],'little')+1)&0xffffffff).to_bytes(4,'little')+out[5:]
            if fault=='old_hash':
                if old_hash is None:old_hash=out[1:5]
                out=out[:1]+old_hash+out[5:]
            if fault=='split_tlv' and rows:out=out[:-1]
            if fault=='critical_bit' and rows:out=out[:5]+bytes((out[5]|128,))+out[6:]
            if fault=='frame_overflow':out+=b'\x7f\0\0'
            if fault=='read_changes_config':e.items[(2,b'\0\0')]=b'\0\0changed'
            if fault=='read_changes_pin':e.pins[1]=(0,0)
            if fault=='read_changes_lease':model.ep.current_core.deadline+=1
            if fault=='read_changes_cache':model.ep.current_core.cache[50000]=(None,None)
            return out
        e.dispatch_request=bad;return r
    a.prepare=fresh;r=ConfigPagingChecks(c,a).run();row=next(x for x in r['checks'] if x['id']=='OEP-CONFIG-PAGING-'+case)
    assert row['status']=='failed' and row['error'].startswith('Violation:'),row


@pytest.mark.parametrize('fault',['drop','duplicate','wrong_hash','never_restart','byte_cursor','return_old_page'])
def test_host_recovery_faults_are_detected(fault):
    model,c=setup();a=SampleConfigPagingAdapter(model);read=a.read_all
    def broken(source):
        if fault in ('never_restart','byte_cursor','return_old_page'):
            from oep_client.conformance import tlvs
            from oep_client.conformance_config import item
            rows=[];pages=[]
            for _ in range(30):
                first=sum(len(item(t,v)) for t,v in rows) if fault=='byte_cursor' else len(rows)
                p=source(first);part=tlvs(p[5:]);pages.append(first);rows+=part
                if fault=='return_old_page' or not p[0]:break
            return dict(items=rows,hash=int.from_bytes(p[1:5],'little'),restarts=0,pages=pages)
        r=read(source)
        if fault=='drop' and r['items']:r['items']=r['items'][1:]
        if fault=='duplicate' and r['items']:r['items']=r['items']+[r['items'][0]]
        if fault=='wrong_hash':r['hash']^=1
        return r
    a.read_all=broken;r=ConfigPagingChecks(c,a).run()
    expected='CHANGED' if fault=='never_restart' else 'DENSE'
    row=next(x for x in r['checks'] if x['id']=='OEP-CONFIG-PAGING-'+expected)
    assert row['status']=='failed' and row['error'].startswith('Violation:'),row


@pytest.mark.parametrize('missing',['state','seed','restart_fixture','prepare','read_all'])
def test_missing_controls_do_not_pass(missing):
    model,c=setup();a=SampleConfigPagingAdapter(model);setattr(a,missing,None)
    r=ConfigPagingChecks(c,a).run();assert r['checks'][2]['status']=='failed'


@pytest.mark.parametrize('defect',['frame','channels'])
def test_different_profile_is_not_silently_accepted(defect):
    model,c=setup()
    if defect=='frame':model.ep.probe.max_frame=65
    else:
        from oep_client.virtual_core import tlv
        model.ep.static[0]=tuple(row for row in model.ep.static[0] if row[0]!=67)+(tlv(67,b'\10\0'),)
    r=ConfigPagingChecks(c,SampleConfigPagingAdapter(model)).run();assert r['checks'][2]['status']=='failed'


@pytest.mark.parametrize('defect',['fixed','tlv','progress','churn'])
def test_host_reader_refuses_broken_or_unstable_pages(defect):
    from oep_client.conformance import Violation
    from oep_client.conformance_config_paging_sample import read_config_pages
    calls=[]
    def page(first):
        calls.append(first)
        if defect=='fixed':return b'\0'*4
        if defect=='tlv':return b'\1'+b'\0'*4+b'\2\5\0x'
        if defect=='progress':return b'\1'+b'\0'*4
        return b'\1'+len(calls).to_bytes(4,'little')+b'\2\3\0\0\0a'
    with pytest.raises(Violation):read_config_pages(page,max_restarts=1)
    if defect=='churn':assert calls==[0,1,0,1]
