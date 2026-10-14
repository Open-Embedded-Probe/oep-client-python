from oep_client.conformance import Checks
from oep_client.conformance_wire_options import WireOptionsChecks
from oep_client.conformance_wire_options_sample import SampleWireOptionsAdapter
from oep_client.virtual_wire_options_model import WireOptionsModel
from test_conformance_attach import setup as attach_setup


def setup():
    _,c,_=attach_setup()
    for s in c.reg['interface'][:3]:s['tlv']['attach']['reset']=5
    c.reg['interface'][0]['tlv']['attach']['idle_clock']=4;c.reg['interface'][2]['tlv']['attach']['targetsel']=2
    c.reg['status']=dict(ok=0,wait=1,line=2,fault=3,timeout=4,state=5)
    for spec in c.reg['interface'][:2]:spec.setdefault('enum',{})['attach_flags']=dict(havereset_acked=1,halted=8)
    c.reg['interface'][2].setdefault('enum',{})['attach_flags']=dict(dormant_woken=4)
    model=WireOptionsModel(lambda:0,boot_id=17)
    return model,Checks(model.handle,c.reg,'virtual-resource-1'),SampleWireOptionsAdapter(model)


def test_wire_options_contracts_pass():
    _,c,a=setup();r=WireOptionsChecks(c,a).run()
    assert r['status']=='passed',[(row['id'],row.get('error')) for row in r['checks'] if row['status']!='passed']
    assert len(r['checks'])==40 and not r['full_conformance']


import pytest
import struct
from oep_client.virtual_core import Completed, Reject


@pytest.mark.parametrize('fault,case',[
    ('clock_high','IDLE-1'),('data_low','IDLE-None'),('reset_push_pull','RESET-1'),
    ('reset_short','RESET-1'),('reset_leaked','RESET-2'),('reset_default','IDLE-None'),
    ('read_before_release','RESET-1'),('read_fast','RESET-1'),('no_retry','RESET-RECOVER'),
    ('wrong_flags','RESET-HALT-1'),('bad_dpc','RESET-HALT-2'),('targetsel_lost','TARGETSEL-16777217'),
    ('listing_scheme','TARGETSEL-None'),('failure_allocated','RESET-FAILED'),
    ('failure_stream_lost','RESET-FAILED'),('failure_pins','RESET-FAILED'),
    ('close_left_drive','DETACH-IDLE'),('close_resumed','DETACH-HALTED'),
    ('reset_ignored','NO-RESET'),('reset_channel_ignored','CHANNEL'),
    ('idle_ignored','IDLE'),('reject_effect','HOLD'),
])
def test_wire_options_checker_detects_independent_faults(fault,case):
    model,c,a=setup();install=model.install
    def faulty_install():
        install();e=model.extension;dispatch=e.dispatch_request;close=e.close_connection;page=e.connection_page
        def broken_close(rid):
            pins=e.connections[rid]['pins'];old={p:dict(e.pin_levels[p]) for p in pins if p!=65535};close(rid)
            if fault=='close_left_drive':e.pin_levels.update(old)
            elif fault=='close_resumed':
                for t in e.targets.values():t['halted']=False
        def broken_page(fn,first):
            data=page(fn,first)
            if fn==3 and data[1] and fault=='listing_scheme':data=data[:14]+b'\0'+data[15:]
            return data
        def broken_dispatch(fn,op,payload,tags):
            if fn>3 or op!=17:return dispatch(fn,op,payload,tags)
            if fault=='reset_ignored':e.reset_supported=True
            if fault=='reset_channel_ignored' and 5 in tags:
                raw,value=tags[5];tags=dict(tags);tags[5]=(raw,b'\x06\0'+value[2:])
            if fault=='idle_ignored' and 4 in tags:tags=dict(tags);tags[4]=(tags[4][0],b'\0')
            try:value=dispatch(fn,op,payload,tags)
            except Reject:
                if fault=='reject_effect':e.wire_events.append(('unexpected-pin-touch',))
                raise
            data=bytes(value);outcome=getattr(value,'outcome',0)
            if len(data)>=13 and not outcome:
                rid=struct.unpack_from('<I',data)[0];pins=e.connections[rid]['pins']
                if fault=='clock_high' and fn==1:e.pin_levels[pins[1]]['level']=1
                elif fault=='data_low' and fn==1:e.pin_levels[pins[0]]['level']=0
                elif fault=='wrong_flags':data=data[:8]+bytes((data[8]&~8,))+data[9:]
                elif fault=='bad_dpc' and data[8]&8:data=data[:16]+struct.pack('<I',0xdeadbeef)+data[20:]
                elif fault=='targetsel_lost' and fn==3:e.connections[rid]['targetsel']=None
            if fault=='reset_push_pull':e.wire_events[:]=[(r[0],r[1],'push-pull-low',r[3]) if r[0]=='reset-assert' else r for r in e.wire_events]
            elif fault=='reset_short':e.wire_events[:]=[r[:3]+(r[3]-1,) if r[0]=='reset-release' else r for r in e.wire_events]
            elif fault=='reset_leaked' and 5 in tags:e.pin_levels[6]=dict(mode='open-drain',level=0)
            elif fault=='reset_default' and 5 not in tags:e.wire_events.append(('reset-assert',6,'open-drain-low',e.clock_ms))
            elif fault=='read_before_release':e.wire_events[:]=[r[:3]+(-1,)+r[4:] if r[0]=='reset-read' else r for r in e.wire_events]
            elif fault=='read_fast':e.wire_events[:]=[r[:4]+(2000,) if r[0]=='reset-read' else r for r in e.wire_events]
            elif fault=='no_retry':e.wire_events[:]=[r for r in e.wire_events if r[0]!='reset-read' or r[2]!=2]
            elif fault=='failure_allocated' and outcome:e.allocate()
            elif fault=='failure_stream_lost' and outcome:
                for rid in list(e.streams):e.close_stream(rid)
            elif fault=='failure_pins' and outcome:e.pin_levels[0]=dict(mode='wire',level=1)
            return Completed(data,outcome)
        e.dispatch_request=broken_dispatch;e.close_connection=broken_close;e.connection_page=broken_page
    model.install=faulty_install;r=WireOptionsChecks(c,a).run()
    assert next(row for row in r['checks'] if row['id']=='OEP-WIRE-OPTIONS-'+case)['status']=='failed',r['checks']


