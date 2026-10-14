import pytest
from oep_client.conformance import Checks
from oep_client.conformance_spi import SpiChecks
from oep_client.conformance_spi_sample import SampleSpiAdapter
from oep_client.virtual_spi_model import SpiModel
from test_conformance_registered_ops import setup as registered_setup


def setup(**kwargs):
    _,c=registered_setup();reg=c.reg;reg['constants']={'min_max_frame':64}
    for spec in reg['interface']:
        if spec['name']=='oep.probe.plan':spec['tlv']={'plan_apply':{'role_assignment':16},'describe':{'plan_roles':64}}
        if spec['name']=='oep.fixture.spi-target':
            spec['tlv']={'describe':{'queue_depth':64,'cs_setup_ns':67},'read_rx_answer':{'ns':1}}
            spec['enum']={'role':{'sck':1,'mosi':2,'miso':3,'cs':4},'state':{'unconfigured':0,'running':1},'features':{'lsb_first':1}}
    model=SpiModel(boot_id=17,**kwargs);return model,Checks(model.handle,reg,'virtual-resource-1')


@pytest.mark.parametrize('lsb',[True,False])
def test_spi_contract(lsb):
    model,c=setup(lsb=lsb);r=SpiChecks(c,SampleSpiAdapter(model)).run()
    assert r['status']=='passed',[(row['id'],row.get('error')) for row in r['checks'] if row['status']!='passed']
    assert len(r['checks'])==42 and not r['full_conformance']


def test_common_declarations():
    from oep_client.conformance_interfaces import InterfaceChecks
    _,c=setup();r=InterfaceChecks(c).run();assert r['status']=='passed',r['checks']


def test_metadata():
    from oep_client.conformance_fixture_metadata import FixtureMetadataChecks
    _,c=setup();r=FixtureMetadataChecks(c,{2:'oep.fixture.spi-target'}).run();assert r['status']=='passed',r['checks']


@pytest.mark.parametrize('kind',SpiChecks.cases)
def test_wire_faults_detected(kind):
    model,c=setup();original=model.handle
    def broken(req):
        reply=original(req)
        if int.from_bytes(req[3:5],'little'):
            if reply[3]==1:reply=reply[:4]+bytes((0 if reply[4] else 1,))+reply[5:]
            else:reply=reply[:4]+b'\3'
        return reply
    c.send=broken;checker=SpiChecks(c,SampleSpiAdapter(model));checker.cases=(kind,);r=checker.run()
    assert next(row for row in r['checks'] if row['id']=='OEP-SPI-'+kind)['status']=='failed'


@pytest.mark.parametrize('fault,case',[
 ('rx_lifo','FIFO'),('keep_arm','ZERO-BITS'),('discard_arm','ZERO-BITS'),
 ('no_unarmed_error','UNARMED'),('double_error','DOUBLE-ERROR'),('truncate_bits','TRUNCATE'),
 ('dirty_partial','PARTIAL'),('wrong_order','MODES'),('wrong_phase','MODES'),('wrong_drive','MODES'),
 ('keep_config','RECONFIGURE'),('inactive_drive','MISO-GATE'),('wrong_timestamp','TIMESTAMP'),
 ('not_ready_arms','ARM-READY'),('input_output','MISO-GATE'),('lost_plan_reset','RELEASE'),
])
def test_side_effect_faults_detected(fault,case):
    model,c=setup();install=model.install
    def faulty_install():
        install();e=model.extension;dispatch=e.dispatch_request;cs=e.cs;clock=e.clock;release=e.release_plans
        def broken_dispatch(fn,op,payload,tags):
            import copy
            old=copy.deepcopy((e.rx,e.armed,e.transactions,e.errors))
            if fault=='rx_lifo' and fn==2 and op==18:e.rx.reverse()
            result=dispatch(fn,op,payload,tags)
            if fault=='keep_config' and fn==2 and op==16:e.rx,e.armed,e.transactions,e.errors=old
            if fault=='not_ready_arms' and fn==2 and op==17 and not e.ready:e.armed=(1,b'x')
            if fault=='inactive_drive' and fn==2 and op==17:e.pins[2]=(4,0)
            return result
        def broken_cs(active):
            arm=e.armed;count=len(e.bits);result=cs(active)
            if not active:
                if fault=='discard_arm' and not count:e.armed=None
                if fault=='keep_arm' and count:e.armed=arm
                if fault=='no_unarmed_error':e.errors=0
                if fault=='double_error' and count>8:e.errors+=1
                if e.rx:
                    bits,data,ns=e.rx[-1]
                    if fault=='truncate_bits':bits=len(data)*8
                    if fault=='dirty_partial' and bits%8:data=data[:-1]+bytes((data[-1]|255,))
                    if fault=='wrong_timestamp':ns+=1
                    e.rx[-1]=(bits,data,ns)
            if fault=='input_output' and active:e.pins[0]=(4,0)
            return result
        def broken_clock(level,mosi):
            old_order=e.bit_order;old_mode=e.mode
            if fault=='wrong_order':e.bit_order=1-e.bit_order
            if fault=='wrong_phase':e.mode^=1
            result=clock(level,mosi);e.bit_order=old_order;e.mode=old_mode;return result
        def broken_release(functions):
            release(functions)
            if fault=='lost_plan_reset':e.configured=True;e.transactions=99
        e.dispatch_request=broken_dispatch;e.cs=broken_cs;e.clock=broken_clock;e.release_plans=broken_release
        if fault=='wrong_drive':
            drive=e.drive;e.drive=lambda value:drive(1-value)
    model.install=faulty_install;checker=SpiChecks(c,SampleSpiAdapter(model));checker.cases=(case,);r=checker.run()
    assert next(row for row in r['checks'] if row['id']=='OEP-SPI-'+case)['status']=='failed'


