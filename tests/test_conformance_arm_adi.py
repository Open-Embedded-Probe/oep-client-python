import copy
import struct
import pytest
from oep_client.conformance import Checks
from oep_client.conformance_arm_adi import ArmAdiChecks
from oep_client.conformance_arm_adi_sample import SampleArmAdiAdapter
from oep_client.virtual_arm_adi_model import ArmAdiModel
from oep_client.virtual_core import Completed, Reject
from test_conformance_attach import setup as attach_setup


def setup():
    _,c,_ = attach_setup(); reg = copy.deepcopy(c.reg)
    reg['interface'].append(dict(name='oep.target.arm-adi',revision=1,op=[dict(name=name,code=code) for name,code in [('transfer',16),('read_block',17),('write_block',18)]],enum=dict(swd_ack=dict(ok=1,wait=2,fault=4))))
    reg['status'] = dict(ok=0,wait=1,line=2,fault=3,timeout=4,state=5)
    reg['describe_common']['max_length'] = 3
    model = ArmAdiModel(lambda:0,boot_id=17)
    return model,Checks(model.handle,reg,'virtual-resource-1'),SampleArmAdiAdapter(model)


def test_arm_contracts_pass():
    _,c,a = setup(); result = ArmAdiChecks(c,a).run()
    assert result['status']=='passed',[(row['id'],row.get('error')) for row in result['checks'] if row['status']!='passed']
    assert len(result['checks'])==31 and not result['full_conformance']
    assert all(row['level']=='oep-interface' for row in result['checks'])


@pytest.mark.parametrize('fault,case',[
    ('ack','MIXED'),('done','MIXED'),('nvals','MIXED'),('read_order','POSTED'),('posted','POSTED'),
    ('wait_no_retry','WAIT-RECOVER'),('wait_write_twice','WAIT-RECOVER'),('failure_suffix','ACK-4-1'),
    ('reserved_touch','RESERVED'),('critical_touch','CRITICAL'),('resource_kind','RESOURCE'),
    ('frame_over','FRAME'),('block_stale','BLOCK-LIVE'),('block_select','BLOCK-LIVE'),('block_tar','BLOCK-LIVE'),('empty_bus','BLOCK-EMPTY')])
def test_arm_checker_detects_independent_faults(fault,case):
    model,c,a = setup(); install = model.install
    def corrupt():
        install(); e=model.extension; dispatch=e.dispatch_request; packet=e.packet
        def broken_packet(parent,req,value=None):
            if fault=='wait_no_retry' and e.acks[:1]==[2]: e.acks=[];return 1,2,None
            status,ack,result=packet(parent,req,value)
            if fault=='wait_write_twice' and value is not None and len(e.packets)>=3: e.executed.append((parent,req,value,None))
            if fault=='posted' and req&3==3: result=e.targets[parent]['ap'].get(req&12,0)
            if fault=='failure_suffix' and status:e.executed.append((parent,req,value,None))
            return status,ack,result
        def broken_dispatch(fn,op,payload,tags):
            if fault=='resource_kind' and fn==5:
                parent=int.from_bytes(payload[:4],'little')
                if parent in e.connections:e.connections[parent]['fn']=3
            limit=e.ep.probe.max_frame
            if fault=='frame_over':e.ep.probe.max_frame=1000
            try: value=dispatch(fn,op,payload,tags)
            except Reject:
                if fn==5 and fault in ('reserved_touch','critical_touch'):e.executed.append(('rejected',))
                raise
            finally:e.ep.probe.max_frame=limit
            if fn!=5:return value
            data=bytes(value);outcome=getattr(value,'outcome',0);parent=int.from_bytes(payload[:4],'little')
            if op==16:
                if fault=='ack':data=data[:3]+b'\x02'+data[4:]
                if fault=='done':data=b'\0\0'+data[2:]
                if fault=='nvals':data=data[:4]+b'\xff\xff'+data[6:]
                if fault=='read_order' and int.from_bytes(data[4:6],'little')>1:data=data[:6]+data[10:14]+data[6:10]+data[14:]
            else:
                if fault=='block_stale' and op==17 and len(data)>3:data=data[:3]+b'\0'*(len(data)-3)
                if fault=='block_select':e.targets[parent]['dp'][8]=0
                if fault=='block_tar':e.targets[parent]['ap'][4]=0
                if fault=='empty_bus' and payload[8:10]==b'\0\0':e.bus.append(('empty',))
            return Completed(data,outcome)
        e.packet=broken_packet;e.dispatch_request=broken_dispatch
        if fault=='critical_touch':
            parse=e.request_tags
            def tags(fn,op,data):
                try:return parse(fn,op,data)
                except Reject:
                    if fn==5:e.executed.append(('rejected',))
                    raise
            e.request_tags=tags
    model.install=corrupt;result=ArmAdiChecks(c,a).run()
    assert next(row for row in result['checks'] if row['id']=='OEP-ARM-'+case)['status']=='failed',result['checks']


@pytest.mark.parametrize('invalid',['scope','registry','ack','status'])
def test_arm_preflight_blocks_without_requests(invalid):
    _,c,a=setup()
    if invalid=='scope':a.reset_scope='physical'
    elif invalid=='registry':c.reg['interface'][-1]['op'][0]['code']=17
    elif invalid=='ack':c.reg['interface'][-1]['enum']['swd_ack']['wait']=4
    else:c.reg['status']['line']=3
    result=ArmAdiChecks(c,a).run()
    assert result['checks'][0]['status']=='failed' and not c.trace and not a.trace
    assert all(row['status']=='blocked' for row in result['checks'][1:])


def test_arm_max_length_must_use_common_u16_width():
    model,c,a=setup();describe=model.extension.describe
    def wrong(fn):
        return (b'\x07\x02\0\x10\x07',b'\x03\x04\0\x10\0\0\0') if fn==5 else describe(fn)
    model.extension.describe=wrong
    result=ArmAdiChecks(c,a).run()
    assert result['checks'][0]['status']=='failed'
    assert all(row['status']=='blocked' for row in result['checks'][1:])
