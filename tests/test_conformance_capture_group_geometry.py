import copy
import struct
import pytest
from oep_client import registry_legacy as registry
from oep_client.conformance_capture_group_geometry import CaptureGroupGeometryChecks
from oep_client.virtual_capture_group_geometry_model import geometry,fixtures
from test_conformance_registered_ops import setup as registered_setup


def checker():
    _,checks=registered_setup()
    for spec in checks.reg['interface']:
        if spec['name']=='oep.fixture.analog':spec['tlv']=copy.deepcopy(registry.FIXTURE_ANALOG.tlv)
        if spec['name']=='oep.fixture.logic':spec['tlv']=copy.deepcopy(registry.FIXTURE_LOGIC.tlv)
    return CaptureGroupGeometryChecks(checks)


@pytest.mark.parametrize('data',fixtures())
def test_immediate_group_geometry(data):
    result=checker().run(**data)
    assert result['status']=='passed',result['checks']
    assert len(result['checks'])==3 and not result['full_conformance']


def record(data,fn,**fields):
    names=['serial','position','samples','start','uncertainty','index','flags','generation']
    values=list(struct.unpack('<IQIQIIBI',data['first_segments'][fn]))
    for name,value in fields.items():values[names.index(name)]=value
    data['first_segments'][fn]=struct.pack('<IQIQIIBI',*values)


@pytest.mark.parametrize('fault',['short_flag','gap_flag','reserved_flags','wrong_count','wrong_origin','before_start','wrong_generation','serial','position','trigger_index','missing_track','short_record'])
def test_bad_first_segment_geometry_is_detected(fault):
    data=geometry(offsets=(0,5000))
    if fault=='short_flag':record(data,3,flags=2,samples=31)
    elif fault=='gap_flag':record(data,3,flags=1)
    elif fault=='reserved_flags':record(data,3,flags=8)
    elif fault=='wrong_count':record(data,3,samples=31)
    elif fault=='wrong_origin':record(data,3,start=20000000+5002)
    elif fault=='before_start':data=geometry(offsets=(0,0));data['sample_origins'][3]-=2;record(data,3,start=data['sample_origins'][3])
    elif fault=='wrong_generation':record(data,3,generation=1)
    elif fault=='serial':record(data,3,serial=1)
    elif fault=='position':record(data,3,position=1)
    elif fault=='trigger_index':record(data,3,index=2)
    elif fault=='missing_track':del data['first_segments'][3]
    else:data['first_segments'][3]=data['first_segments'][3][:-1]
    result=checker().run(**data)
    assert result['checks'][-1]['status']=='failed',result


@pytest.mark.parametrize('fault',['order','count','zero_group_gen','zero_track_gen','no_time','status_gen','status_start','status_state','legacy_status','legacy_start','short_start','short_status','bad_rate','bad_samples','unknown_fn','bad_tolerance'])
def test_bad_group_context_or_status_is_detected(fault):
    data=geometry()
    if fault=='order':data['start_payload']=data['start_payload'][:13]+data['start_payload'][19:]+data['start_payload'][13:19]
    elif fault=='count':data['start_payload']=data['start_payload'][:12]+b'\1'+data['start_payload'][13:]
    elif fault=='zero_group_gen':data['start_payload']=data['start_payload'][:8]+b'\0'*4+data['start_payload'][12:]
    elif fault=='zero_track_gen':data['start_payload']=data['start_payload'][:15]+b'\0'*4+data['start_payload'][19:]
    elif fault=='no_time':data['start_payload']=b'\xff'*8+data['start_payload'][8:]
    elif fault.startswith('status_'):
        row=list(struct.unpack('<BQI',data['status_payload']));at={'status_gen':2,'status_start':1,'status_state':0}[fault]
        row[at]+=1;data['status_payload']=struct.pack('<BQI',*row)
    elif fault=='legacy_status':data['status_payload']=struct.pack('<BQQHI',4,20000000,(1<<64)-1,0,11)
    elif fault=='legacy_start':data['start_payload']=b'\0'*4+data['start_payload']
    elif fault=='short_start':data['start_payload']=data['start_payload'][:12]
    elif fault=='short_status':data['status_payload']=data['status_payload'][:12]
    elif fault=='bad_rate':data['configure_payloads'][3]=('oep.fixture.logic',b'\x50\x08\0'+b'\0'*8+b'\x52\x04\0'+struct.pack('<I',32))
    elif fault=='bad_samples':data['configure_payloads'][3]=('oep.fixture.logic',b'\x50\x08\0'+struct.pack('<II',333,1)+b'\x52\x04\0'+b'\0'*4)
    elif fault=='unknown_fn':data['members']=(2,5)
    else:data['timestamp_tolerance_ns']=-1
    assert checker().run(**data)['status']=='failed'


def test_uncertainty_is_not_used_as_unbounded_geometry_tolerance():
    data=geometry();record(data,3,start=20000000+100,uncertainty=0xffffffff)
    assert checker().run(**data)['checks'][-1]['status']=='failed'


@pytest.mark.parametrize('rates',[(1000,1500),(1500,1000),(1001,1333)])
@pytest.mark.parametrize('order',['logic-first','analog-first'])
def test_geometry_from_real_virtual_endpoint_wire(rates,order):
    from oep_client import capture as c,core
    from test_virtual_bench_capture import bench
    ep,hst,logic,clock=bench();adc=c.AnalogCapture(hst);group=c.CaptureGroup(hst)
    tracks=[logic,adc] if order=='logic-first' else [adc,logic]
    core.plan_apply(hst,[(logic.fn,0,20),(logic.fn,1,21),(adc.fn,1,17),(adc.fn,3,19)])
    wire={};send=hst.send
    def observe(req):
        reply=send(req)
        if reply[3:5]==b'\1\0':wire[(int.from_bytes(req[3:5],'little'),req[5])]=reply[5:]
        return reply
    hst.send=observe
    for track,rate in zip(tracks,rates):track.configure(rate=rate,samples=128)
    payloads={track.fn:('oep.fixture.analog' if track is adc else 'oep.fixture.logic',wire[(track.fn,track.CONFIGURE)]) for track in tracks}
    group.bind(tracks);start_ns=group.start(tracks);start=wire[(group.fn,group.START)]
    clock.t=650;assert group.status().state==c.STATE['done'];status=wire[(group.fn,group.STATUS)]
    records={}
    for track in tracks:
        track.segments();body=wire[(track.fn,track.SEGMENTS)]
        assert body[:2]==b'\0\1';records[track.fn]=body[2:39]
    origins={track.fn:start_ns+(5000 if track is adc else 0) for track in tracks}   # the bench's known ADC first-conversion offset
    result=checker().run(configure_payloads=payloads,start_payload=start,status_payload=status,first_segments=records,
                         members=[track.fn for track in tracks],sample_origins=origins)
    assert result['status']=='passed',result['checks']
