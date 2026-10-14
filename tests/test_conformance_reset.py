from oep_client.conformance import Checks
from oep_client.conformance_reset import ResetChecks
from oep_client.conformance_reset_sample import SampleResetAdapter
from oep_client.virtual_reset_model import ResetModel
from test_conformance_step import setup as step_setup


def setup():
    _,c,_=step_setup();spec=c.reg['interface'][-1];spec['op'].append({'name':'reset','code':19});spec.setdefault('enum',{}).update(reset_mode=dict(run=0,halt_at_reset=2),reset_flags=dict(reached=1))
    model=ResetModel(lambda:0,boot_id=17)
    return model,Checks(model.handle,c.reg,'virtual-resource-1'),SampleResetAdapter(model)


def test_optional_reset_contracts_pass():
    _,c,a=setup();r=ResetChecks(c,a).run()
    assert r['status']=='passed',[(row['id'],row.get('error')) for row in r['checks'] if row['status']!='passed']
    assert len(r['checks'])==25 and not r['full_conformance']


import pytest
import struct
from oep_client.virtual_core import Completed, Reject


@pytest.mark.parametrize('fault,case',[
    ('pc_mode0','OK-0'),('verified_mode0','OK-0'),('reserved_flags','OK-2'),
    ('false_success','STATE-TIMEOUT-0'),('wrong_timeout','STATE-TIMEOUT-2'),
    ('missing_ack','OK-0'),('release_haltreq','OK-2'),('dm_no_retry','DM-RECOVER-0'),
    ('dm_before_release','DM-RECOVER-2'),('dm_over_budget','DM-LINE-0'),
    ('connection_lost','DM-LINE-2'),('reset_pin','OK-0'),
    ('ndmreset_left','OK-0'),('hartsel_left','OK-2'),('haltreq_left','OK-2'),
    ('mark_detail','CONSOLE-0'),('missing_mark','CONSOLE-2'),
    ('sync_retained','CONSOLE-0'),('pending_retained','CONSOLE-2'),
    ('buffer_lost','CONSOLE-2'),('other_connection','CONSOLE-0'),
    ('mailbox_interleave','CONSOLE-0'),('reject_effect','MODE'),
    ('cleanup_kept','CLEANUP-LINE-0'),('cleanup_success','CLEANUP-LINE-2'),
    ('mark_on_failure','CONSOLE-DM-LINE-0'),
])
def test_reset_checker_detects_independent_faults(fault,case):
    model,c,a=setup();install=model.install
    def faulty_install():
        install();e=model.extension;dispatch=e.dispatch_request;interleave=e.interleave;close=e.close_connection
        def kept_close(rid):
            if fault=='cleanup_kept' and e.reset_result=='cleanup-line':return
            close(rid)
        e.close_connection=kept_close
        def broken_interleave(parent):
            if fault=='mailbox_interleave':
                for rid,(p,_) in e.streams.items():
                    if p==parent:e.mailboxes[rid]['debug_busy']=False
            interleave(parent)
        def broken_dispatch(fn,op,payload,tags):
            try:value=dispatch(fn,op,payload,tags)
            except Reject:
                if fn==5 and op==19 and fault=='reject_effect':e.access.append(('unexpected-reset',))
                raise
            if fn!=5 or op!=19:return value
            parent,mode=struct.unpack('<IB',payload);data=bytes(value);outcome=value.outcome
            if fault=='cleanup_success' and e.reset_result=='cleanup-line':return Completed(b'\0'+data[1:],0)
            if parent not in e.targets:return Completed(data,outcome)
            target=e.targets[parent]
            if fault=='pc_mode0':data=data[:2]+struct.pack('<I',0x2000)+data[6:]
            elif fault=='verified_mode0':data=data[:1]+bytes((data[1]|2,))+data[2:]
            elif fault=='reserved_flags':data=data[:1]+bytes((data[1]|4,))+data[2:]
            elif fault=='false_success':outcome=0
            elif fault=='wrong_timeout' and data[0]==4:data=b'\x03'+data[1:]
            elif fault=='missing_ack':e.access[:]=[row for row in e.access if row[0]!='ack-havereset']
            elif fault=='release_haltreq':e.access[:]=[(row[0],row[1],False) if row[0]=='ndmreset-release' else row for row in e.access]
            elif fault=='dm_no_retry':e.access[:]=[row for row in e.access if row[0]!='reset-dm-read' or row[2]!=2]
            elif fault=='dm_before_release':
                row=next(row for row in e.access if row[0]=='reset-dm-read');e.access.remove(row);e.access.insert(next(i for i,row in enumerate(e.access) if row[0]=='ndmreset-release'),row)
            elif fault=='dm_over_budget':e.access[:]=[row[:3]+(1000,) if row[0]=='reset-dm-read' else row for row in e.access]
            elif fault=='connection_lost':e.close_connection(parent)
            elif fault=='reset_pin':e.touch.append(('reset-pin',parent))
            elif fault=='ndmreset_left':target['registers'][0x10]|=2
            elif fault=='hartsel_left':target['registers'][0x10]|=1<<6
            elif fault=='haltreq_left':target['haltreq']=True
            elif fault=='other_connection':
                for p,t in e.targets.items():
                    if p!=parent:t['pc']^=4
            for rid,(p,stream) in e.streams.items():
                if p!=parent:continue
                if fault=='mark_detail' and stream.marks:stream.marks[-1]=stream.marks[-1][:-1]+(2,)
                elif fault=='missing_mark' and stream.marks:stream.marks.pop()
                elif fault=='sync_retained' and rid in e.protocols:e.protocols[rid]['synced']=True
                elif fault=='pending_retained' and rid in e.protocols:e.protocols[rid]['pending']=b'AB'
                elif fault=='buffer_lost':stream.data.clear()
                elif fault=='mark_on_failure' and outcome and not stream.marks:stream.mark(0x40,1)   # console §2: success only
            return Completed(data,outcome)
        e.dispatch_request=broken_dispatch;e.interleave=broken_interleave
    model.install=faulty_install;r=ResetChecks(c,a).run()
    assert next(row for row in r['checks'] if row['id']=='OEP-RESET-'+case)['status']=='failed',r['checks']


