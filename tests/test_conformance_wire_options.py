from oep_client.conformance import Checks
from oep_client.conformance_wire_options import WireOptionsChecks
from oep_client.conformance_wire_options_sample import SampleWireOptionsAdapter
from oep_client.virtual_wire_options_model import WireOptionsModel, AT, ANSWER, FLAGS, WOP
from oep_client.virtual_names import DC, CAUSE
from oep_client.registry import CORE as _CORE, WIRE_RVSWD as _RVSWD

SCHEME_DMI = __import__('oep_client.registry',fromlist=['COMMON']).COMMON.enum['target_id_scheme']['dmi_7f']
from test_conformance_attach import setup as attach_setup


def setup():
    _,c,_=attach_setup()
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
            if fn>3 or op!=WOP['attach']:return dispatch(fn,op,payload,tags)
            if fault=='reset_ignored':e.reset_supported=True
            if fault=='reset_channel_ignored' and AT['reset'] in tags:
                raw,value=tags[AT['reset']];tags=dict(tags);tags[AT['reset']]=(raw,b'\x06\0'+value[2:])
            if fault=='idle_ignored' and AT['idle_clock'] in tags:tags=dict(tags);tags[AT['idle_clock']]=(tags[AT['idle_clock']][0],b'\0')
            try:value=dispatch(fn,op,payload,tags)
            except Reject:
                if fault=='reject_effect':e.wire_events.append(('unexpected-pin-touch',))
                raise
            data=bytes(value);outcome=getattr(value,'outcome',0)
            if len(data)>=13 and not outcome:
                rid=struct.unpack_from('<I',data)[0];pins=e.connections[rid]['pins']
                if fault=='clock_high' and fn==1:e.pin_levels[pins[1]]['level']=1
                elif fault=='data_low' and fn==1:e.pin_levels[pins[0]]['level']=0
                elif fault=='wrong_flags':data=data[:8]+bytes((data[8]&~FLAGS['halted'],))+data[9:]
                elif fault=='bad_dpc' and data[8]&FLAGS['halted']:data=data[:16]+struct.pack('<I',0xdeadbeef)+data[20:]
                elif fault=='targetsel_lost' and fn==3:e.connections[rid]['targetsel']=None
            if fault=='reset_push_pull':e.wire_events[:]=[(r[0],r[1],'push-pull-low',r[3]) if r[0]=='reset-assert' else r for r in e.wire_events]
            elif fault=='reset_short':e.wire_events[:]=[r[:3]+(r[3]-1,) if r[0]=='reset-release' else r for r in e.wire_events]
            elif fault=='reset_leaked' and AT['reset'] in tags:e.pin_levels[6]=dict(mode='open-drain',level=0)
            elif fault=='reset_default' and AT['reset'] not in tags:e.wire_events.append(('reset-assert',6,'open-drain-low',e.clock_ms))
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
    elif invalid=='idle':del c.reg['interface'][0]['tlv']['attach']['idle_clock']          # a SPEC without what the checker uses
    elif invalid=='reset':
        for spec in c.reg['interface'][:3]:del spec['tlv']['attach']['reset']
    elif invalid=='targetsel':del c.reg['interface'][2]['tlv']['attach']['targetsel']
    elif invalid=='status':del c.reg['status']['line']
    elif invalid=='flags':del c.reg['interface'][0]['enum']['attach_flags']['halted']
    elif invalid=='budget':model.ep.max_op_ms=6
    else:
        describe=model.extension.describe;model.extension.describe=lambda fn:tuple(r for r in describe(fn) if r[0]!=DC['role_channels'])
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
    _,c,_=setup()
    r=InterfaceChecks(c).run();assert r['status']=='passed',r['checks']
    assert len(r['checks'])==33


def test_wire_pin_control_cannot_change_owned_channel():
    from oep_client.virtual_core import tlv
    model,c,_=setup();c.confirm()
    with c.holding(3000) as sid:
        c.success(c.request(WOP['attach'],struct.pack('<BHHI',0,0,1,2000),session=sid,fn=1));before=model.wire_options_state()
        with pytest.raises(ValueError):model.wire_pin(0,'output')
        assert model.wire_options_state()==before


@pytest.mark.parametrize('variant',['omitted','unhalted'])
def test_dpc_is_an_optional_diagnostic(variant):
    """debug §3 (a09fa45): "dpc TLVは任意のRV32診断値で、bit3が立ち、読めた場合だけ付ける" - a method 1 attach without dpc
    passes; a dpc on an answer without flags bit3 (method 0) fails."""
    model,c,a=setup();install=model.install
    def variant_install():
        install();e=model.extension;dispatch=e.dispatch_request
        def changed(fn,op,payload,tags):
            value=dispatch(fn,op,payload,tags);data=bytes(value);outcome=getattr(value,'outcome',0)
            if fn<=3 and op==WOP['attach'] and len(data)>=13 and not outcome:
                if variant=='omitted':data=data[:13]+b''.join(tlv_bytes(t,v) for t,v in tlvs(data[13:]) if t!=ANSWER['dpc'])
                elif not data[8]&FLAGS['halted']:data=data+tlv_bytes(ANSWER['dpc'],struct.pack('<I',0x1000))
            return Completed(data,outcome)
        e.dispatch_request=changed
    model.install=variant_install;r=WireOptionsChecks(c,a).run()
    if variant=='omitted':assert r['status']=='passed',[(row['id'],row.get('error')) for row in r['checks'] if row['status']!='passed']
    else:assert r['status']=='failed'


def tlv_bytes(tag,value):return bytes((tag,))+struct.pack('<H',len(value))+value


from oep_client.conformance import tlvs


def test_rvswd_target_id_may_be_attached():
    """debug §1 (a09fa45): "attach の応答の後ろに、probe が読めた target の識別子を TLV 0x10 target_id で付けてよい" and the
    connections entry carries it (§2.1) - an rvswd / swio probe that attaches one (scheme 1, DMI 0x7F) passes."""
    model,c,a=setup();install=model.install
    def tid_install():
        install();e=model.extension;dispatch=e.dispatch_request;page=e.connection_page;given=set()
        def with_tid(fn,op,payload,tags):
            value=dispatch(fn,op,payload,tags);data=bytes(value);outcome=getattr(value,'outcome',0)
            if fn in (1,2) and op==WOP['attach'] and len(data)>=13 and not outcome:
                given.add(struct.unpack_from('<I',data)[0]);data=data+tlv_bytes(ANSWER['target_id'],bytes((SCHEME_DMI,))+struct.pack('<I',0x12345678))
            return Completed(data,outcome)
        def listed(fn,first):
            data=bytearray(page(fn,first));at=2
            for _ in range(data[1]):
                rid=struct.unpack_from('<I',data,at)[0]
                if rid in given and data[at+13]==0:data[at+12:at+14]=bytes((SCHEME_DMI,4));data[at+14:at+14]=struct.pack('<I',0x12345678)
                at+=14+data[at+13]
            return bytes(data)
        e.dispatch_request=with_tid;e.connection_page=listed
    model.install=tid_install;r=WireOptionsChecks(c,a).run()
    assert r['status']=='passed',[(row['id'],row.get('error')) for row in r['checks'] if row['status']!='passed']
