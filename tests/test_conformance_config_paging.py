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
def test_config_paging_and_generation_recovery(frame):
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
    ('early_end','DENSE'),('empty_more','EMPTY'),('nonboolean_more','EMPTY'),('changing_generation','DENSE'),
    ('old_generation','CHANGED'),('split_item','DENSE'),('critical_bit','DENSE'),('frame_overflow','DENSE'),
    ('count_past_items','DENSE'),('tail_counted_as_item','DENSE'),('generation_per_label','DENSE'),
    ('read_changes_config','READONLY'),('read_changes_pin','READONLY'),('read_changes_lease','READONLY'),('read_changes_cache','READONLY')])
def test_semantic_faults_are_detected(fault,case):
    model,c=setup();a=SampleConfigPagingAdapter(model);prepare=a.prepare;index=-1
    def fresh():
        nonlocal index
        index+=1;r=prepare()
        if ConfigPagingChecks.cases[index]!=case:return r
        e=model.extension;original=e.dispatch_request;old=None
        if fault=='byte_key_order':
            from oep_client.virtual_core import tlv
            e.canonical=lambda:[tlv(tag,v) for (tag,ch),v in sorted(e.items.items(),key=lambda kv:(kv[0][0],kv[1][:2]))]
        def bad(fn,op,payload,tags):
            nonlocal old
            if fn==4 and op==16 and fault=='byte_offset':payload=(int.from_bytes(payload,'little')//37).to_bytes(2,'little')
            out=original(fn,op,payload,tags)
            if fn==4 and op==17 and fault=='generation_per_label' and e.generation>2:e.generation-=1;out=e.generation.to_bytes(4,'little')
            if fn!=4 or op!=16:return out
            from oep_client.conformance_config import item,split_items
            rows,_=split_items(out[6:],out[5]);first=int.from_bytes(payload,'little');head=out[:5]
            def again(items):return head+bytes((len(items),))+b''.join(item(t,v) for t,v in items)
            if fault=='skip_item' and rows:out=again(rows[1:])
            if fault=='duplicate_item' and len(rows)>1:out=again([rows[0],rows[0],*rows[2:]])
            if fault=='early_end':out=b'\0'+out[1:]
            if fault=='empty_more':out=b'\1'+out[1:]
            if fault=='nonboolean_more':out=b'\2'+out[1:]
            if fault=='changing_generation' and first:out=out[:1]+((int.from_bytes(out[1:5],'little')+1)&0xffffffff).to_bytes(4,'little')+out[5:]
            if fault=='old_generation':
                if old is None:old=out[1:5]
                out=out[:1]+old+out[5:]
            if fault=='split_item' and rows:out=out[:-1]
            if fault=='critical_bit' and rows:out=out[:6]+bytes((out[6]|128,))+out[7:]
            if fault=='frame_overflow':out+=b'\x7f\0\0'
            if fault=='count_past_items' and rows:out=out[:5]+bytes((out[5]+1,))+out[6:]
            if fault=='tail_counted_as_item' and rows:out=out[:5]+bytes((out[5]-1,))+out[6:]
            if fault=='read_changes_config':e.items[(2,0)]=b'\0\0changed'
            if fault=='read_changes_pin':e.pins[1]=3
            if fault=='read_changes_lease':model.ep.current_core.deadline+=1
            if fault=='read_changes_cache':model.ep.current_core.cache[50000]=(None,None)
            return out
        e.dispatch_request=bad;return r
    a.prepare=fresh;r=ConfigPagingChecks(c,a).run();row=next(x for x in r['checks'] if x['id']=='OEP-CONFIG-PAGING-'+case)
    assert row['status']=='failed' and row['error'].startswith('Violation:'),row


@pytest.mark.parametrize('frame',[64,256])
def test_answer_tail_is_not_items(frame):
    # A get answer's own noncritical TLV tail after the counted items is not an item and passes.
    model,c=setup(frame);a=SampleConfigPagingAdapter(model);prepare=a.prepare
    def fresh():
        r=prepare();e=model.extension;inner=e.dispatch_request
        def tail(fn,op,payload,tags):
            out=inner(fn,op,payload,tags)
            return out+b'\x7f\0\0' if fn==4 and op==16 and len(out)+8<=model.ep.probe.max_frame else out
        e.dispatch_request=tail;return r
    a.prepare=fresh;r=ConfigPagingChecks(c,a).run()
    assert r['status']=='passed',[(x['id'],x.get('error')) for x in r['checks']]


@pytest.mark.parametrize('fault',['drop','duplicate','wrong_generation','never_restart','item_cursor','return_old_page'])
def test_host_recovery_faults_are_detected(fault):
    model,c=setup();a=SampleConfigPagingAdapter(model);read=a.read_all
    def broken(source):
        if fault in ('never_restart','item_cursor','return_old_page'):
            from oep_client.conformance_config import item,page
            rows=[];pages=[]
            for _ in range(30):
                first=sum(len(item(t,v)) for t,v in rows) if fault=='item_cursor' else len(rows)
                more,g,part=page(source(first));pages.append(first);rows+=part
                if fault=='return_old_page' or not more:break
            return dict(items=rows,generation=g,restarts=0,pages=pages)
        r=read(source)
        if fault=='drop' and r['items']:r['items']=r['items'][1:]
        if fault=='duplicate' and r['items']:r['items']=r['items']+[r['items'][0]]
        if fault=='wrong_generation':r['generation']^=1
        return r
    a.read_all=broken;r=ConfigPagingChecks(c,a).run()
    expected='CHANGED' if fault=='never_restart' else 'DENSE'
    row=next(x for x in r['checks'] if x['id']=='OEP-CONFIG-PAGING-'+expected)
    assert row['status']=='failed' and row['error'].startswith('Violation:'),row


@pytest.mark.parametrize('missing',['state','seed','restart_fixture','generation_at','prepare','read_all'])
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


@pytest.mark.parametrize('defect',['fixed','item','progress','bit7','churn'])
def test_host_reader_refuses_broken_or_unstable_pages(defect):
    from oep_client.conformance import Violation
    from oep_client.conformance_config_paging_sample import read_config_pages
    calls=[]
    def page(first):
        calls.append(first)
        if defect=='fixed':return b'\0'*5
        if defect=='item':return b'\1'+b'\0'*4+b'\1\2\5\0x'
        if defect=='progress':return b'\1'+b'\0'*4+b'\0'
        if defect=='bit7':return b'\0'+b'\0'*4+b'\1\x82\3\0\0\0a'
        return b'\1'+len(calls).to_bytes(4,'little')+b'\1\2\3\0\0\0a'
    with pytest.raises(Violation):read_config_pages(page,max_restarts=1)
    if defect=='churn':assert calls==[0,1,0,1]
