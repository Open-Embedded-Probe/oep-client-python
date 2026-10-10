from oep_client.conformance import Checks
from oep_client.conformance_debug import DebugChecks
from oep_client.conformance_debug_sample import SampleDebugAdapter
from oep_client.virtual_debug_model import DebugModel
from test_conformance_dmseq import setup as dmseq_setup


def setup():
    _,c,_=dmseq_setup();c.reg['interface'].append({'name':'oep.target.riscv-dm','revision':1,'op':[{'name':name,'code':code} for name,code in [('dmi',16),('halt',17),('resume',18)]],'enum':{'dmi_step':dict(write=1,read=2,poll_reads=3,wait_us=4,poll_us=5)}})
    c.reg['describe_common'].update(role_channels=1,max_clock_hz=2,max_length=3,min_clock_hz=4,features=5,channel_group=6)
    c.reg['status']=dict(ok=0,wait=1,line=2,fault=3,timeout=4,state=5)
    model=DebugModel(lambda:0,boot_id=17)
    return model,Checks(model.handle,c.reg,'virtual-resource-1'),SampleDebugAdapter(model)


def test_mandatory_debug_contracts_pass():
    _,c,a=setup();r=DebugChecks(c,a).run()
    assert r['status']=='passed',[(row['id'],row.get('error')) for row in r['checks'] if row['status']!='passed']
    assert len(r['checks'])==21 and not r['full_conformance']


import pytest
import struct
from oep_client.virtual_core import Completed, Reject


@pytest.mark.parametrize('fault,case',[
    ('done_wrong','MIXED'),('values_reverse','MIXED'),('timeout_fault','POLL-READS'),
    ('nvals_wrong','MIXED'),('raw_restore','MIXED'),('reject_access','UNKNOWN-LATE'),
    ('console_interleave','ARBITRATION'),('status_not_reread','ARBITRATION'),
    ('busy_left','ARBITRATION'),('resume_twice','HALT-RESUME'),
    ('halt_not_stop','HALT-RESUME'),('halt_failure_left','HALT-FAILURE'),
    ('line_has_value','POLL-LINE'),('timeout_no_value','POLL-TIMEOUT'),
    ('zero_us_twice','POLL-US'),
])
def test_debug_checker_detects_independent_faults(fault,case):
    model,c,a=setup();install=model.install
    def faulty_install():
        install();e=model.extension;dispatch=e.dispatch_request;interleave=e.interleave;poll=e.poll
        def broken_interleave(parent):
            if fault=='console_interleave':
                for rid,(conn,_) in e.streams.items():
                    if conn==parent:e.mailboxes[rid]['debug_busy']=False
            return interleave(parent)
        def broken_poll(rid):
            poll(rid)
            if fault=='status_not_reread' and e.mailboxes[rid]['events']:e.mailboxes[rid]['events']=e.mailboxes[rid]['events'][1:]
        def broken_dispatch(fn,op,payload,tags):
            before={p:dict(t['registers']) for p,t in e.targets.items()}
            try:value=dispatch(fn,op,payload,tags)
            except Reject:
                if fault=='reject_access' and fn==5:e.access.append(('invalid-access',))
                raise
            if fn!=5:return value
            parent=struct.unpack_from('<H',payload)[0];data=bytes(value);outcome=getattr(value,'outcome',0)
            if op==16:
                if fault=='done_wrong':data=b'\0\0'+data[2:]
                elif fault=='values_reverse' and data[3:5]==b'\x02\0':data=data[:5]+data[9:13]+data[5:9]+data[13:]
                elif fault=='timeout_fault' and data[2]==4:data=data[:2]+b'\x03'+data[3:]
                elif fault=='nvals_wrong':data=data[:3]+b'\xff\xff'+data[5:]
                elif fault=='raw_restore':
                    for address in (4,5):e.writereg(parent,address,before[parent].get(address,0))
                elif fault=='busy_left':
                    for rid,(conn,_) in e.streams.items():
                        if conn==parent:e.mailboxes[rid]['debug_busy']=True
                elif fault=='line_has_value' and data[2]==2:data=data[:3]+b'\x01\0'+struct.pack('<I',123)+data[5:]
                elif fault=='timeout_no_value' and data[2]==4:data=data[:3]+b'\0\0'+data[9:]
                elif fault=='zero_us_twice' and len(payload)>=18 and payload[4]==5 and payload[14:18]==b'\0'*4:e.access.append(('dmi-read',parent,payload[5],e.readreg(parent,payload[5])))
            elif op==18 and fault=='resume_twice':e.access.append(('resume',parent))
            elif op==17 and fault=='halt_not_stop':e.targets[parent]['halted']=False
            elif op==17 and fault=='halt_failure_left' and outcome:e.targets[parent]['haltreq']=True
            return Completed(data,outcome)
        e.dispatch_request=broken_dispatch;e.interleave=broken_interleave;e.poll=broken_poll
    model.install=faulty_install;r=DebugChecks(c,a).run()
    assert next(row for row in r['checks'] if row['id']=='OEP-DEBUG-'+case)['status']=='failed',r['checks']


@pytest.mark.parametrize('invalid',['scope','grammar','status'])
def test_debug_preflight_blocks(invalid):
    _,c,a=setup()
    if invalid=='scope':a.reset_scope='physical'
    elif invalid=='grammar':c.reg['interface'][-1]['enum']['dmi_step']['read']=3
    else:c.reg['status']['timeout']=3
    r=DebugChecks(c,a).run();assert r['checks'][0]['status']=='failed'
    assert all(row['status']=='blocked' for row in r['checks'][1:]) and not a.trace


@pytest.mark.parametrize('method,args',[
    ('debug_failure',(-1,2)),('debug_failure',(0,0)),('debug_hart_failure',(1,)),
    ('debug_registers',(65535,{4:0})),('debug_registers',(1,{4:-1})),
])
def test_invalid_debug_controls_do_not_change_state(method,args):
    model,_,_=setup();before=model.debug_state()
    with pytest.raises(ValueError):getattr(model,method)(*args)
    assert model.debug_state()==before


def test_debug_fixture_all_five_functions_pass_common_declarations():
    from oep_client.conformance_interfaces import InterfaceChecks
    _,c,_=setup();r=InterfaceChecks(c).run()
    assert r['status']=='passed',r['checks']
    assert len(r['checks'])==41 and all(row['level']=='interface' for row in r['checks'])
