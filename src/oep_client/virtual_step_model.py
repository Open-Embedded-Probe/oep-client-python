"""Logical optional step fixture with explicit halt/fallback results."""
import struct
from .virtual_block_model import Blocks, BlockModel, HARTSEL
from .virtual_debug_model import fixed_dmi
from .virtual_core import Core, Completed, Reject, tlv


class Steps(Blocks):
    def __init__(self,ep):super().__init__(ep);self.step_mode='ok';self.step_after=0x1004
    def describe(self,fn):return (tlv(7,b'\x10\xb7'),tlv(3,struct.pack('<H',8))) if fn==5 else super().describe(fn)
    def dispatch_request(self,fn,op,payload,tags):
        if fn!=5 or op!=23:
            value=super().dispatch_request(fn,op,payload,tags)
            if fn<=3 and op==17 and len(value)>=13 and getattr(value,'outcome',0)==0:
                parent=struct.unpack_from('<I',value)[0];self.targets[parent].update(pc=0x1000,dcsr=3)
            return value
        parent=struct.unpack('<I',payload)[0];self.resource(parent,'connection')
        if self.connections[parent]['fn']==3:raise Reject(4,tlv(1,b'\x06'))
        target=self.targets[parent];before=target['pc'];mode=self.step_mode
        saved={reg:target['registers'].get(reg,0) for reg in (4,5)}
        self.writereg(parent,0x10,target['registers'].get(0x10,0)&~(HARTSEL|0x80000000));target['haltreq']=False
        related=[rid for rid,(connection,_) in self.streams.items() if connection==parent]
        for rid in related:self.mailboxes[rid]['debug_busy']=True
        target['dcsr']|=4;status=0
        try:
            self.writereg(parent,5,0x7b0);self.writereg(parent,4,target['dcsr']);self.interleave(parent)
            if mode in ('prepare-line','prepare-fault'):status=2 if mode=='prepare-line' else 3
            else:
                self.access.append(('step-resume',parent));target['halted']=False;self.interleave(parent);target['pc']=self.step_after
                if mode=='ok':target['halted']=True
                else:
                    status=5;target['haltreq']=True;self.access.append(('step-rehalt',parent));target['halted']=mode=='late-halt'
            if mode!='running':target['dcsr']&=~4
        finally:
            for reg in (5,4):self.writereg(parent,reg,saved[reg]);self.restore.append(('step-restore',parent,reg,saved[reg]))
            target['haltreq']=False;self.writereg(parent,0x10,target['registers'].get(0x10,0)&~(HARTSEL|0x80000000))
            for rid in related:self.mailboxes[rid]['debug_busy']=False
        result=struct.pack('<BBII',status,int(before!=target['pc']) if not status else 0,before if not status else 0,target['pc'] if not status else 0)
        if mode=='running':
            # step_left: the cleanup could not finish - the answer, then the connection and its streams close (debug §4.2)
            result+=tlv(1,b'');answer=Completed(result+tlv(127,b'x'),1);self.close_connection(parent);return answer
        return Completed(result+tlv(127,b'x'),int(bool(status)))


class StepModel(BlockModel):
    def install(self):self.extension=Steps(self.ep);self.ep.current_core=Core(self.ep,self.extension)
    def handle(self,request):
        if len(request)<6 or struct.unpack_from('<H',request,3)[0]!=5:return super().handle(request)
        e=self.extension;fixed,free=e.fixed,e.free;e.fixed={16:fixed_dmi(request[10:]),17:4,18:4,20:10,21:10,23:4};e.free=set()
        if request[5]==21 and len(request)>=20:e.fixed[21]=10+4*struct.unpack_from('<H',request,18)[0]
        try:return self.ep.handle(request)
        finally:e.fixed,e.free=fixed,free
    def step_state(self):
        state=self.block_state();state.update(step_mode=self.extension.step_mode,step_after=self.extension.step_after);return state
    def step_seed(self,parent,pc,dcsr):
        if parent not in self.extension.targets or any(type(v)is not int or not 0<=v<=0xffffffff for v in (pc,dcsr)):raise ValueError('live parent and u32 PC/DCSR required')
        self.extension.targets[parent].update(pc=pc,dcsr=dcsr)
    def step_behavior(self,mode,after):
        if mode not in ('ok','late-halt','running','prepare-line','prepare-fault') or type(after)is not int or not 0<=after<=0xffffffff:raise ValueError('explicit step mode and u32 final PC required')
        self.extension.step_mode=mode;self.extension.step_after=after
