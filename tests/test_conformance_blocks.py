from oep_client.conformance import Checks
from oep_client.conformance_blocks import BlockChecks
from oep_client.conformance_block_sample import SampleBlockAdapter
from oep_client.virtual_block_model import BlockModel
from test_conformance_debug import setup as debug_setup


def setup():
    _,c,_=debug_setup();c.reg['interface'][-1]['op'] += [{'name':'read_block','code':20},{'name':'write_block','code':21}]
    model=BlockModel(lambda:0,boot_id=17)
    return model,Checks(model.handle,c.reg,'virtual-resource-1'),SampleBlockAdapter(model)


def test_optional_block_contracts_pass():
    _,c,a=setup();r=BlockChecks(c,a).run()
    assert r['status']=='passed',[(row['id'],row.get('error')) for row in r['checks'] if row['status']!='passed']
    assert len(r['checks'])==22 and not r['full_conformance']


import pytest
import struct
from oep_client.virtual_core import Completed, Reject


@pytest.mark.parametrize('fault,case',[
    ('data0_not_restored','ROUNDTRIP'),('data1_not_restored','ROUNDTRIP'),
    ('auto_not_restored','READ-FAIL-0'),('gpr_not_restored','WRITE-FAIL-1'),
    ('unrelated_gpr_changed','ROUNDTRIP'),('restore_order','ROUNDTRIP'),
    ('hartsel_retained','ROUNDTRIP'),('stale_read','LIVE'),('wrong_done','ROUNDTRIP'),
    ('failed_write_suffix','WRITE-FAIL-1'),('alignment_ignored','READ-ALIGN'),
    ('length_ignored','READ-OVER'),('reject_access','READ-ALIGN'),
    ('zero_running_failed','ZERO-RUNNING'),('mailbox_interleave','MAILBOX'),
    ('cleanup_kept','CLEANUP-WRITE'),('cleanup_success','CLEANUP-READ'),
])
def test_block_checker_detects_independent_faults(fault,case):
    model,c,a=setup();install=model.install
    def faulty_install():
        install();e=model.extension;dispatch=e.dispatch_request;interleave=e.interleave;cache={};close=e.close_connection
        def kept_close(rid):
            if fault=='cleanup_kept' and e.restore_fail:return
            close(rid)
        e.close_connection=kept_close
        def broken_interleave(parent):
            if fault=='mailbox_interleave':
                for rid,(connection,_) in e.streams.items():
                    if connection==parent:e.mailboxes[rid]['debug_busy']=False
            interleave(parent)
        def broken_dispatch(fn,op,payload,tags):
            if fn!=5 or op not in (20,21):return dispatch(fn,op,payload,tags)
            parent=struct.unpack_from('<I',payload)[0];address=struct.unpack_from('<I',payload,4)[0];count=struct.unpack_from('<H',payload,8)[0]
            saved=dict(e.targets.get(parent,{}).get('registers',{}));start=len(e.restore)
            if fault=='alignment_ignored':payload=payload[:4]+struct.pack('<I',address&~3)+payload[8:]
            if fault=='length_ignored' and count>2:e.max_length=12
            if fault=='stale_read' and op==20 and parent in cache:return cache[parent]
            try:value=dispatch(fn,op,payload,tags)
            except Reject:
                if fault=='reject_access':e.access.append(('invalid-bus-access',parent))
                raise
            if op==20:cache[parent]=value
            outcome=getattr(value,'outcome',0);data=bytes(value)
            if fault=='cleanup_success' and e.restore_fail:return Completed(data[:2]+b'\0'+data[3:],0)
            if fault=='data0_not_restored' and count:e.writereg(parent,4,0xdeadbeef)
            elif fault=='data1_not_restored' and count:e.writereg(parent,5,0xdeadbeef)
            elif fault=='auto_not_restored' and count:e.writereg(parent,0x18,1)
            elif fault=='gpr_not_restored' and count:e.targets[parent]['gprs'][8]=0xdeadbeef
            elif fault=='unrelated_gpr_changed':e.targets[parent]['gprs'][12]=0xdeadbeef
            elif fault=='restore_order' and len(e.restore)>start+1:e.restore[start],e.restore[start+1]=e.restore[start+1],e.restore[start]
            elif fault=='hartsel_retained':e.writereg(parent,0x10,saved.get(0x10,0))
            elif fault=='wrong_done' and count:data=b'\0\0'+data[2:]
            elif fault=='failed_write_suffix' and op==21 and outcome:e.memory[parent][address+4]=0x56
            elif fault=='zero_running_failed' and count==0 and not e.targets[parent]['halted']:data=b'\0\0\x05'+data[3:];outcome=1
            return Completed(data,outcome)
        e.dispatch_request=broken_dispatch;e.interleave=broken_interleave
    model.install=faulty_install;r=BlockChecks(c,a).run()
    assert next(row for row in r['checks'] if row['id']=='OEP-BLOCK-'+case)['status']=='failed',r['checks']


@pytest.mark.parametrize('invalid',['scope','registry','pair','length'])
def test_block_preflight_blocks(invalid):
    model,c,a=setup()
    if invalid=='scope':a.reset_scope='physical'
    elif invalid=='registry':c.reg['interface'][-1]['op'][-1]['code']=22
    else:
        describe=model.extension.describe
        def wrong(fn):
            rows=describe(fn)
            if fn!=5:return rows
            return tuple(row[:4]+bytes((row[4]&~0x20,)) if row[0]==7 and invalid=='pair' else row[:3]+b'\x04\0' if row[0]==3 and invalid=='length' else row for row in rows)
        model.extension.describe=wrong
    r=BlockChecks(c,a).run();assert r['checks'][0]['status']=='failed'
    assert all(row['status']=='blocked' for row in r['checks'][1:]) and not a.trace


@pytest.mark.parametrize('method,args',[
    ('block_failure',(-1,2)),('block_failure',(0,0)),('block_failure',(True,2)),
    ('block_memory',(65535,{0:1})),('block_scratch',(65535,{8:1})),
])
def test_invalid_block_controls_preserve_state(method,args):
    model,_,_=setup();before=model.block_state()
    with pytest.raises(ValueError):getattr(model,method)(*args)
    assert model.block_state()==before


def test_block_fixture_common_declarations_pass():
    from oep_client.conformance_interfaces import InterfaceChecks
    _,c,_=setup();r=InterfaceChecks(c).run()
    assert r['status']=='passed',r['checks']
    assert len(r['checks'])==41
