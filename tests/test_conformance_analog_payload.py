import copy
import struct
import pytest
from oep_client import capture, registry_legacy as registry, message
from oep_client.conformance_analog_payload import AnalogPayloadChecks
from oep_client.conformance import Violation
from test_conformance_registered_ops import setup as registered_setup

T=registry.FIXTURE_ANALOG.tlv['configure_answer']
C=registry.FIXTURE_ANALOG.tlv['calibration_answer']


def checker():
    _,checks=registered_setup()
    for spec in checks.reg['interface']:
        if spec['name']=='oep.fixture.analog':
            spec['tlv']=copy.deepcopy(registry.FIXTURE_ANALOG.tlv)
            spec['enum']=copy.deepcopy(registry.FIXTURE_ANALOG.enum)
    return AnalogPayloadChecks(checks)


def payload(slot=16,offset=2,bits=12,order=(1,0),mode=1):
    rows=[(T['actual_rate'],struct.pack('<II',1000,3)),(T['layout'],bytes((slot,offset,bits,2,*order))),
          (T['blocking_ms'],struct.pack('<I',5)),(T['reference'],struct.pack('<BIB',2,3300,1))]
    if mode in (1,2):rows.append((T['actual_samples'],struct.pack('<I',3)))
    if mode==2:rows.append((T['actual_segments'],struct.pack('<I',2)))
    for role,zero,scale in ((0,-17,1000000),(1,30000,-2000000)):
        rows.extend([(T['scale'],struct.pack('<Bii',role,zero,scale)),(T['frontend_used'],bytes((role,1)))])
    rows.append((T['skew'],struct.pack('<BI',1,7)))
    return b''.join(message.tlv(tag,value) for tag,value in rows)


def factory(fe=0,scheme=b'org.example.adc',raw=b'\x00\xff\x12'):
    return message.tlv(C['factory'],bytes((fe,len(scheme)))+scheme+struct.pack('<H',len(raw))+raw)


