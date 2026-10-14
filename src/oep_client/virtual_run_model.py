"""Logical loader fixture; explicit program effects, no CPU or physical clock."""
import copy
import struct
from .virtual_reset_model import Resets, ResetModel
from .virtual_block_model import HARTSEL
from .virtual_debug_model import fixed_dmi
from .virtual_core import Core, Completed, Reject, tlv


def fixed_run(payload):
    if len(payload)<13:return 13
    cursor=13+6*payload[12]
    if cursor>=len(payload):return cursor+1
    return cursor+1+2*payload[cursor]


class Runs(Resets):
    def __init__(self,ep):super().__init__(ep);self.run_mode='stopped';self.run_pc=0x2004;self.run_clock_us=0;self.programs={}
    def describe(self,fn):return (tlv(7,b'\x10\xff'),tlv(3,struct.pack('<H',8))) if fn==5 else super().describe(fn)
    def close_connection(self,parent):super().close_connection(parent);self.programs.pop(parent,None)
    def dispatch_request(self,fn,op,payload,tags):
        if fn!=5 or op!=22:
            value=super().dispatch_request(fn,op,payload,tags)
            if fn<=3 and op==17 and len(value)>=13 and getattr(value,'outcome',0)==0:self.programs[struct.unpack_from('<I',value)[0]]={'memory':{},'gprs':{}}
            return value
        parent,pc,timeout,count=struct.unpack_from('<IIIB',payload);cursor=13;inputs=[]
        for _ in range(count):inputs.append(struct.unpack_from('<HI',payload,cursor));cursor+=6
        nout=payload[cursor];cursor+=1;outputs=[struct.unpack_from('<H',payload,cursor+2*i)[0] for i in range(nout)]
        self.resource(parent,'connection')
        if self.connections[parent]['fn']==3:raise Reject(4,tlv(1,b'\x06'))
        if timeout>self.ep.max_op_ms or 16+4*nout>self.ep.probe.max_frame:raise Reject(11,b'\0')
        mode=self.run_mode
        if any(not 0x1000<=reg<=0x101f for reg in [r for r,_ in inputs]+outputs):mode='prepare-fault'
        target=self.targets[parent];saved={reg:target['registers'].get(reg,0) for reg in (4,5,0x18)}
        related=[rid for rid,(p,_) in self.streams.items() if p==parent]
        for rid in related:self.mailboxes[rid]['debug_busy']=True
        self.writereg(parent,0x10,target['registers'].get(0x10,0)&~(HARTSEL|0x80000000));target['haltreq']=False
        restored=False;stopped=3;status=2 if mode=='prepare-line' else 3;values=[];dpc=0;elapsed=0
        def restore():
            nonlocal restored
            for reg in (5,4,0x18):self.writereg(parent,reg,saved[reg]);self.restore.append(('run-restore',parent,reg,saved[reg]))
            restored=True
        try:
            self.run_clock_us+=400;self.writereg(parent,5,0x7b1);self.writereg(parent,4,pc);self.writereg(parent,0x18,1);self.interleave(parent)
            if mode not in ('prepare-line','prepare-fault'):
                for reg,value in inputs:target['gprs'][reg-0x1000]=value
                target['pc']=pc;target['dcsr']=target['dcsr']&~3|0x8003
                restore();self.access.append(('run-resume',parent,self.run_clock_us));target['halted']=False;start=self.run_clock_us;self.interleave(parent)
                program=self.programs[parent];self.memory[parent].update(program['memory']);target['gprs'].update(program['gprs']);self.access.append(('run-program',parent,copy.deepcopy(program)))
                stopped={'stopped':1,'timeout-halted':0,'not-halted':2}[mode];status=0 if stopped==1 else 4
                self.run_clock_us+=100 if stopped==1 else timeout*1000+(200 if stopped==0 else 300)
                if stopped!=1:
                    target['haltreq']=True;self.writereg(parent,0x10,target['registers'].get(0x10,0)|0x80000000);self.access.append(('run-rehalt',parent,start+timeout*1000))
                target['halted']=stopped!=2;target['pc']=self.run_pc;self.access.append(('run-finish',parent,self.run_clock_us));elapsed=self.run_clock_us-start
                if stopped in (0,1):dpc=target['pc'];values=[target['gprs'].get(reg-0x1000,0) for reg in outputs]
        finally:
            if not restored:restore()
            target['haltreq']=False;self.writereg(parent,0x10,target['registers'].get(0x10,0)&~(HARTSEL|0x80000000))
            for rid in related:self.mailboxes[rid]['debug_busy']=False
        result=struct.pack('<BBIIB',status,stopped,dpc,elapsed,len(values))+b''.join(struct.pack('<I',v) for v in values)
        if len(result)+9<=self.ep.probe.max_frame:result+=tlv(127,b'x')
        return Completed(result,int(bool(status)))


class RunModel(ResetModel):
    def install(self):self.extension=Runs(self.ep);self.ep.current_core=Core(self.ep,self.extension)
    def handle(self,request):
        if len(request)<6 or struct.unpack_from('<H',request,3)[0]!=5:return super().handle(request)
        e=self.extension;fixed,free=e.fixed,e.free;e.fixed={16:fixed_dmi(request[10:]),17:4,18:4,19:5,20:10,21:10,22:fixed_run(request[10:]),23:4};e.free=set()
        if request[5]==21 and len(request)>=20:e.fixed[21]=10+4*struct.unpack_from('<H',request,18)[0]
        try:return self.ep.handle(request)
        finally:e.fixed,e.free=fixed,free
    def run_state(self):
        state=self.reset_state();e=self.extension;state.update(run_mode=e.run_mode,run_pc=e.run_pc,run_clock_us=e.run_clock_us,programs=copy.deepcopy(e.programs));return state
    def run_behavior(self,mode,pc):
        if mode not in ('stopped','timeout-halted','not-halted','prepare-line','prepare-fault') or type(pc)is not int or not 0<=pc<=0xffffffff:raise ValueError('explicit run mode and u32 PC required')
        self.extension.run_mode=mode;self.extension.run_pc=pc
    def run_program(self,parent,memory,gprs):
        if parent not in self.extension.programs or not isinstance(memory,dict) or not isinstance(gprs,dict) or any(type(k)is not int or not 0<=k<=0xfffffffc or k&3 or type(v)is not int or not 0<=v<=0xffffffff for k,v in memory.items()) or any(type(k)is not int or not 0<=k<32 or type(v)is not int or not 0<=v<=0xffffffff for k,v in gprs.items()):raise ValueError('live parent and explicit aligned memory/GPR u32 program effects required')
        self.extension.programs[parent]={'memory':dict(memory),'gprs':dict(gprs)}
