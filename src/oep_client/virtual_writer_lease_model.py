"""Explicit deterministic clock and standard-resource deferred writer fixture."""
import struct
from .virtual_cut_model import Cut, CutModel
from .virtual_core import Core, tlv


class LeaseCut(Cut):
    def __init__(self,ep,model):super().__init__(ep);self.model=model
    def dispatch_request(self,*args):
        value=super().dispatch_request(*args)
        if args[:2]==(5,16):
            self.model.milliseconds+=self.model.execution_ms;self.model.execution_ms=0
        return value


class WriterLeaseModel(CutModel):
    def __init__(self,boot_id=None):
        self.milliseconds=0;self.execution_ms=0
        super().__init__(lambda:self.milliseconds,boot_id=boot_id)
        self.ep.max_op_ms=5000
        self.ep.static[0]=tuple(row for row in self.ep.static[0] if row[0]!=77)+(tlv(77,struct.pack('<I',5000)),)
    def install(self):
        self.extension=LeaseCut(self.ep,self);self.ep.current_core=Core(self.ep,self.extension);self.waiting=None;self.execution_ms=0
    def writer_lease_state(self):
        state=self.cut_state();core=self.ep.current_core
        state.update(clock_scope='explicit-logical-ms',now_ms=self.milliseconds,execution_ms=self.execution_ms,holder=core.holder,deadline=core.deadline,lease_ms=core.lease,generation=core.generation,max_op_ms=self.ep.max_op_ms)
        return state
    def writer_advance(self,ms):
        if type(ms)is not int or not 0<=ms<=60000:raise ValueError('explicit 0..60000 logical milliseconds required')
        self.milliseconds+=ms
    def writer_execution(self,ms):
        if type(ms)is not int or not 0<=ms<=5000 or self.waiting is not None:raise ValueError('idle writer and 0..5000 logical execution milliseconds required')
        self.execution_ms=ms
