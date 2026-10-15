from oep_client.conformance import Checks
from oep_client.conformance_run import RunChecks
from oep_client.conformance_run_sample import SampleRunAdapter
from oep_client.virtual_run_model import RunModel
from test_conformance_reset import setup as reset_setup


def setup():
    _,c,_=reset_setup();spec=c.reg['interface'][-1];spec['op'].append({'name':'run','code':22});spec['enum']['run_stopped']=dict(timeout_halted=0,stopped=1,not_halted=2,not_run=3)
    model=RunModel(lambda:0,boot_id=17)
    return model,Checks(model.handle,c.reg,'virtual-resource-1'),SampleRunAdapter(model)


def test_optional_run_contracts_pass():
    _,c,a=setup();r=RunChecks(c,a).run()
    assert r['status']=='passed',[(row['id'],row.get('error')) for row in r['checks'] if row['status']!='passed']
    assert len(r['checks'])==29 and not r['full_conformance']


import pytest
import struct
from oep_client.virtual_core import Completed, Reject, tlv


@pytest.mark.parametrize('fault,case',[
    ('wrong_stopped','TIMEOUT-HALTED'),('wrong_timeout','NOT-HALTED'),('invalid_pc','NOT-HALTED'),
    ('invalid_values','NOT-HALTED'),('elapsed_preparation','STOPPED'),('elapsed_halt_wait','TIMEOUT-HALTED'),
    ('elapsed_prepare','PREPARE-LINE'),('reverse_outputs','STOPPED'),('deduplicate','STOPPED'),
    ('gpr_reverted','STOPPED'),('pc_reverted','SAME-PC'),('dcsr_reverted','STOPPED'),
    ('auto','STOPPED'),('data0','STOPPED'),('data1','PREPARE-FAULT'),
    ('haltreq','TIMEOUT-HALTED'),('hartsel','STOPPED'),
    ('repeat_resume','SAME-PC'),('prepare_resume','PREPARE-FAULT'),('missing_rehalt','NOT-HALTED'),
    ('prep_running','PREPARE-LINE'),('program_omitted','PROGRAM-EFFECTS'),
    ('other_connection','CONSOLE-STOPPED'),('mailbox_interleave','CONSOLE-STOPPED'),
    ('frame_over_accepted','FRAME-OVER'),('frame_unknown_tlv','FRAME-BOUND'),('reject_effect','TIMEOUT'),
    ('over_budget','TIMEOUT-HALTED'),('over_budget','NOT-HALTED'),
])
def test_run_checker_detects_independent_faults(fault,case):
    model,c,a=setup();install=model.install
    def faulty_install():
        install();e=model.extension;dispatch=e.dispatch_request;interleave=e.interleave
        def broken_interleave(parent):
            if fault=='mailbox_interleave':
                for rid,(p,_) in e.streams.items():
                    if p==parent:e.mailboxes[rid]['debug_busy']=False
            interleave(parent)
        def broken_dispatch(fn,op,payload,tags):
            if fn!=5 or op!=22:return dispatch(fn,op,payload,tags)
            parent=struct.unpack_from('<I',payload)[0];saved_gprs=dict(e.targets.get(parent,{}).get('gprs',{}));old_limit=e.ep.probe.max_frame
            if fault=='frame_over_accepted':e.ep.probe.max_frame=1000
            if fault=='program_omitted' and parent in e.programs:e.programs[parent]={'memory':{},'gprs':{}}
            try:
                try:value=dispatch(fn,op,payload,tags)
                except Reject:
                    if fault=='reject_effect':e.access.append(('invalid-loader-access',parent))
                    raise
            finally:e.ep.probe.max_frame=old_limit
            data=bytes(value);outcome=value.outcome;t=e.targets[parent];count=data[10];end=11+4*count
            if fault=='wrong_stopped':data=data[:1]+b'\x01'+data[2:]
            elif fault=='wrong_timeout' and data[0]==4:data=b'\x03'+data[1:]
            elif fault=='invalid_pc' and data[1]==2:data=data[:2]+struct.pack('<I',0x2004)+data[6:]
            elif fault=='invalid_values' and data[1]==2:data=data[:10]+b'\x01'+struct.pack('<I',1)+data[11:]
            elif fault=='elapsed_preparation':data=data[:6]+struct.pack('<I',struct.unpack_from('<I',data,6)[0]+400)+data[10:]
            elif fault=='elapsed_halt_wait' and data[1]==0:data=data[:6]+struct.pack('<I',1000)+data[10:]
            elif fault=='over_budget' and data[1] in (0,2):   # debug §4.4: the whole wait of timeout_ms, past max_op_ms
                i=max(k for k,row in enumerate(e.access) if row[0]=='run-finish');e.access[i]=e.access[i][:2]+(e.access[i][2]+600,)
                data=data[:6]+struct.pack('<I',struct.unpack_from('<I',data,6)[0]+600)+data[10:]
            elif fault=='elapsed_prepare' and data[1]==3:data=data[:6]+struct.pack('<I',400)+data[10:]
            elif fault=='reverse_outputs' and count:data=data[:11]+b''.join(data[11+4*i:15+4*i] for i in reversed(range(count)))+data[end:]
            elif fault=='deduplicate' and count==4:data=data[:10]+b'\x03'+data[11:19]+data[23:27]+data[end:]
            elif fault=='gpr_reverted':t['gprs']=saved_gprs
            elif fault=='pc_reverted':t['pc']=0x1000
            elif fault=='dcsr_reverted':t['dcsr']&=~0x8003
            elif fault=='auto':e.writereg(parent,0x18,1)
            elif fault=='data0':e.writereg(parent,4,0xdeadbeef)
            elif fault=='data1':e.writereg(parent,5,0xdeadbeef)
            elif fault=='haltreq':t['haltreq']=True
            elif fault=='hartsel':t['registers'][0x10]|=1<<6
            elif fault=='restore_order':e.restore[-3:-1]=reversed(e.restore[-3:-1])
            elif fault=='repeat_resume':e.access.append(('run-resume',parent,e.run_clock_us))
            elif fault=='prepare_resume' and data[1]==3:e.access.append(('run-resume',parent,e.run_clock_us))
            elif fault=='missing_rehalt':e.access[:]=[row for row in e.access if row[0]!='run-rehalt']
            elif fault=='prep_running' and data[1]==3:t['halted']=False
            elif fault=='other_connection':
                for p,target in e.targets.items():
                    if p!=parent:target['pc']^=4
            elif fault=='frame_unknown_tlv':data+=tlv(127,b'x')
            return Completed(data,outcome)
        e.dispatch_request=broken_dispatch;e.interleave=broken_interleave
    model.install=faulty_install;r=RunChecks(c,a).run()
    assert next(row for row in r['checks'] if row['id']=='OEP-RUN-'+case)['status']=='failed',r['checks']


