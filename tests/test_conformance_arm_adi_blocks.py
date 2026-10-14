from oep_client.conformance_arm_adi_blocks import ArmAdiBlockChecks
from test_conformance_arm_adi import setup


def test_mem_ap_packet_contracts_pass():
    _,c,a=setup();r=ArmAdiBlockChecks(c,a).run()
    assert r['status']=='passed',[(x['id'],x.get('error')) for x in r['checks'] if x['status']!='passed']
    assert len(r['checks'])==59 and not r['full_conformance']


import struct
import pytest
from oep_client.virtual_core import Completed


@pytest.mark.parametrize('fault,case',[
    ('bypass','BOUNDARY-False'),('tar_skip','BOUNDARY-False'),('posted','BOUNDARY-False'),
    ('done','FAIL-True-4-1'),('suffix','FAIL-True-4-1'),('store_twice','BOUNDARY-True'),
    ('host_csw','BOUNDARY-False'),('delivery_required','DROP'),('replay','REPLAY-READ'),('no_buffer','BOUNDARY-False')])
def test_mem_ap_checker_catches_packet_and_progress_faults(fault,case):
    model,c,a=setup();install=model.install
    def corrupt():
        install();e=model.extension;packet=e.packet;dispatch=e.dispatch_request
        def broken_packet(parent,req,value=None):
            if fault=='tar_skip' and req==5 and value>=0x2400:return 0,1,None
            if fault=='no_buffer' and req==14:return 0,1,e.targets[parent]['pending']
            status,ack,result=packet(parent,req,value)
            if fault=='posted' and req==14:result=0xfedcba98
            if fault=='suffix' and status and req==13:e.bus.append((parent,'write',e.targets[parent]['ap'][4],value))
            if fault=='store_twice' and not status and req==13:e.bus.append(e.bus[-1])
            return status,ack,result
        def broken_dispatch(fn,op,payload,tags):
            if fault=='bypass' and fn==5 and op==17:
                parent,address,count=struct.unpack('<IIH',payload)
                values=[e.targets[parent]['memory'].get(address+4*i,0) for i in range(count)]
                e.bus += [(parent,'read',address+4*i,v) for i,v in enumerate(values)]
                return struct.pack('<HB',count,0)+b''.join(struct.pack('<I',v) for v in values)
            value=dispatch(fn,op,payload,tags)
            if fn!=5 or op==16:return value
            data=bytes(value);outcome=getattr(value,'outcome',0);parent=int.from_bytes(payload[:4],'little')
            if fault=='done' and data[2]:data=struct.pack('<H',int.from_bytes(data[:2],'little')+1)+data[2:]
            if fault=='host_csw':e.targets[parent]['ap'][0]=0
            if fault=='delivery_required' and op==18 and not e.deliver:data=b'\0\0\x03';outcome=1
            return Completed(data,outcome)
        e.packet=broken_packet;e.dispatch_request=broken_dispatch
        if fault=='replay':
            core=model.ep.current_core;handle=core.handle
            def replay(request,route,**kwargs):
                corr=int.from_bytes(request[1:3],'little')
                if request[3:6]==b'\x05\0\x11' and corr in core.cache:core.cache.pop(corr);core.high=corr-1
                return handle(request,route,**kwargs)
            core.handle=replay
    model.install=corrupt;r=ArmAdiBlockChecks(c,a).run()
    assert next(x for x in r['checks'] if x['id']=='OEP-ARM-PACKET-'+case)['status']=='failed',r['checks']
