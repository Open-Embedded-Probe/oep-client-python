import pytest
from oep_client.conformance import Checks
from oep_client.conformance_i2c import I2cChecks
from oep_client.conformance_i2c_sample import SampleI2cAdapter
from oep_client.virtual_i2c_model import I2cModel
from test_conformance_registered_ops import setup as registered_setup


def setup(**kwargs):
    _,c=registered_setup();reg=c.reg;reg['constants']={'min_max_frame':64}
    for spec in reg['interface']:
        if spec['name']=='oep.probe.plan':spec['tlv']={'plan_apply':{'role_assignment':16},'describe':{'plan_roles':64}}
        if spec['name']=='oep.fixture.i2c-target':spec['tlv']={'describe':{'queue_depth':64,'max_stretch_us':65},'read_rx_answer':{'ns':1}};spec['enum']={'role':{'sda':1,'scl':2},'state':{'unconfigured':0,'running':1},'features':{'internal_pullups':4}}
    model=I2cModel(boot_id=17,**kwargs);return model,Checks(model.handle,reg,'virtual-resource-1')


@pytest.mark.parametrize('stretch,pullups',[(True,True),(False,True),(True,False),(False,False)])
def test_i2c_contract(stretch,pullups):
    model,c=setup(stretch=stretch,pullups=pullups);r=I2cChecks(c,SampleI2cAdapter(model)).run()
    assert r['status']=='passed',[(x['id'],x.get('error')) for x in r['checks'] if x['status']!='passed']
    assert len(r['checks'])==35 and not r['full_conformance']


def test_common_declarations():
    from oep_client.conformance_interfaces import InterfaceChecks
    _,c=setup();r=InterfaceChecks(c).run();assert r['status']=='passed',r['checks']


def test_metadata():
    from oep_client.conformance_fixture_metadata import FixtureMetadataChecks
    _,c=setup();r=FixtureMetadataChecks(c,{2:'oep.fixture.i2c-target'}).run();assert r['status']=='passed',r['checks']


@pytest.mark.parametrize('kind',I2cChecks.cases)
def test_bad_wire_results_detected(kind):
    model,c=setup();original=model.handle
    def broken(req):
        reply=original(req)
        if int.from_bytes(req[3:5],'little'):
            if reply[3]==1:reply=reply[:4]+b'\1'+reply[5:]
            else:reply=reply[:4]+b'\3'
        return reply
    c.send=broken;checker=I2cChecks(c,SampleI2cAdapter(model));checker.cases=(kind,);r=checker.run()
    assert next(row for row in r['checks'] if row['id']=='OEP-I2C-'+kind)['status']=='failed'


@pytest.mark.parametrize('fault,case',[
    ('rx_lifo','READ-FIFO'),('tx_lifo','PRELOAD'),('empty_counted','EMPTY'),
    ('no_error','TRUNCATE'),('double_error','DOUBLE-ERROR'),('drop_ack','TRUNCATE'),
    ('keep_config','RECONFIGURE'),('stretch_address','STRETCH'),('keep_plan','RELEASE'),
    ('keep_slot','READ-LENGTH'),('wrong_timestamp','WRITE'),('push_high','PULLUPS'),('wrong_address','MISMATCH'),
])
def test_side_effect_faults_detected(fault,case):
    model,c=setup();install=model.install
    def faulty_install():
        install();e=model.extension;dispatch=e.dispatch_request;write=e.write;read=e.read;release=e.release_plans
        def broken_dispatch(fn,op,payload,tags):
            import copy
            before=copy.deepcopy((e.rx,e.tx,e.rx_frames,e.errors))
            if fault=='rx_lifo' and fn==2 and op==17:e.rx.reverse()
            
            result=dispatch(fn,op,payload,tags)
            if fault=='keep_config' and fn==2 and op==16:e.rx,e.tx,e.rx_frames,e.errors=before
            return result
        def broken_write(address,data,end):
            result=write(address,data,end)
            if fault=='empty_counted' and not data:e.rx_frames+=1
            if fault=='no_error':e.errors=0
            if fault=='double_error' and len(data)>8:e.errors+=1
            if fault=='drop_ack' and len(data)>8:result['acked_bytes']=8
            if fault=='stretch_address' and e.stretch_us:result['stretches'].insert(0,e.stretch_us)
            if fault=='wrong_timestamp' and e.rx:
                data,ns=e.rx[-1];e.rx[-1]=(data,ns+1)
            return result
        def broken_read(address,count):
            before=list(e.tx)
            if fault=='tx_lifo':e.tx.reverse()
            result=read(address,count)
            if fault=='wrong_address' and address==0x43:e.tx.clear()
            if fault=='keep_slot':e.tx[:]=before
            return result
        def broken_release(functions):
            release(functions)
            if fault=='keep_plan':e.plans[2]=[0,1];e.configured=True
        e.dispatch_request=broken_dispatch;e.write=broken_write;e.read=broken_read;e.release_plans=broken_release
        if fault=='push_high':
            original=e.dispatch_request
            def high(fn,op,payload,tags):
                result=original(fn,op,payload,tags)
                if fn==2 and op==16:e.pins[0]=(4,0)
                return result
            e.dispatch_request=high
    model.install=faulty_install;checker=I2cChecks(c,SampleI2cAdapter(model));checker.cases=(case,);r=checker.run()
    assert next(row for row in r['checks'] if row['id']=='OEP-I2C-'+case)['status']=='failed'


