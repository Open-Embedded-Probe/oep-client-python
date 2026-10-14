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
def test_fractional_group_geometry(data):
    result=checker().run(**data)
    assert result['status']=='passed',result['checks']
    assert len(result['checks'])==3 and not result['full_conformance']


def record(data,fn,**fields):
    names=['serial','position','samples','start','uncertainty','index','flags','generation']
    values=list(struct.unpack('<IQIQIIBI',data['first_segments'][fn]))
    for name,value in fields.items():values[names.index(name)]=value
    data['first_segments'][fn]=struct.pack('<IQIQIIBI',*values)


@pytest.mark.parametrize('fault',['floor_pretrigger','missing_short','wrong_count','wrong_origin','wrong_index','wrong_generation','serial','position','reserved_flags','missing_track','short_record','skip_trigger_marker'])
def test_bad_first_segment_geometry_is_detected(fault):
    data=geometry(pre=7,trigger_ns=50000000)
    if fault=='floor_pretrigger':record(data,3,index=2,start=45045045)
    elif fault=='missing_short':data=geometry(pre=7,trigger_ns=0);record(data,3,flags=0)
    elif fault=='wrong_count':record(data,3,samples=31)
    elif fault=='wrong_origin':record(data,3,start=1)
    elif fault=='wrong_index':record(data,3,index=4)
    elif fault=='wrong_generation':record(data,3,generation=1)
    elif fault=='serial':record(data,3,serial=1)
    elif fault=='position':record(data,3,position=1)
    elif fault=='reserved_flags':record(data,3,flags=8)
    elif fault=='missing_track':del data['first_segments'][3]
    elif fault=='short_record':data['first_segments'][3]=data['first_segments'][3][:-1]
    else:record(data,3,index=0xffffffff)
    result=checker().run(**data)
    assert result['checks'][-1]['status']=='failed',result


@pytest.mark.parametrize('fault',['order','count','zero_group_gen','zero_track_gen','status_gen','status_start','status_trigger','status_fn','status_state','blocking','short_start','short_status','bad_rate','bad_samples','unknown_fn','no_history'])
def test_bad_group_context_or_status_is_detected(fault):
    data=geometry()
    if fault=='order':data['start_payload']=data['start_payload'][:17]+data['start_payload'][23:]+data['start_payload'][17:23]
    elif fault=='count':data['start_payload']=data['start_payload'][:16]+b'\1'+data['start_payload'][17:]
    elif fault=='zero_group_gen':data['start_payload']=data['start_payload'][:12]+b'\0'*4+data['start_payload'][16:]
    elif fault=='zero_track_gen':data['start_payload']=data['start_payload'][:19]+b'\0'*4+data['start_payload'][23:]
    elif fault.startswith('status_'):
        row=list(struct.unpack('<BQQHI',data['status_payload']));at={'status_gen':4,'status_start':1,'status_trigger':2,'status_fn':3,'status_state':0}[fault]
        row[at]+=1;data['status_payload']=struct.pack('<BQQHI',*row)
    elif fault=='blocking':data['start_payload']=struct.pack('<I',1001)+data['start_payload'][4:]
    elif fault=='short_start':data['start_payload']=data['start_payload'][:16]
    elif fault=='short_status':data['status_payload']=data['status_payload'][:22]
    elif fault=='bad_rate':data['configure_payloads'][3]=('oep.fixture.logic',b'\x50\x08\0'+b'\0'*8+b'\x52\x04\0'+struct.pack('<I',32))
    elif fault=='bad_samples':data['configure_payloads'][3]=('oep.fixture.logic',b'\x50\x08\0'+struct.pack('<II',333,1)+b'\x52\x04\0'+b'\0'*4)
    elif fault=='unknown_fn':data['members']=(2,5)
    else:data['continuous_history']=False
    assert checker().run(**data)['status']=='failed'


