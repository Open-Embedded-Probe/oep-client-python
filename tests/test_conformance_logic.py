import struct
import pytest
from oep_client.virtual_logic_model import OP, C, A, ST, MODE
from oep_client.registry import OUTCOMES
from spec_names import iface


def tags_of(name):
    return {c:dict(t) for c,t in iface(name)['tlv'].items()}

from oep_client.conformance import Checks
from oep_client.conformance_logic import LogicChecks
from oep_client.conformance_logic_sample import SampleLogicAdapter
from oep_client.virtual_logic_model import LogicModel
from test_conformance_registered_ops import setup as registered_setup


def setup():
    _,c=registered_setup();reg=c.reg
    for spec in reg['interface']:
        if spec['name']=='oep.probe.plan':spec['tlv']=tags_of(spec['name'])   # by name from the selected SPEC
        if spec['name']=='oep.fixture.logic':spec['tlv']=tags_of(spec['name'])   # by name from the selected SPEC
    model=LogicModel(boot_id=17);return model,Checks(model.handle,reg,'virtual-resource-1')


def test_logic_contract():
    model,c=setup();r=LogicChecks(c,SampleLogicAdapter(model)).run()
    assert r['status']=='passed',[(row['id'],row.get('error')) for row in r['checks'] if row['status']!='passed']
    assert len(r['checks'])==34 and not r['full_conformance']


def test_generic_declarations():
    from oep_client.conformance_interfaces import InterfaceChecks
    _,c=setup();r=InterfaceChecks(c).run();assert r['status']=='passed',r['checks']


def test_metadata():
    from oep_client.conformance_capture_metadata import CaptureMetadataChecks
    _,c=setup();r=CaptureMetadataChecks(c,{2:'oep.fixture.logic'}).run();assert r['status']=='passed',r['checks']


@pytest.mark.parametrize('kind',LogicChecks.cases)
def test_wire_faults_detected(kind):
    model,c=setup();original=model.handle
    def broken(req):
        reply=original(req)
        if int.from_bytes(req[3:5],'little'):
            if reply[3]==1:reply=reply[:4]+b'\1'+reply[5:]
            else:reply=reply[:4]+b'\3'
        return reply
    c.send=broken;checker=LogicChecks(c,SampleLogicAdapter(model));checker.cases=(kind,);r=checker.run()
    assert next(row for row in r['checks'] if row['id']=='OEP-LOGIC-'+kind)['status']=='failed'


def rewrite(result,tag,value=None):
    from oep_client.conformance import tlvs
    from oep_client.virtual_core import tlv
    return b''.join(tlv(t,v if t!=tag else value) for t,v in tlvs(result) if t!=tag or value is not None)


def read_fault(fault,payload,result):
    asked,maximum=struct.unpack_from('<QH',payload,4);position,flags,count=struct.unpack_from('<QBH',result)
    if fault=='future_keeps_position' and not count and not flags&2 and asked>position:return struct.pack('<QBH',asked,0,0)
    if fault=='gone_without_gap' and not count and flags&2:return struct.pack('<QBH',position,flags&~2,0)
    if fault=='max0_ignores_gap' and not maximum and flags&2:return struct.pack('<QBH',asked,0,0)
    return result