def encode(samples,slot,offset,bits,order,dirty=True):
    mask=((1<<bits)-1)<<offset;unused=((1<<slot)-1)^mask
    return b''.join(((row[channel]<<offset)|(unused if dirty else 0)).to_bytes(slot//8,'little') for row in samples for channel in order)


@pytest.mark.parametrize('slot,offset,bits',[(8,0,1),(8,1,7),(16,3,12),(32,1,31)])
@pytest.mark.parametrize('order',[(0,1),(1,0)])
@pytest.mark.parametrize('dirty',[False,True])
def test_analog_slots_orders_unused_bits_and_raw_values(slot,offset,bits,order,dirty):
    maximum=(1<<bits)-1;samples=[[0,maximum],[maximum,0],[maximum//2,maximum//3]]
    p=payload(slot,offset,bits,order);data=encode(samples,slot,offset,bits,order,dirty)
    checks=checker();result=checks.run(configure_payload=p,calibration_payload=factory(),data=data,expected_samples=samples,roles=(0,1),frontends={0,1},mode=1,max_op_ms=10)
    assert result['status']=='passed',result['checks'];assert len(result['checks'])==3 and not result['full_conformance']
    assert all(row['level']=='oep-interface' for row in result['checks'])
    host=capture.AnalogCapture.__new__(capture.AnalogCapture);host.config=capture._config(p,True)
    assert [host.values(data,k,3) for k in (0,1)]==[[row[k] for row in samples] for k in (0,1)]
    if bits>1:assert host.millivolts(0,1)==18 and host.millivolts(1,1)==59998
    else:assert host.millivolts(0,1) is None
    assert host.clipped(1,0)==host.CLIP_HIGH and host.clipped(1,maximum)==host.CLIP_LOW


@pytest.mark.parametrize('name,value',[
 ('actual_rate',b'\0'*8),('actual_rate',b'x'),('layout',bytes((24,0,12,2,0,1))),
 ('layout',bytes((16,0,0,2,0,1))),('layout',bytes((32,0,32,2,0,1))),('layout',bytes((16,8,12,2,0,1))),
 ('layout',bytes((16,0,12,2,0,0))),('layout',bytes((16,0,12,2,0,2))),('layout',bytes((16,0,12,3,0,1))),
 ('layout',bytes((16,0,12,2,0))),('actual_samples',b'\0'*4),('actual_samples',b'x'),
 ('blocking_ms',struct.pack('<I',11)),('reference',struct.pack('<BIB',3,3300,0)),
 ('reference',struct.pack('<BIB',0,3300,2)),('scale',b'x'),('skew',b'x'),('frontend_used',b'\0\x09'),
])
def test_invalid_configure_values(name,value):
    from oep_client.conformance import tlvs
    rows=tlvs(payload());rows=[(tag,value if tag==T[name] else old) for tag,old in rows]
    with pytest.raises(Violation):checker().configure(b''.join(message.tlv(*row) for row in rows),roles=(0,1),frontends={0,1},mode=1,max_op_ms=10)


@pytest.mark.parametrize('name',['actual_rate','layout','actual_samples','blocking_ms','scale','frontend_used','reference'])
def test_required_configure_information_cannot_be_missing(name):
    from oep_client.conformance import tlvs
    p=b''.join(message.tlv(tag,value) for tag,value in tlvs(payload()) if tag!=T[name])
    with pytest.raises(Violation):checker().configure(p,roles=(0,1),frontends={0,1},mode=1,max_op_ms=10)



@pytest.mark.parametrize('name,width',[('scale',9),('skew',5),('frontend_used',2)])
@pytest.mark.parametrize('role',[1,2])
def test_channel_rows_cannot_duplicate_or_name_unplanned_role(name,width,role):
    p=payload()+message.tlv(T[name],bytes((role,))+b'\0'*(width-1))
    with pytest.raises(Violation):checker().configure(p,roles=(0,1),frontends={0,1},mode=1,max_op_ms=10)


@pytest.mark.parametrize('mode',[1,2,3])
def test_mode_conditioned_capacity_and_optional_zero_skew(mode):
    checked=checker();cfg=checked.configure(payload(mode=mode),roles=(0,1),frontends={0,1},mode=mode,max_op_ms=10)
    assert ('actual_samples' in cfg)==(mode in (1,2))
    assert ('actual_segments' in cfg)==(mode==2)
    assert cfg['skew']=={1:(7,)}
    from oep_client.conformance import tlvs
    p=b''.join(message.tlv(tag,v) for tag,v in tlvs(payload(mode=mode)) if tag!=T['skew'])
    assert checked.configure(p,roles=(0,1),frontends={0,1},mode=mode,max_op_ms=10)['skew']=={}


@pytest.mark.parametrize('mode,name',[(1,'actual_segments'),(2,'actual_segments'),(3,'actual_samples'),(3,'actual_segments')])
def test_forbidden_or_zero_mode_capacity(mode,name):
    from oep_client.conformance import tlvs
    p=b''.join(message.tlv(tag,value) for tag,value in tlvs(payload(mode=mode)) if tag!=T[name])+message.tlv(T[name],b'\0'*4)
    with pytest.raises(Violation):checker().configure(p,roles=(0,1),frontends={0,1},mode=mode,max_op_ms=10)


def test_unknown_response_tags_and_first_scalar_duplicate_are_ignored():
    p=payload()+message.tlv(126,b'x')+message.tlv(T['actual_rate'],b'bad duplicate')
    assert checker().configure(p,roles=(0,1),frontends={0,1},mode=1,max_op_ms=10)['rate'].numerator==1000


@pytest.mark.parametrize('body',[b'',factory(255,raw=b''),factory()+factory(1),message.tlv(C['vrefint'],b'\0'*16),message.tlv(126,b'unknown')])
def test_calibration_empty_generic_factory_and_vref_are_allowed(body):
    checker().calibration(body,frontends={0,1})


@pytest.mark.parametrize('body',[
 factory()+factory(),factory(7),factory(scheme=b''),factory(scheme=b'X'),factory(scheme=b'x'*65),
 message.tlv(C['factory'],b'\0'),message.tlv(C['factory'],b'\0\x05ab'),
 factory()[:-1],message.tlv(C['vrefint'],b'x'),
 message.tlv(C['factory'],b'\0\x01x\x02\0a'),
])
def test_calibration_malformed_or_wrong_frontend_is_detected(body):
    with pytest.raises(Violation):checker().calibration(body,frontends={0,1})


def test_repeated_vrefint_uses_first_value():
    checker().calibration(message.tlv(C['vrefint'],b'\0'*16)+message.tlv(C['vrefint'],b'ignored'),frontends={0,1})


@pytest.mark.parametrize('fault',['channel_order','value','short','extra','calibrated'])
def test_raw_data_faults_are_detected(fault):
    checked=checker();cfg=checked.configure(payload(),roles=(0,1),frontends={0,1},mode=1,max_op_ms=10)
    samples=[[3,7],[100,200],[1111,3333]]
    data=encode(samples,16,2,12,(0,1) if fault=='channel_order' else (1,0))
    if fault=='value':data=bytes((data[0]^4,))+data[1:]
    elif fault=='short':data=data[:-1]
    elif fault=='extra':data+=b'x'
    elif fault=='calibrated':data=encode([[v+1 for v in row] for row in samples],16,2,12,(1,0))
    with pytest.raises(Violation):checked.data(data,cfg,samples)


def test_existing_virtual_bench_success_bodies_and_raw_capture():
    from test_virtual_bench_capture import bench
    from oep_client import core, virtual_bench_capture
    ep,hst,_,clock=bench();an=capture.AnalogCapture(hst)
    core.plan_apply(hst,[(an.fn,0,16),(an.fn,1,17)])
    checked=checker();observed=[];original=hst.request
    def observe(*args,**kwargs):
        reply=original(*args,**kwargs)
        if args[:2]==(an.fn,an.CONFIGURE):observed.append(reply.payload)
        return reply
    hst.request=observe;an.configure(rate=10000,samples=3);an.start();clock.t=1;segment,=an.wait();raw=an.read_segment(segment)
    calibration=hst.request(an.fn,an.CALIBRATION).payload
    expected=[[virtual_bench_capture.analog_value(k,i) for k in range(2)] for i in range(3)]
    result=checked.run(configure_payload=observed[0],calibration_payload=calibration,data=raw,expected_samples=expected,roles=(0,1),frontends=set(ep.captures[an.fn].frontends),mode=1,max_op_ms=1000)
    assert result['status']=='passed',[(row['id'],row.get('error')) for row in result['checks']]



def sparse_payload(roles,order):
    from oep_client.conformance import tlvs
    result=[]
    for tag,value in tlvs(payload(order=(0,1))):
        if tag==T['layout']:value=value[:4]+bytes(order)
        elif tag in (T['scale'],T['skew'],T['frontend_used']):value=bytes((roles[value[0]],))+value[1:]
        result.append(message.tlv(tag,value))
    return b''.join(result)


@pytest.mark.parametrize('roles',[(1,3),(0,63),(62,63)])
@pytest.mark.parametrize('reversed_order',[False,True])
@pytest.mark.parametrize('dirty',[False,True])
def test_sparse_roles_use_role_identity_for_layout_and_raw_samples(roles,reversed_order,dirty):
    order=roles[::-1] if reversed_order else roles
    p=sparse_payload(roles,order);samples=[[0,4095],[29,513],[2048,0]]
    ordinal=[roles.index(role) for role in order];data=encode(samples,16,2,12,ordinal,dirty)
    checked=checker();result=checked.run(configure_payload=p,calibration_payload=factory(),data=data,expected_samples=samples,roles=roles[::-1],frontends={0,1},mode=1,max_op_ms=10)
    assert result['status']=='passed',result['checks']
    assert checked.config['order']==list(order) and checked.config['roles']==list(roles)
    host=capture.AnalogCapture.__new__(capture.AnalogCapture);host.config=capture._config(p,True)
    for i,role in enumerate(roles):assert host.values(data,role)==[sample[i] for sample in samples]
    assert host.millivolts(roles[0],1)==18 and host.millivolts(roles[1],1)==59998


@pytest.mark.parametrize('order',[(0,1),(1,1),(1,2),(1,63)])
def test_sparse_role_layout_rejects_ordinal_unplanned_or_duplicate_numbers(order):
    with pytest.raises(Violation):checker().configure(sparse_payload((1,3),order),roles=(1,3),frontends={0,1},mode=1,max_op_ms=10)


@pytest.mark.parametrize('roles',[(0,64),(-1,3),(1,1),(1,True)])
def test_invalid_analog_planned_role_fixture_is_rejected(roles):
    with pytest.raises(Violation):checker().configure(payload(),roles=roles,frontends={0,1},mode=1,max_op_ms=10)
