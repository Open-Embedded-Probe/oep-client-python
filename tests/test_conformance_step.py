from oep_client.conformance import Checks
from oep_client.conformance_step import StepChecks
from oep_client.conformance_step_sample import SampleStepAdapter
from oep_client.virtual_step_model import StepModel
from test_conformance_blocks import setup as block_setup


def setup():
    _,c,_=block_setup();c.reg['interface'][-1]['op'].append({'name':'step','code':23});c.reg['interface'][-1]['tlv']={'step_answer':{'step_left':1}}
    model=StepModel(lambda:0,boot_id=17)
    return model,Checks(model.handle,c.reg,'virtual-resource-1'),SampleStepAdapter(model)


def test_optional_step_contracts_pass():
    _,c,a=setup();r=StepChecks(c,a).run()
    assert r['status']=='passed',[(row['id'],row.get('error')) for row in r['checks'] if row['status']!='passed']
    assert len(r['checks'])==22 and not r['full_conformance']


import pytest
import struct
from oep_client.virtual_core import Completed


@pytest.mark.parametrize('fault,case',[
    ('wrong_moved','SELF-BRANCH'),('wrong_pc','MOVED'),('failure_pc','LATE-HALT'),
    ('failure_success','RUNNING'),('missing_left','RUNNING'),('nonempty_left','RUNNING'),
    ('spurious_left','MOVED'),('changed_prv','PRV-0'),('retained_step','OLD-STEP'),
    ('retained_haltreq','LATE-HALT'),('retained_hartsel','MOVED'),
    ('data0','MOVED'),('data1','PREPARE-LINE'),('restore_order','MOVED'),
    ('repeat_resume','MOVED'),('prepare_resume','PREPARE-FAULT'),('missing_rehalt','LATE-HALT'),
    ('mailbox_interleave','MAILBOX-OK'),('gpr','MOVED'),('kept_after_left','RUNNING'),
])
def test_step_checker_detects_independent_faults(fault,case):
    model,c,a=setup();install=model.install
    def faulty_install():
        install();e=model.extension;dispatch=e.dispatch_request;interleave=e.interleave;close=e.close_connection
        def kept_close(rid):
            if fault=='kept_after_left' and e.step_mode=='running':return
            close(rid)
        e.close_connection=kept_close
        def broken_interleave(parent):
            if fault=='mailbox_interleave':
                for rid,(connection,_) in e.streams.items():
                    if connection==parent:e.mailboxes[rid]['debug_busy']=False
            interleave(parent)
        def broken_dispatch(fn,op,payload,tags):
            value=dispatch(fn,op,payload,tags)
            if fn!=5 or op!=23:return value
            parent=struct.unpack_from('<I',payload)[0];t=e.targets.get(parent,{'dcsr':0,'haltreq':False,'registers':{0x10:0},'gprs':{12:0}});data=bytes(value);outcome=value.outcome
            if fault=='wrong_moved':data=data[:1]+b'\x01'+data[2:]
            elif fault=='wrong_pc':data=data[:2]+struct.pack('<I',0xdeadbeef)+data[6:]
            elif fault=='failure_pc' and outcome:data=data[:6]+struct.pack('<I',0x1004)+data[10:]
            elif fault=='failure_success' and outcome:outcome=0
            elif fault=='missing_left' and e.step_mode=='running':data=data[:10]+data[13:]
            elif fault=='nonempty_left' and e.step_mode=='running':data=data[:10]+b'\x01\x01\0x'+data[13:]
            elif fault=='spurious_left' and not outcome:data+=b'\x01\0\0'
            elif parent not in e.targets:pass                 # closed after step_left: nothing left to break
            elif fault=='changed_prv':t['dcsr']^=1
            elif fault=='retained_step':t['dcsr']|=4
            elif fault=='retained_haltreq':t['haltreq']=True
            elif fault=='retained_hartsel':t['registers'][0x10]|=1<<6
            elif fault=='data0':e.writereg(parent,4,0xdeadbeef)
            elif fault=='data1':e.writereg(parent,5,0xdeadbeef)
            elif fault=='restore_order':e.restore[-2:]=reversed(e.restore[-2:])
            elif fault=='repeat_resume':e.access.append(('step-resume',parent))
            elif fault=='prepare_resume' and e.step_mode.startswith('prepare-'):e.access.append(('step-resume',parent))
            elif fault=='gpr':t['gprs'][12]^=1
            elif fault=='missing_rehalt':e.access[:]=[row for row in e.access if row[0]!='step-rehalt']
            return Completed(data,outcome)
        e.dispatch_request=broken_dispatch;e.interleave=broken_interleave
    model.install=faulty_install;r=StepChecks(c,a).run()
    assert next(row for row in r['checks'] if row['id']=='OEP-STEP-'+case)['status']=='failed',r['checks']


def test_running_step_left_does_not_require_step_bit_to_remain_set():
    model,c,a=setup();install=model.install
    def permissive_install():
        install();e=model.extension;dispatch=e.dispatch_request
        def cleared(fn,op,payload,tags):
            value=dispatch(fn,op,payload,tags)
            if fn==5 and op==23 and e.step_mode=='running' and struct.unpack_from('<I',payload)[0] in e.targets:e.targets[struct.unpack_from('<I',payload)[0]]['dcsr']&=~4
            return value
        e.dispatch_request=cleared
    model.install=permissive_install;r=StepChecks(c,a).run()
    assert r['status']=='passed',r['checks']


@pytest.mark.parametrize('invalid',['scope','operation','tag','status','offer'])
def test_step_preflight_blocks(invalid):
    model,c,a=setup()
    if invalid=='scope':a.reset_scope='physical'
    elif invalid=='operation':c.reg['interface'][-1]['op'][-1]['code']=24
    elif invalid=='status':c.reg['status']['fault']=4
    elif invalid=='tag':c.reg['interface'][-1]['tlv']['step_answer']['step_left']=2
    else:
        describe=model.extension.describe
        model.extension.describe=lambda fn:tuple(row[:4]+bytes((row[4]&~0x80,)) if fn==5 and row[0]==7 else row for row in describe(fn))
    r=StepChecks(c,a).run();assert r['checks'][0]['status']=='failed'
    assert all(row['status']=='blocked' for row in r['checks'][1:]) and not a.trace


@pytest.mark.parametrize('method,args',[
    ('step_behavior',('unknown',0)),('step_behavior',('ok',-1)),('step_behavior',('ok',True)),
    ('step_seed',(65535,0,0)),('step_seed',(65535,-1,0)),('step_seed',(65535,0,0x100000000)),
])
def test_invalid_step_controls_preserve_state(method,args):
    model,_,_=setup();before=model.step_state()
    with pytest.raises(ValueError):getattr(model,method)(*args)
    assert model.step_state()==before


def test_step_fixture_common_declarations_pass():
    from oep_client.conformance_interfaces import InterfaceChecks
    _,c,_=setup();r=InterfaceChecks(c).run()
    assert r['status']=='passed',r['checks']
    assert len(r['checks'])==41