@pytest.mark.parametrize('method,args',[
 ('i2c_write',(128,b'x','stop')),('i2c_write',(0x42,'x','stop')),('i2c_write',(0x42,b'x','unknown')),
 ('i2c_read',(0x42,0)),('i2c_read',(0x42,True)),('i2c_read',(128,1)),('i2c_advance',(-1,)),('i2c_advance',(True,)),
])
def test_controls_reject_invalid_stimuli(method,args):
    model,_=setup();before=model.i2c_state()
    with pytest.raises(ValueError):getattr(model,method)(*args)
    assert model.i2c_state()==before


@pytest.mark.parametrize('invalid',['scope','selection','geometry'])
def test_preflight_blocks_invalid_fixture(invalid):
    model,c=setup();a=SampleI2cAdapter(model)
    if invalid=='scope':a.reset_scope='physical'
    elif invalid=='selection':a.selection={2:'oep.fixture.i2c-target'}
    else:
        original=model.extension.describe
        def describe(fn):
            rows=original(fn)
            return tuple(bytes((64,1,0,4)) if fn==2 and row[0]==64 else row for row in rows)
        model.extension.describe=describe
    r=I2cChecks(c,a).run();assert next(row for row in r['checks'] if row['id']=='OEP-I2C-CONTEXT')['status']=='failed'
    assert all(row['status']=='blocked' for row in r['checks'] if row['id'].startswith('OEP-I2C-') and row['id']!='OEP-I2C-CONTEXT')
    assert not a.trace or all(row['method']=='i2c_state' for row in a.trace)


@pytest.mark.parametrize('case',['REPLAY-RX','REPLAY-TX','REPLAY-CONFIG'])
def test_cached_reply_cannot_repeat_side_effects(case):
    model,c=setup();original=model.handle;seen=set()
    operation={'REPLAY-RX':17,'REPLAY-TX':18,'REPLAY-CONFIG':16}[case]
    def broken(req):
        reply=original(req)
        if int.from_bytes(req[3:5],'little')==2 and req[5]==operation:
            if req in seen:
                e=model.extension
                if case=='REPLAY-RX' and e.rx:e.rx.pop(0)
                elif case=='REPLAY-TX':e.tx.append(req[12:])
                elif case=='REPLAY-CONFIG':e.rx.clear();e.rx_frames=0
            seen.add(req)
        return reply
    c.send=broken;checker=I2cChecks(c,SampleI2cAdapter(model));checker.cases=(case,);r=checker.run()
    assert next(row for row in r['checks'] if row['id']=='OEP-I2C-'+case)['status']=='failed'


def test_optional_timestamp_can_be_absent():
    model,c=setup();original=model.handle
    def without_timestamp(req):
        reply=original(req)
        if int.from_bytes(req[3:5],'little')==2 and req[5]==17 and reply[3:5]==b'\1\0':
            count=int.from_bytes(reply[6:8],'little');reply=reply[:8+count]
        return reply
    c.send=without_timestamp;r=I2cChecks(c,SampleI2cAdapter(model)).run()
    assert r['status']=='passed',r['checks']
