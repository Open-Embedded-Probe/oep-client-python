import copy
import struct
import pytest
from oep_client.conformance import Checks
from oep_client.conformance_capture_timeline import CaptureTimelineChecks
from oep_client.conformance_capture_timeline_sample import SampleCaptureTimelineAdapter
from oep_client.virtual_capture_timeline_model import CaptureTimelineModel
from test_conformance_registered_ops import setup as registered_setup
from spec_names import iface, tags


def tags_of(name):
    return {c:dict(t) for c,t in iface(name)['tlv'].items()}


# The capture family's tags and events by name from the selected SPEC (tests/spec_names.py).
_CTX=('configure','configure_answer','status_answer','data','describe')
TRACK={c:tags('oep.fixture.logic',c) for c in _CTX}
ANALOG={c:tags('oep.fixture.analog',c) for c in _CTX+('calibration_answer',)}
EVENTS=dict(iface('oep.fixture.logic')['event'])
TAGS={'oep.probe.plan':(tags_of('oep.probe.plan'),{}),
      'oep.fixture.analog':(ANALOG,EVENTS),'oep.fixture.logic':(TRACK,EVENTS),
      'oep.fixture.capture-group':({'bind':{},**tags_of('oep.fixture.capture-group')},dict(iface('oep.fixture.capture-group')['event']))}


def setup():
    _,old=registered_setup();reg=old.reg
    reg['core']['tlv']['unavailable_payload']={'cause':1,'fn':5}
    for spec in reg['interface']:
        if spec['name'] in TAGS:spec['tlv'],spec['event']=copy.deepcopy(TAGS[spec['name']])
    model=CaptureTimelineModel(boot_id=17)
    return model,Checks(model.handle,reg,'virtual-resource-1')


def run(model,checks,scenario,checker=CaptureTimelineChecks):
    instance=checker(checks,SampleCaptureTimelineAdapter(model));instance.scenarios=(scenario,)
    return instance.run()


def failed(result,name='OEP-TIMELINE-0'):
    return next(r for r in result['checks'] if r['id']==name)['status']=='failed'


@pytest.mark.parametrize('scenario',CaptureTimelineChecks.scenarios)
def test_cross_rate_acquisition(scenario):
    model,checks=setup();result=run(model,checks,scenario)
    assert result['status']=='passed',[(r['id'],r.get('error')) for r in result['checks'] if r['status']!='passed']
    assert not result['full_conformance']


@pytest.mark.parametrize('kind',['declarations','metadata'])
def test_timeline_declarations(kind):
    from oep_client.conformance_interfaces import InterfaceChecks
    from oep_client.conformance_capture_metadata import CaptureMetadataChecks
    model,checks=setup()
    result=(InterfaceChecks(checks) if kind=='declarations' else CaptureMetadataChecks(checks,{2:'oep.fixture.analog',3:'oep.fixture.logic',4:'oep.fixture.capture-group'})).run()
    assert result['status']=='passed',result['checks']


@pytest.mark.parametrize('method,args',[('timeline_advance_to',(-1,)),('timeline_advance_to',(True,)),('timeline_advance_to',(2000000000,)),
                                        ('timeline_fail',(2,1)),('timeline_fail',(7,1)),('timeline_fail_next_start',(2,9)),('timeline_seed',(1<<32,0,0))])
def test_bad_scheduler_controls_do_not_mutate(method,args):
    model,_=setup();before=model.timeline_state()
    with pytest.raises(ValueError):getattr(model,method)(*args)
    assert model.timeline_state()==before


@pytest.mark.parametrize('scenario',CaptureTimelineChecks.scenarios)
def test_wire_failure_is_detected(scenario):
    model,checks=setup();original=model.handle
    def broken(req):
        reply=original(req)
        if int.from_bytes(req[3:5],'little')==4:reply=reply[:4]+bytes(((reply[4]+1)&255,))+reply[5:]
        return reply
    checks.send=broken;assert failed(run(model,checks,scenario))


ONE_SHOT=(1,(2,3),(1000,333),1234567)
STREAM=(3,(2,3),(1000,333),1234567)
STREAM_LOGIC=(3,(3,2),(1000,333),0)


def patch_tracks(model,fault,hook):
    """Wrap every installed fixture so restart keeps the injected fault."""
    install=model.install
    def faulty_install():
        install()
        for track in model.extension.tracks.values():hook(model.extension,track)
    model.install=faulty_install


