import pytest
from oep_client.conformance_capture_serial import CaptureSerialChecks
from oep_client.conformance_capture_serial_sample import SampleCaptureSerialAdapter
from oep_client.conformance_capture_serial import reviewed_identity as actual_reviewed_identity

SPEC='/explicit/capture-serial-spec'
@pytest.fixture(autouse=True)
def selected_spec(monkeypatch):
    monkeypatch.setattr('oep_client.conformance_capture_serial.reviewed_identity',lambda path:{'commit':'unit-fixture'} if path==SPEC else None)

def test_capture_serial_wrap_and_release():
    r=CaptureSerialChecks(SampleCaptureSerialAdapter(),spec=SPEC).run()
    assert r['status']=='passed',r['checks']
    assert len(r['checks'])==5 and len(r['observations'])==4

@pytest.mark.parametrize('fault',[
    'scope','serial','position','data','generation','current','span','metadata_position',
    'page','read','refusal','replay','gap','partial','holder','fn'])
def test_serial_observation_faults(fault):
    adapter=SampleCaptureSerialAdapter();run=adapter.run_case
    def broken(case):
        r=run(case);states={x['label']:x['state'] for x in r['states']};trace=r['trace']
        if fault=='scope':r['seed_scope']='physical'
        if fault=='serial':states['wrapped']['serial']=0xffffffff
        if fault=='position':states['released']['base']&=0xffffffff
        if fault=='data':states['wrapped']['data_hex']='ff'
        if fault=='generation':states['wrapped']['generation']=2
        if fault=='current':states['future-release']['current']=[]
        if fault=='span':states['full']['segments'][0][0]=1<<30
        if fault=='metadata_position':states['full']['segments'][0][1]+=1
        if fault=='page':next(e for e in trace if e['op']==22)['reply_hex']='02050001000000'
        if fault=='read':e=next(e for e in trace if e['op']==21);e['reply_hex']=e['reply_hex'][:-2]+'ff'
        if fault=='refusal':e=next(e for e in trace if e['op']==23 and e['reply_hex'][6:10]=='0004');e['after']['current']=[[1,1]]
        if fault=='replay':next(e for e in trace if e.get('replay'))['after']['serial']=3
        if fault=='gap':states['wrapped']['segments'][-1][6]=0
        if fault=='partial':states['stopped']['segments'][0][2]=2
        if fault=='holder':states['stopped']['holder']=None
        if fault=='fn':next(e for e in trace if e['op']==22)['fn']=1
        return r
    adapter.run_case=broken;c=CaptureSerialChecks(adapter,spec=SPEC)
    c.check('FAULT',lambda:c.case(dict(kind='logic',seed=0xfffffffe)))
    assert c.checks[-1]['status']=='failed' and c.checks[-1]['error'].startswith('Violation:'),c.checks

@pytest.mark.parametrize('fault',['no_wrap','unsigned_release','discard_partial','corrupt_replay'])
def test_actual_capture_faults(monkeypatch,fault):
    from oep_client.virtual_logic_model import Logic
    from oep_client.virtual_core import Core
    if fault=='no_wrap':
        finish=Logic.finish
        def bad_finish(e,*args):
            before=e.serial;finish(e,*args)
            if before==0xffffffff:e.serial=before+1
        monkeypatch.setattr(Logic,'finish',bad_finish)
    if fault in ('unsigned_release','discard_partial'):
        dispatch=Logic.dispatch_request
        def bad_dispatch(e,fn,op,payload,tags):
            if fn==2 and op==23 and fault=='unsigned_release':
                import struct
                serial=struct.unpack_from('<I',payload,4)[0]
                e.segments[:]=[row for row in e.segments if row[0]>serial]
            result=dispatch(e,fn,op,payload,tags)
            if fn==2 and op==23 and fault=='discard_partial':e.current=[]
            return result
        monkeypatch.setattr(Logic,'dispatch_request',bad_dispatch)
    if fault=='corrupt_replay':
        handle=Core.handle
        def bad_handle(e,data,*args,**kwargs):
            replay=data[3:6]==b'\2\0\x17' and int.from_bytes(data[1:3],'little') in e.cache
            result=handle(e,data,*args,**kwargs)
            return result+b'x' if replay else result
        monkeypatch.setattr(Core,'handle',bad_handle)
    c=CaptureSerialChecks(SampleCaptureSerialAdapter(),spec=SPEC)
    c.check('FAULT',lambda:c.case(dict(kind='logic',seed=0xfffffffe)))
    assert c.checks[-1]['status']=='failed',c.checks

@pytest.mark.parametrize('missing',['run_case','source_sha256'])
def test_missing_serial_controls(missing):
    a=SampleCaptureSerialAdapter();setattr(a,missing,None)
    assert CaptureSerialChecks(a,spec=SPEC).run()['checks'][0]['status']=='failed'

@pytest.mark.parametrize('changed',['capture','common'])
def test_changed_reviewed_document_rejected(tmp_path,monkeypatch,changed):
    import hashlib
    import oep_client.conformance_capture_serial as module
    names={name:'interfaces/oep-if-'+name+'.ja.md' for name in ('capture','common')}
    hashes={}
    for name,path in names.items():
        file=tmp_path/path;file.parent.mkdir(exist_ok=True);file.write_text('reviewed '+name)
        hashes[path]=hashlib.sha256(file.read_bytes()).hexdigest()
    (tmp_path/names[changed]).write_text('changed')
    monkeypatch.setattr(module,'REVIEWED',hashes)
    monkeypatch.setattr(module,'core_identity',lambda path:{'commit':'unit-fixture'})
    with pytest.raises(Exception,match='reviewed capture serial document changed'):
        actual_reviewed_identity(tmp_path)
