"""Logical optional block fixture with explicit bus and scratch restoration."""
import copy
import struct
from .virtual_debug_model import Debug, DebugModel, fixed_dmi
from .virtual_core import Core, Completed, Reject, tlv


HARTSEL=(0x3ff<<6)|(0x3ff<<16)


class Blocks(Debug):
    max_length=8
    def __init__(self,ep):super().__init__(ep);self.memory={};self.restore=[];self.block_fail=None;self.block_status=2
    def describe(self,fn):return (tlv(7,b'\x10\x37'),tlv(3,struct.pack('<H',self.max_length))) if fn==5 else super().describe(fn)
    def close_connection(self,parent):super().close_connection(parent);self.memory.pop(parent,None)
    def dispatch_request(self,fn,op,payload,tags):
        if fn!=5 or op not in (20,21):
            value=super().dispatch_request(fn,op,payload,tags)
            if fn<=3 and op==17 and len(value)>=11 and getattr(value,'outcome',0)==0:
                parent=struct.unpack_from('<H',value)[0];self.targets[parent]['gprs']={8:0,9:0,10:0,11:0};self.memory[parent]={}
            return value
        parent,address,count=struct.unpack_from('<HIH',payload);self.resource(parent,'connection')
        if self.connections[parent]['fn']==3:raise Reject(4,tlv(1,b'\x06'))
        if address&3:raise Reject(3)
        if count*4>self.max_length:raise Reject(11,b'\0')
        target=self.targets[parent];self.writereg(parent,0x10,target['registers'].get(0x10,0)&~HARTSEL)
        if not count:return struct.pack('<HB',0,0)+tlv(127,b'x')
        if not target['halted']:return Completed(struct.pack('<HB',0,5)+tlv(127,b'x'),1)
        saved={reg:target['registers'].get(reg,0) for reg in (4,5,0x18)};gprs=dict(target['gprs'])
        related=[rid for rid,(connection,_) in self.streams.items() if connection==parent]
        for rid in related:self.mailboxes[rid]['debug_busy']=True
        done=0;status=0;values=[]
        try:
            target['gprs'].update({8:address,9:count,10:0xdead,11:0xbeef});self.writereg(parent,0x18,1)
            for index in range(count):
                at=address+4*index;self.writereg(parent,5,at);self.writereg(parent,4,0xface0000|index);self.interleave(parent)
                if index==self.block_fail:status=self.block_status;break
                if op==20:
                    if at not in self.memory[parent]:status=3;break
                    value=self.memory[parent][at];values.append(value);self.access.append(('bus-read',parent,at,value))
                else:
                    value=struct.unpack_from('<I',payload,8+4*index)[0];self.memory[parent][at]=value;self.access.append(('bus-write',parent,at,value))
                done+=1
        finally:
            target['gprs']=gprs
            for reg in (5,4,0x18):self.writereg(parent,reg,saved[reg]);self.restore.append(('restore',parent,reg,saved[reg]))
            for rid in related:self.mailboxes[rid]['debug_busy']=False
        return Completed(struct.pack('<HB',done,status)+b''.join(struct.pack('<I',value) for value in values)+tlv(127,b'x'),0 if not status else (2 if done else 1))


class BlockModel(DebugModel):
    def install(self):self.extension=Blocks(self.ep);self.ep.current_core=Core(self.ep,self.extension)
    def handle(self,request):
        if len(request)<6 or struct.unpack_from('<H',request,3)[0]!=5:return super().handle(request)
        e=self.extension;fixed,free=e.fixed,e.free;e.fixed={16:fixed_dmi(request[10:]),17:2,18:2,20:8,21:8};e.free=set()
        if request[5]==21 and len(request)>=18:e.fixed[21]=8+4*struct.unpack_from('<H',request,16)[0]
        try:return self.ep.handle(request)
        finally:e.fixed,e.free=fixed,free
    def block_state(self):
        state=self.debug_state();e=self.extension;state.update(memory=copy.deepcopy(e.memory),restore=list(e.restore),block_fail=e.block_fail,block_status=e.block_status)
        return state
    def block_memory(self,parent,values):
        if parent not in self.extension.memory or not isinstance(values,dict) or any(type(k)is not int or not 0<=k<=0xfffffffc or k&3 or type(v)is not int or not 0<=v<=0xffffffff for k,v in values.items()):raise ValueError('live parent and aligned u32 memory rows required')
        self.extension.memory[parent].update(values)
    def block_scratch(self,parent,values):
        if parent not in self.extension.targets or not isinstance(values,dict) or any(type(k)is not int or not 0<=k<32 or type(v)is not int or not 0<=v<=0xffffffff for k,v in values.items()):raise ValueError('live parent and GPR u32 values required')
        self.extension.targets[parent]['gprs'].update(values)
    def block_failure(self,index,status):
        if index is not None and (type(index)is not int or not 0<=index<=2):raise ValueError('logical block failure index must be 0..2 or None')
        if type(status)is not int or status not in (1,2,3,4,5):raise ValueError('standard block failure status required')
        self.extension.block_fail=index;self.extension.block_status=status