@pytest.mark.parametrize('fault,case',[
 ('query_applies','QUERY'),('wrong_pack','ONE-SHOT'),('generation_zero','GEN-EXHAUST'),('wrap_to_one','GEN-EXHAUST'),
 ('old_start_shape','ONE-SHOT'),('layout_count','QUERY'),('missing_actual_samples','QUERY'),('lone_pretrigger','CONTRACT'),
 ('mode3_accepted','CONTRACT'),('segments_unclamped','SAMPLES'),('future_keeps_position','READ'),('gone_without_gap','READ-GAP'),
 ('max0_ignores_gap','READ-GAP'),
 ('keep_old_data','GENERATION'),('no_resume','RELEASE'),('no_gap','REPEAT'),
 ('no_short','SHORT-STOP'),('wrong_stop_state','SHORT-STOP'),('discard_committed','ERROR'),
 ('error_retains_partial','ERROR'),('pin_output','ONE-SHOT'),('reset_generation','PLAN-RELEASE'),
])
def test_side_effect_faults_detected(fault,case):
    model,c=setup();install=model.install
    def faulty_install():
        install();e=model.extension;dispatch=e.dispatch_request;finish=e.finish;release=e.release_plans
        def broken_dispatch(fn,op,payload,tags):
            old_data=bytes(e.data);old_state=e.state
            if fn==2:
                if fault=='wrap_to_one' and op==OP['start'] and e.generation==0xffffffff and e.state in (ST['configured'],ST['done'],ST['error']):e.generation=0
                if fault=='lone_pretrigger' and op in (OP['configure'],OP['query']) and C['pretrigger'] in tags and C['trigger'] not in tags:tags={k:v for k,v in tags.items() if k!=C['pretrigger']}
                if fault=='mode3_accepted' and op in (OP['configure'],OP['query']) and tags.get(C['mode'],(0,b''))[1]==bytes((MODE['streaming'],)):tags={**tags,C['mode']:(C['mode'],bytes((MODE['one_shot'],))),C['samples']:(C['samples'],struct.pack('<I',8))}
            result=dispatch(fn,op,payload,tags)
            if fn==2:
                if fault=='query_applies' and op==OP['query']:e.config=e.settings(tags);e.state=ST['configured']
                if fault=='generation_zero' and op==OP['start']:e.generation=0;result=b'\0'*4
                if fault=='old_start_shape' and op==OP['start']:result=b'\0'*4+result
                if fault=='layout_count' and op in (OP['configure'],OP['query']):result=rewrite(result,A['layout'],b'\3\3\0\1\2')
                if fault=='missing_actual_samples' and op in (OP['configure'],OP['query']):result=rewrite(result,A['actual_samples'])
                if fault=='segments_unclamped' and op in (OP['configure'],OP['query']) and C['segments'] in tags:result=rewrite(result,A['actual_segments'],tags[C['segments']][1])
                if op==OP['read']:result=read_fault(fault,payload,result)
                if fault=='keep_old_data' and op==OP['start']:e.data[:]=old_data
                if fault=='no_resume' and op==OP['release']:e.state=ST['paused']
                if fault=='no_gap' and op==OP['release']:e.gap=False
                if fault=='wrong_stop_state' and op==OP['stop'] and old_state==ST['capturing']:e.state=ST['done']
            return result
        def broken_finish(short=False):
            finish(short)
            if fault=='wrong_pack' and e.data:e.data[0]^=1
            if fault=='no_short' and e.segments:
                row=e.segments[-1];e.segments[-1]=row[:6]+(0,)+row[7:]
            if fault=='pin_output':e.pins[1]=(4,0)
        def broken_release(functions):
            release(functions)
            if fault=='reset_generation':e.generation=0
        e.dispatch_request=broken_dispatch;e.finish=broken_finish;e.release_plans=broken_release
    model.install=faulty_install
    if fault in ('discard_committed','error_retains_partial'):
        original=model.logic_fail
        def broken_fail(error):
            original(error)
            if fault=='discard_committed':model.extension.data.clear();model.extension.segments.clear()
            else:model.extension.data.append(255)
        model.logic_fail=broken_fail
    checker=LogicChecks(c,SampleLogicAdapter(model));checker.cases=(case,);r=checker.run()
    assert next(row for row in r['checks'] if row['id']=='OEP-LOGIC-'+case)['status']=='failed'


@pytest.mark.parametrize('case',['REPLAY-START','REPLAY-RELEASE'])
def test_replay_cannot_repeat_side_effects(case):
    model,c=setup();original=model.handle;seen=set();operation=17 if case=='REPLAY-START' else 23
    def broken(req):
        reply=original(req)
        if int.from_bytes(req[3:5],'little')==2 and req[5]==operation:
            if req in seen:
                if case=='REPLAY-START':model.extension.clear(3);model.extension.generation+=1
                else:model.extension.segments.clear();model.extension.data.clear()
            seen.add(req)
        return reply
    c.send=broken;checker=LogicChecks(c,SampleLogicAdapter(model));checker.cases=(case,);r=checker.run()
    assert next(row for row in r['checks'] if row['id']=='OEP-LOGIC-'+case)['status']=='failed'