@pytest.mark.parametrize('case',['REPLAY-RX','REPLAY-ARM','REPLAY-CONFIG'])
def test_cached_reply_cannot_repeat_side_effects(case):
    model,c=setup();original=model.handle;seen=set();operation={'REPLAY-RX':18,'REPLAY-ARM':17,'REPLAY-CONFIG':16}[case]
    def broken(req):
        reply=original(req)
        if int.from_bytes(req[3:5],'little')==2 and req[5]==operation:
            if req in seen:
                e=model.extension
                if case=='REPLAY-RX' and e.rx:e.rx.pop(0)
                elif case=='REPLAY-ARM':e.armed=(1,b'a')
                elif case=='REPLAY-CONFIG':e.armed=None;e.mode=0
            seen.add(req)
        return reply
    c.send=broken;checker=SpiChecks(c,SampleSpiAdapter(model));checker.cases=(case,);r=checker.run()
    assert next(row for row in r['checks'] if row['id']=='OEP-SPI-'+case)['status']=='failed'


@pytest.mark.parametrize('method,args',[
 ('spi_cs',(1,)),('spi_cs',(False,)),('spi_clock',(0,0)),('spi_ready',(1,)),
 ('spi_transfer',([True],)),('spi_transfer',([2],)),('spi_advance',(-1,)),('spi_advance',(True,)),
])
def test_invalid_stimuli_do_not_change_fixture(method,args):
    model,_=setup();before=model.spi_state()
    with pytest.raises(ValueError):getattr(model,method)(*args)
    assert model.spi_state()==before


@pytest.mark.parametrize('field',['timestamp','setup'])
def test_optional_metadata_can_be_absent(field):
    model,c=setup();original=model.handle
    def without_optional(req):
        reply=original(req)
        if reply[3:5]!=b'\1\0':return reply
        if field=='timestamp' and int.from_bytes(req[3:5],'little')==2 and req[5]==18:
            count=int.from_bytes(reply[10:12],'little');reply=reply[:12+count]
        return reply
    c.send=without_optional
    if field=='setup':
        install=model.install
        def no_setup_install():
            install();original_describe=model.extension.describe
            def describe(fn):return tuple(row for row in original_describe(fn) if fn!=2 or row[0]!=67)
            model.extension.describe=describe
        model.install=no_setup_install;model.install()
    r=SpiChecks(c,SampleSpiAdapter(model)).run();assert r['status']=='passed',r['checks']


@pytest.mark.parametrize('invalid',['scope','selection','geometry'])
def test_preflight_blocks_invalid_fixture(invalid):
    model,c=setup();a=SampleSpiAdapter(model)
    if invalid=='scope':a.reset_scope='physical'
    elif invalid=='selection':a.selection={2:'oep.fixture.spi-target'}
    else:
        original=model.extension.describe
        def describe(fn):return tuple(bytes((64,1,0,4)) if fn==2 and row[0]==64 else row for row in original(fn))
        model.extension.describe=describe
    r=SpiChecks(c,a).run();assert next(row for row in r['checks'] if row['id']=='OEP-SPI-CONTEXT')['status']=='failed'
    assert all(row['status']=='blocked' for row in r['checks'] if row['id'].startswith('OEP-SPI-') and row['id']!='OEP-SPI-CONTEXT')
    assert not a.trace or all(row['method']=='spi_state' for row in a.trace)


@pytest.mark.parametrize('case',['ACTIVE-RELEASE','ACTIVE-END','ACTIVE-EXPIRY','ACTIVE-FORCE'])
def test_active_cs_release_must_restore_miso(case):
    model,c=setup();install=model.install
    def faulty_install():
        install();e=model.extension;release=e.release_plans
        def broken(functions):
            active=e.active;pin=e.pins[2];release(functions)
            if active:e.active=True;e.pins[2]=pin
        e.release_plans=broken
    model.install=faulty_install;checker=SpiChecks(c,SampleSpiAdapter(model));checker.cases=(case,);r=checker.run()
    assert next(row for row in r['checks'] if row['id']=='OEP-SPI-'+case)['status']=='failed'


def test_empty_rx_bits_field_is_not_specified():
    model,c=setup();original=model.handle
    def unused_bits(req):
        reply=original(req)
        if int.from_bytes(req[3:5],'little')==2 and req[5]==18 and reply[3:5]==b'\1\0' and reply[10:12]==b'\0\0':reply=reply[:6]+b'\xef\xbe\xad\xde'+reply[10:]
        return reply
    c.send=unused_bits;r=SpiChecks(c,SampleSpiAdapter(model)).run();assert r['status']=='passed',r['checks']


@pytest.mark.parametrize('fault',['failed','missing_cause','wrong_cause','arms'])
@pytest.mark.parametrize('planned',[False,True])
def test_state0_arm_refusal_faults(fault,planned):
    from oep_client.virtual_core import Reject, Completed
    model,c=setup();install=model.install
    def faulty_install():
        install();e=model.extension;dispatch=e.dispatch_request
        def broken(fn,op,payload,tags):
            if fn==2 and op==17 and not e.configured and (2 in e.plans)==planned:
                if fault=='failed':return Completed(b'',1)
                if fault=='missing_cause':raise Reject(4)
                if fault=='wrong_cause':raise Reject(4,b'\x01\x01\x00\x05')
                e.armed=(1,b'x')
            return dispatch(fn,op,payload,tags)
        e.dispatch_request=broken
    model.install=faulty_install
    checker=SpiChecks(c,SampleSpiAdapter(model));checker.cases=('ARM-STATE0',)
    r=checker.run()
    assert next(row for row in r['checks'] if row['id']=='OEP-SPI-ARM-STATE0')['status']=='failed'