def track_fault(fault):
    def hook(group,track):
        flush,read,finish,dispatch,encode=track.flush,track.read,track.finish,track.dispatch,track.encode
        if fault=='future_sample':track.write_pos=lambda t=track:t.position+t.confirmed(len(t.current)+(1 if t.state==3 else 0))
        if fault=='unconfirmed_byte':track.confirmed=lambda n,t=track:t.full(n)
        if fault=='wrong_data':track.encode=lambda rows,t=track:bytes(b^1 for b in encode(rows))
        if fault in ('read_requested_position','read_no_gap','read_gap_position','read_zero_max'):
            def broken_read(payload,t=track):
                out=read(payload);position,flags,count=struct.unpack_from('<QBH',out);generation,requested,maximum=struct.unpack('<IQH',payload)
                if fault=='read_requested_position' and requested>=t.write_pos():return struct.pack('<QBH',requested,0,0)
                if fault=='read_no_gap' and flags&2:return struct.pack('<QBH',position,flags&~2,count)+out[11:]
                if fault=='read_gap_position' and flags&2 and not count:return struct.pack('<QBH',requested,flags,0)
                if fault=='read_zero_max' and maximum==0 and requested<t.write_pos():return struct.pack('<QBH',requested,flags|2,0)
                return out
            track.read=broken_read
        if fault=='segment_start':
            def broken_finish(short=False,t=track):
                finish(short)
                if t.segments:row=t.segments[-1];t.segments[-1]=row[:3]+(row[3]+1,)+row[4:]
            track.finish=broken_finish
        if fault in ('cross_segment','no_segment_info','info_samples','info_start','generation_tlv','segment_event','extra_frame_byte'):
            def broken_flush(t=track):
                before=len(t.out);flush()
                for k in range(before,len(t.out)):
                    frame=t.out[k]
                    if frame[0]!=4:continue
                    size=struct.unpack_from('<H',frame,13)[0];tail=frame[15+size:]
                    info=list(struct.unpack('<QIQI',tail[10:34]))
                    if fault=='no_segment_info':tail=tail[:7]
                    if fault=='info_samples':info[1]+=1
                    if fault=='info_start':info[2]+=1000
                    if fault=='generation_tlv':tail=tail[:3]+struct.pack('<I',t.generation+1)+tail[7:]
                    if fault in ('no_segment_info','generation_tlv'):t.out[k]=frame[:15+size]+tail;continue
                    t.out[k]=frame[:15+size]+tail[:10]+struct.pack('<QIQI',*info)
                if fault=='cross_segment' and not getattr(t,'faulted',False):
                    frames=[k for k in range(before,len(t.out)) if t.out[k][0]==4]
                    for i,j in zip(frames,frames[1:]):
                        a,b=t.out[i],t.out[j];sa=struct.unpack_from('<H',a,13)[0];sb=struct.unpack_from('<H',b,13)[0]
                        if a[15+sa+3:15+sa+11]!=b[15+sb+3:15+sb+11] and len(a)+sb<=t.ep.probe.max_frame:
                            t.out[i]=a[:13]+struct.pack('<H',sa+sb)+a[15:15+sa]+b[15:15+sb]+a[15+sa:];del t.out[j]
                            for m in [m for m in range(j,len(t.out)) if struct.unpack_from('<H',t.out[m],1)[0]==t.fn]:t.out[m]=t.out[m][:3]+struct.pack('<H',struct.unpack_from('<H',t.out[m],3)[0]-1)+t.out[m][5:]
                            t.seq=(t.seq-1)&0xffff;t.faulted=True;break
                if fault=='segment_event' and t.config['mode']==3 and t.segments and t.subscribed and not getattr(t,'faulted',False):
                    t.faulted=True;t.event(1,struct.pack('<IQIQIIBI',*t.segments[0]))
            track.flush=broken_flush
    return hook


@pytest.mark.parametrize('fault,scenario',[
    ('future_sample',ONE_SHOT),('unconfirmed_byte',(1,(3,2),(333,1000),0)),('wrong_data',ONE_SHOT),('segment_start',ONE_SHOT),
    ('read_requested_position',ONE_SHOT),('read_no_gap',STREAM),('read_gap_position',STREAM),('read_zero_max',ONE_SHOT),
    ('cross_segment',STREAM_LOGIC),('no_segment_info',STREAM),('info_samples',STREAM),('info_start',STREAM),
    ('generation_tlv',STREAM),('segment_event',STREAM),('unconfirmed_byte',STREAM_LOGIC),('wrong_data',STREAM)])
def test_track_faults_are_detected(fault,scenario):
    model,checks=setup();patch_tracks(model,fault,track_fault(fault))
    result=run(model,checks,scenario);assert failed(result),result['checks']


@pytest.mark.parametrize('fault',['zero_generation','start_without_subscription','keep_subscription','stale_start_ns'])
def test_group_faults_are_detected(fault):
    model,checks=setup();install=model.install
    def faulty_install():
        install();e=model.extension;start=e.start;release=e.release
        def broken_start():
            if fault=='start_without_subscription':
                for fn in e.bound:e.tracks[fn].subscribed=True
            out=start()
            if fault=='zero_generation':e.generation=0;out=out[:8]+struct.pack('<I',0)+out[12:]
            if fault=='stale_start_ns':e.start_ns+=1
            return out
        def broken_release():
            kept={fn:t.subscribed for fn,t in e.tracks.items()};release()
            if fault=='keep_subscription':
                for fn,t in e.tracks.items():t.subscribed=kept[fn]
        e.start=broken_start;e.release=broken_release
    model.install=faulty_install
    scenario=STREAM if fault in ('start_without_subscription','keep_subscription') else ONE_SHOT
    result=run(model,checks,scenario);assert failed(result),result['checks']


@pytest.mark.parametrize('invalid',['scope','selection'])
def test_timeline_preflight_blocks(invalid):
    model,checks=setup();adapter=SampleCaptureTimelineAdapter(model)
    if invalid=='scope':adapter.reset_scope='physical'
    else:adapter.selection={4:'oep.fixture.capture-group'}
    result=CaptureTimelineChecks(checks,adapter).run()
    assert result['checks'][0]['status']=='failed' and all(r['status']=='blocked' for r in result['checks'][1:])
    assert not adapter.trace


@pytest.mark.parametrize('scenario',CaptureTimelineChecks.scenarios)
def test_short_reads_are_the_probe_choice(scenario):
    """capture §3.2: the probe decides how much one read returns (max or less); the checker reads on while more is set."""
    from test_conformance_logic import one_byte_reads
    model,checks=setup()
    for fn in range(1,8):one_byte_reads(checks,fn=fn)
    result=run(model,checks,scenario)
    assert result['status']=='passed',[(r['id'],r.get('error')) for r in result['checks'] if r['status']!='passed']