@pytest.mark.parametrize('invalid',['scope','operation','stopped','status','offer','budget'])
def test_run_preflight_blocks(invalid):
    model,c,a=setup();spec=c.reg['interface'][-1]
    if invalid=='scope':a.reset_scope='physical'
    elif invalid=='operation':spec['op'][-1]['code']=24
    elif invalid=='stopped':spec['enum']['run_stopped']['not_run']=4
    elif invalid=='status':c.reg['status']['timeout']=3
    elif invalid=='budget':model.ep.max_op_ms=2
    else:
        describe=model.extension.describe;model.extension.describe=lambda fn:tuple(row[:4]+bytes((row[4]&~0x40,)) if fn==5 and row[0]==7 else row for row in describe(fn))
    r=RunChecks(c,a).run();assert r['checks'][0]['status']=='failed'
    assert all(row['status']=='blocked' for row in r['checks'][1:])
    if invalid!='budget':assert not a.trace


@pytest.mark.parametrize('mode,pc',[('unknown',0),('stopped',-1),('stopped',True),('stopped',0x100000000)])
def test_invalid_run_behavior_preserves_state(mode,pc):
    model,_,_=setup();before=model.run_state()
    with pytest.raises(ValueError):model.run_behavior(mode,pc)
    assert model.run_state()==before


@pytest.mark.parametrize('memory,gprs',[(None,{}),({1:0},{}),({0:0x100000000},{}),({},None),({},{32:0}),({},{10:True})])
def test_invalid_live_run_program_controls_preserve_state(memory,gprs):
    model,c,_=setup();c.confirm()
    with c.holding(3000) as sid:
        parent=struct.unpack_from('<I',c.success(c.request(17,struct.pack('<BHHI',0,0,1,2000),session=sid,fn=1)))[0];before=model.run_state()
        with pytest.raises(ValueError):model.run_program(parent,memory,gprs)
        assert model.run_state()==before


def test_run_program_requires_live_parent():
    model,_,_=setup();before=model.run_state()
    with pytest.raises(ValueError):model.run_program(65535,{}, {})
    assert model.run_state()==before


def test_run_fixture_common_declarations_pass():
    from oep_client.conformance_interfaces import InterfaceChecks
    _,c,_=setup();r=InterfaceChecks(c).run()
    assert r['status']=='passed',r['checks']
    assert len(r['checks'])==41
