from oep_client.conformance import Checks
from oep_client.conformance_reset import ResetChecks
from oep_client.conformance_reset_sample import SampleResetAdapter
from oep_client.virtual_reset_model import ResetModel
from test_conformance_step import setup as step_setup


def setup():
    _,c,_=step_setup()
    model=ResetModel(lambda:0,boot_id=17)
    return model,Checks(model.handle,c.reg,'virtual-resource-1'),SampleResetAdapter(model)


def test_optional_reset_contracts_pass():
    _,c,a=setup();r=ResetChecks(c,a).run()
    assert r['status']=='passed',[(row['id'],row.get('error')) for row in r['checks'] if row['status']!='passed']
    assert len(r['checks'])==25 and not r['full_conformance']


import pytest
from oep_client.conformance import ops
from oep_client.catalog import pack_ops
from oep_client.virtual_core import tlv
from oep_client.virtual_names import DC
from oep_client.registry import TARGET_RISCV_DM

RV = TARGET_RISCV_DM.op
import struct
from oep_client.virtual_core import Completed, Reject


@pytest.mark.parametrize('fault,case',[
    ('pc_mode0','OK-RUN'),('verified_mode0','OK-RUN'),('reserved_flags','OK-HALT-AT-RESET'),
    ('false_success','STATE-TIMEOUT-RUN'),('wrong_timeout','STATE-TIMEOUT-HALT-AT-RESET'),
    ('missing_ack','OK-RUN'),('release_haltreq','OK-HALT-AT-RESET'),('dm_no_retry','DM-RECOVER-RUN'),
    ('dm_before_release','DM-RECOVER-HALT-AT-RESET'),('dm_over_budget','DM-LINE-RUN'),
    ('connection_lost','DM-RECOVER-HALT-AT-RESET'),('reset_pin','OK-RUN'),
    ('ndmreset_left','OK-RUN'),('hartsel_left','OK-HALT-AT-RESET'),('haltreq_left','OK-HALT-AT-RESET'),
    ('mark_detail','CONSOLE-RUN'),('missing_mark','CONSOLE-HALT-AT-RESET'),
    ('sync_retained','CONSOLE-RUN'),('pending_retained','CONSOLE-HALT-AT-RESET'),
    ('buffer_lost','CONSOLE-HALT-AT-RESET'),('other_connection','CONSOLE-RUN'),
    ('mailbox_interleave','CONSOLE-RUN'),('reject_effect','MODE'),
    ('cleanup_kept','CLEANUP-LINE-RUN'),('cleanup_success','CLEANUP-LINE-HALT-AT-RESET'),
    ('mark_on_failure','CONSOLE-DM-LINE-RUN'),
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
                if fn==5 and op==RV['reset'] and fault=='reject_effect':e.access.append(('unexpected-reset',))
                raise
            if fn!=5 or op!=RV['reset']:return value
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
    elif invalid=='operation':spec['op']=[o for o in spec['op'] if o['name']!='reset']   # a SPEC without what the checker uses
    elif invalid=='mode':del spec['enum']['reset_mode']['halt_at_reset']
    elif invalid=='flags':del spec['enum']['reset_flags']['reached']
    elif invalid=='status':del c.reg['status']['line']
    elif invalid=='budget':model.ep.max_op_ms=2
    else:
        describe=model.extension.describe;model.extension.describe=lambda fn:tuple(tlv(DC['ops'],pack_ops(set(ops(row[1]))-{RV['reset']})) if fn==5 and row[0]==DC['ops'] else row for row in describe(fn))
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
