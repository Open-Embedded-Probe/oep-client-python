"""Explicit packet-outcome fixture; retains connections after line failure.

No physical driver or automatic wire-loss policy is represented here. Host DMI
steps are never repeated after ambiguous delivery, including reads. Only a
probe-owned DMSTATUS read may retry line failure. Busy means not accepted.
"""
import copy
import struct
from .virtual_debug_model import Debug, DebugModel, SIZES
from .virtual_core import Core, Completed, Reject, tlv


class Retry(Debug):
    def __init__(self,ep):
        super().__init__(ep);self.plans={};self.idle={};self.levels={};self.packets=[]
    def rest(self,parent,failed):
        row=self.connections[parent];pins=[p for p in row['pins'] if p!=65535]
        self.levels[parent]=[dict(mode='released' if failed else 'wire',level=None if failed else (1-self.idle.get(parent,0) if i==1 else 1),pull=None) for i,_ in enumerate(pins)]
    def packet(self,parent,kind,address,value,speed):
        status=self.plans.get(parent,[]).pop(0) if self.plans.get(parent) else 0
        # Ambiguous delivery is deliberately represented as delivered, to catch
        # harmful retries even when a repeated DATA write looks idempotent.
        delivered=status in (0,2)
        if kind=='write' and delivered:
            self.writereg(parent,address,value)
            if address==0x10 and value&0x40000000:self.targets[parent]['halted']=False
        actual=self.readreg(parent,address) if kind=='read' and status==0 else None
        self.rest(parent,status==2)
        self.packets.append(dict(parent=parent,kind=kind,address=address,value=value,status=status,speed=speed,delivered=delivered,rest=copy.deepcopy(self.levels[parent])))
        return status,actual
    def transaction(self,parent,kind,address,value=None,owned=False):
        speed=self.connections[parent]['speed'];actual=None
        for attempt in range(2):
            status,actual=self.packet(parent,kind,address,value,speed if attempt==0 else max(1,speed//2))
            if status==0:return status,actual
            if status!=1 and not (owned and kind=='read' and status==2):break
        return status,actual
    def close_connection(self,parent):
        super().close_connection(parent);self.plans.pop(parent,None);self.idle.pop(parent,None);self.levels.pop(parent,None)
    def dispatch_request(self,fn,op,payload,tags):
        if fn!=5 or op!=16:return super().dispatch_request(fn,op,payload,tags)
        parent=struct.unpack_from('<H',payload)[0];self.resource(parent,'connection')
        if self.connections[parent]['fn']==3:raise Reject(4,tlv(1,b'\x06'))
        count=struct.unpack_from('<H',payload,2)[0];cursor=4;steps=[];time_sum=0
        for _ in range(count):
            if cursor>=len(payload) or payload[cursor] not in SIZES:raise Reject(3)
            size=SIZES[payload[cursor]];row=payload[cursor:cursor+size]
            if len(row)!=size:raise Reject(3)
            if row[0] in (4,5):time_sum+=struct.unpack_from('<I',row,size-4)[0]
            steps.append(row);cursor+=size
        if cursor!=len(payload):raise Reject(3)
        if time_sum>self.ep.max_op_ms*1000:raise Reject(11,b'\0')
        # Other grammar retains the existing debug fixture; it does not claim
        # packet failure injection in poll/wait steps.
        if any(row[0] not in (1,2) for row in steps):return super().dispatch_request(fn,op,payload,tags)
        related=[rid for rid,(connection,_) in self.streams.items() if connection==parent]
        for rid in related:self.mailboxes[rid]['debug_busy']=True
        values=[];done=0;status=0
        try:
            for row in steps:
                self.interleave(parent)
                status,value=self.transaction(parent,'write' if row[0]==1 else 'read',row[1],struct.unpack_from('<I',row,2)[0] if row[0]==1 else None)
                if status:break
                if row[0]==2:values.append(value)
                done+=1
            return Completed(struct.pack('<HBH',done,status,len(values))+b''.join(struct.pack('<I',v) for v in values)+tlv(127,b'x'),0 if not status else (2 if done else 1))
        finally:
            for rid in related:self.mailboxes[rid]['debug_busy']=False


class RetryModel(DebugModel):
    def install(self):self.extension=Retry(self.ep);self.ep.current_core=Core(self.ep,self.extension)
    def retry_state(self):
        state=self.debug_state();e=self.extension
        state.update(plans=copy.deepcopy(e.plans),idle=dict(e.idle),levels=copy.deepcopy(e.levels),packets=copy.deepcopy(e.packets),retry_limit=2,cut_policy='retain')
        return state
    def retry_plan(self,parent,statuses):
        if type(parent)is not int or parent not in self.extension.targets or type(statuses)is not list or any(type(s)is not int or s not in (0,1,2,3) for s in statuses):raise ValueError('live RISC-V parent and explicit ok/wait/line/fault list required')
        self.extension.plans[parent]=list(statuses)
    def retry_idle(self,parent,value):
        if type(parent)is not int or parent not in self.extension.targets or type(value)is not int or value not in (0,1) or self.extension.connections[parent]['fn']!=1:raise ValueError('live RVSWD parent and logical idle_clock 0/1 required')
        self.extension.idle[parent]=value
    def retry_probe_read(self,parent):
        if type(parent)is not int or parent not in self.extension.targets:raise ValueError('live RISC-V parent required')
        status,value=self.extension.transaction(parent,'read',0x11,owned=True)
        return dict(status=status,value=value)