@pytest.mark.parametrize('method,args',[('logic_feed',([[True]],)),('logic_advance',(-1,)),('logic_fail',(2,)),('logic_seed',(True,))])
def test_invalid_controls_do_not_mutate(method,args):
    model,_=setup();before=model.logic_state()
    with pytest.raises(ValueError):getattr(model,method)(*args)
    assert model.logic_state()==before


def test_unused_layout_bits_are_not_required_zero():
    model,c=setup();r=LogicChecks(c,SampleLogicAdapter(model)).run();assert r['status']=='passed',r['checks']
    # The model always sets the unassigned bit2 for two-channel samples.
    checker=LogicChecks(c,SampleLogicAdapter(model));checker.cases=('ONE-SHOT',);r=checker.run();assert r['status']=='passed'


@pytest.mark.parametrize('invalid',['scope','selection'])
def test_preflight_blocks(invalid):
    model,c=setup();a=SampleLogicAdapter(model)
    if invalid=='scope':a.reset_scope='physical'
    else:a.selection={2:'oep.fixture.logic'}
    r=LogicChecks(c,a).run();assert r['checks'][0]['status']=='failed'
    assert all(row['status']=='blocked' for row in r['checks'][1:]);assert not a.trace


def test_future_empty_read_must_answer_write_pos():
    model,c=setup();original=model.handle
    def requested_position(req):
        reply=original(req)
        if int.from_bytes(req[3:5],'little')==2 and req[5]==21 and reply[3:5]==b'\1\0':
            asked=int.from_bytes(req[14:22],'little')
            if asked==999:reply=reply[:5]+asked.to_bytes(8,'little')+reply[13:]
        return reply
    c.send=requested_position;checker=LogicChecks(c,SampleLogicAdapter(model));checker.cases=('READ',);r=checker.run()
    assert next(row for row in r['checks'] if row['id']=='OEP-LOGIC-READ')['status']=='failed'


def test_omitted_repeat_segments_are_the_probe_choice():
    """capture §3.3 (a09fa45): mode 2 without segments - "省略時はprobeが決め、actual_segmentsは常に1以上"; a probe that
    picks 1 segment (not its capacity 2) passes SAMPLES."""
    model,c=setup();install=model.install
    def one_by_default():
        install();e=model.extension;settings=e.settings
        def chosen(tags):
            config=settings(tags)
            if config['mode']==MODE['repeat'] and C['segments'] not in tags:config['segments']=1
            return config
        e.settings=chosen
    model.install=one_by_default;model.install()
    checker=LogicChecks(c,SampleLogicAdapter(model));checker.cases=('SAMPLES',);r=checker.run()
    assert next(row for row in r['checks'] if row['id']=='OEP-LOGIC-SAMPLES')['status']=='passed',r['checks']


def one_byte_reads(c,fn=2,op=21,at=22):
    """A probe that returns at most 1 byte a read (capture §3.2: "read が実際に返す量は probe が決める（max 以下..."):
    every read request's max (the u16 at byte `at` of the request) is cut to 1 before the model answers."""
    original=c.send
    def send(req):
        req=bytes(req)
        if len(req)>=at+2 and int.from_bytes(req[3:5],'little')==fn and req[5]==op and int.from_bytes(req[at:at+2],'little')>1:
            req=req[:at]+b'\1\0'+req[at+2:]
        return original(req)
    c.send=send


def test_short_reads_are_the_probe_choice():
    model,c=setup();one_byte_reads(c);r=LogicChecks(c,SampleLogicAdapter(model)).run()
    assert r['status']=='passed',[(row['id'],row.get('error')) for row in r['checks'] if row['status']!='passed']