def test_arithmetic_product_exceeds_u64_without_truncation():
    data=fixtures()[-2]
    assert data['pretrigger']*0xfffffffe*0xfffffffd>0xffffffffffffffff
    result=checker().run(**data)
    assert result['status']=='passed'
    wanted=result['observed']['group_geometry']['3']['pretrigger']
    assert wanted==2147483648
    numerator=(data['pretrigger']*0xfffffffe*0xfffffffd)&0xffffffffffffffff
    denominator=0xfffffffb*0xffffffff
    truncated=(numerator+denominator-1)//denominator
    assert truncated<wanted
    from fractions import Fraction
    start=int(Fraction((10-truncated)*1000000000)/Fraction(0xfffffffe,0xfffffffb))
    record(data,3,index=truncated,samples=0xffffffff,start=start,flags=0)
    assert checker().run(**data)['checks'][-1]['status']=='failed'


def test_uncertainty_is_not_used_as_unbounded_geometry_tolerance():
    data=geometry();record(data,3,start=1,uncertainty=0xffffffff)
    assert checker().run(**data)['checks'][-1]['status']=='failed'


@pytest.mark.parametrize('analog_source',[False,True])
@pytest.mark.parametrize('rates',[(1000,1500),(1500,1000),(1001,1333)])
@pytest.mark.parametrize('pre',[0,2,7])
@pytest.mark.parametrize('forced',[False,True])
def test_geometry_from_real_virtual_endpoint_wire(analog_source,rates,pre,forced):
    from oep_client import capture as c,core
    from oep_client.conformance import tlvs
    from fractions import Fraction
    from test_virtual_bench_capture import bench
    ep,hst,logic,clock=bench();adc=c.AnalogCapture(hst);group=c.CaptureGroup(hst)
    source=adc if analog_source else logic;other=logic if analog_source else adc
    core.plan_apply(hst,[(logic.fn,0,20),(logic.fn,1,21),(adc.fn,1,17),(adc.fn,3,19)])
    wire={};send=hst.send
    def observe(req):
        reply=send(req)
        if reply[3:5]==b'\1\0':wire[(int.from_bytes(req[3:5],'little'),req[5])]=reply[5:]
        return reply
    hst.send=observe
    for track,rate in zip([source,other],rates):
        track.configure(rate=rate,samples=128,trigger=((c.CROSS_UP,3,1500) if analog_source else (c.EDGE,0,0)) if track is source else None,
                        pretrigger=pre if track is source else None)
    payloads={track.fn:('oep.fixture.analog' if track is adc else 'oep.fixture.logic',wire[(track.fn,track.CONFIGURE)]) for track in [source,other]}
    source_body=dict(tlvs(payloads[source.fn][1]));actual=Fraction(*struct.unpack('<II',source_body[80]))
    skew=0
    if analog_source:
        skew=dict(struct.unpack('<BI',value) for tag,value in tlvs(payloads[source.fn][1]) if tag==87)[3]
    ep.captures[source.fn].value=lambda role,index:0 if forced or index<10 else 3000 if analog_source else 1
    group.bind([source,other],trigger=source);group.start([source,other]);start=wire[(group.fn,group.START)]
    if forced:clock.t=2;group.force();trigger_ns=2000000
    else:trigger_ns=int(Fraction(10*1000000000)/actual)+(5000 if analog_source else 0)+skew
    clock.t=650;assert group.status().state==c.STATE['done'];status=wire[(group.fn,group.STATUS)]
    records={}
    for track in [source,other]:
        track.segments();body=wire[(track.fn,track.SEGMENTS)]
        assert body[:2]==b'\0\1';records[track.fn]=body[2:39]
    result=checker().run(configure_payloads=payloads,start_payload=start,status_payload=status,first_segments=records,
                         members=[source.fn,other.fn],source_fn=source.fn,pretrigger=pre,
                         sample_origins={source.fn:5000 if analog_source else 0,other.fn:0 if analog_source else 5000},
                         expected_trigger_ns=trigger_ns,forced=forced,continuous_history=True)
    assert result['status']=='passed',result['checks']