@pytest.mark.parametrize('invalid',['scope','idle','reset','targetsel','role','budget','status','flags'])
def test_wire_options_preflight_blocks(invalid):
    model,c,a=setup()
    if invalid=='scope':a.reset_scope='physical'
    elif invalid=='idle':c.reg['interface'][0]['tlv']['attach']['idle_clock']=6
    elif invalid=='reset':c.reg['interface'][1]['tlv']['attach']['reset']=6
    elif invalid=='targetsel':c.reg['interface'][2]['tlv']['attach']['targetsel']=6
    elif invalid=='status':c.reg['status']['line']=3
    elif invalid=='flags':c.reg['interface'][0]['enum']['attach_flags']['halted']=4
    elif invalid=='budget':model.ep.max_op_ms=6
    else:
        describe=model.extension.describe;model.extension.describe=lambda fn:tuple(r for r in describe(fn) if r[0]!=1)
    r=WireOptionsChecks(c,a).run();assert r['checks'][0]['status']=='failed'
    assert all(row['status']=='blocked' for row in r['checks'][1:])


@pytest.mark.parametrize('method,args',[
    ('wire_behavior',('unknown',)),('wire_reset_support',(1,)),
    ('wire_pin',(True,'free')),('wire_pin',(8,'free')),('wire_pin',(6,'unknown')),
])
def test_invalid_wire_controls_preserve_state(method,args):
    model,_,_=setup();before=model.wire_options_state()
    with pytest.raises(ValueError):getattr(model,method)(*args)
    assert model.wire_options_state()==before


def test_wire_fixture_common_declarations_pass():
    from oep_client.conformance_interfaces import InterfaceChecks
    _,c,_=setup();c.reg['describe_common'].update(role_channels=1,max_clock_hz=2,max_length=3,min_clock_hz=4,features=5,channel_group=6)
    r=InterfaceChecks(c).run();assert r['status']=='passed',r['checks']
    assert len(r['checks'])==33


def test_wire_pin_control_cannot_change_owned_channel():
    from oep_client.virtual_core import tlv
    model,c,_=setup();c.confirm()
    with c.holding(3000) as sid:
        c.success(c.request(17,struct.pack('<BHHI',0,0,1,2000),session=sid,fn=1));before=model.wire_options_state()
        with pytest.raises(ValueError):model.wire_pin(0,'output')
        assert model.wire_options_state()==before