@pytest.mark.parametrize('invalid',['scope','operation','mode','flags','status','offer','budget'])
def test_reset_preflight_blocks(invalid):
    model,c,a=setup();spec=c.reg['interface'][-1]
    if invalid=='scope':a.reset_scope='physical'
    elif invalid=='operation':spec['op'][-1]['code']=24
    elif invalid=='mode':spec['enum']['reset_mode']['halt_at_reset']=3
    elif invalid=='flags':spec['enum']['reset_flags']['reached']=2
    elif invalid=='status':c.reg['status']['line']=3
    elif invalid=='budget':model.ep.max_op_ms=2
    else:
        describe=model.extension.describe;model.extension.describe=lambda fn:tuple(row[:4]+bytes((row[4]&~8,)) if fn==5 and row[0]==7 else row for row in describe(fn))
    r=ResetChecks(c,a).run();assert r['checks'][0]['status']=='failed'
    assert all(row['status']=='blocked' for row in r['checks'][1:])
    if invalid!='budget':assert not a.trace


@pytest.mark.parametrize('invalid',['unknown',None,0,True,[]])
def test_invalid_reset_controls_preserve_state(invalid):
    model,_,_=setup();before=model.reset_state()
    with pytest.raises(ValueError):model.reset_behavior(invalid)
    assert model.reset_state()==before


def test_reset_fixture_common_declarations_pass():
    from oep_client.conformance_interfaces import InterfaceChecks
    _,c,_=setup();r=InterfaceChecks(c).run()
    assert r['status']=='passed',r['checks']
    assert len(r['checks'])==41


def test_dm_line_may_proactively_clear_dmseq_sync_without_havereset_mark():
    from oep_client.virtual_dmseq_model import initial
    model,c,a=setup();install=model.install
    def permissive_install():
        install();e=model.extension;dispatch=e.dispatch_request
        def cleared(fn,op,payload,tags):
            value=dispatch(fn,op,payload,tags)
            if fn==5 and op==19 and e.reset_result=='dm-line':
                parent=struct.unpack_from('<I',payload)[0]
                for rid,(p,_) in e.streams.items():
                    if p==parent and rid in e.protocols:e.protocols[rid]=initial()
            return value
        e.dispatch_request=cleared
    model.install=permissive_install;r=ResetChecks(c,a).run();assert r['status']=='passed',r['checks']
