"""Logical wire loss with an explicit response-writer gate and console close.

The fixture chooses two consecutive failed operations, not a universal loss
threshold. Physical driver behavior and concurrent writers are not modeled.
"""
import copy
import struct
from .virtual_retry_model import Retry, RetryModel
from .virtual_debug_model import fixed_dmi
from .virtual_core import Core


class Cut(Retry):
    def __init__(self,ep):
        super().__init__(ep);self.streak={};self.pending=set();self.lifecycle=[];self.closed_targets={};self.freed_pins={}
    def observe(self,parent,status):
        if status==2:self.streak[parent]=self.streak.get(parent,0)+1
        elif status==0:self.streak[parent]=0
    def decide(self,parent,source):
        if self.streak.get(parent,0)>=2:
            self.pending.add(parent);self.lifecycle.append(('decision',parent,source));return True
        return False
    def dispatch_request(self,fn,op,payload,tags):
        result=super().dispatch_request(fn,op,payload,tags)
        if fn==5 and op==16:
            parent=struct.unpack_from('<I',payload)[0];self.observe(parent,result[2])
            if result[2]==2:self.decide(parent,'request')
        return result
    def close_stream(self,rid):
        parent,_=self.streams[rid]
        super().close_stream(rid);self.lifecycle.append(('stream-close',rid,parent))   # released: no mark (debug §2)
    def close_connection(self,parent):
        snapshot=copy.deepcopy(self.targets.get(parent));pins=self.connections[parent]['pins']
        super().close_connection(parent);self.closed_targets[parent]=snapshot
        self.lifecycle.append(('connection-close',parent))
        for pin in pins:
            if pin!=65535:self.freed_pins[pin]=dict(mode='free',level=None,pull=None)
        self.lifecycle.append(('pins-free',parent));self.streak.pop(parent,None);self.pending.discard(parent)
    def console_poll_cut(self,rid):
        parent,stream=self.streams[rid]
        status,_=self.transaction(parent,'read',0x11,owned=True);self.observe(parent,status)
        if status==2 and self.decide(parent,'console'):
            self.close_connection(parent)                  # the connection and its stream; no mark nobody could read
        elif status==0:super().poll(rid)
        return status


class CutModel(RetryModel):
    def install(self):
        self.extension=Cut(self.ep);self.ep.current_core=Core(self.ep,self.extension);self.waiting=None
    def accept_request(self,request):
        if type(request)is not bytes or self.waiting is not None:raise ValueError('explicit bytes and an empty logical writer required')
        e=self.extension;fixed,free=e.fixed,e.free
        fn=struct.unpack_from('<H',request,3)[0] if len(request)>=6 else None
        if fn==5:e.fixed={16:fixed_dmi(request[10:]),17:4,18:4};e.free=set()
        elif fn==4:
            e.fixed={16:5,17:15,18:8,19:4,20:5,21:6,22:4,23:2};e.free={17,18,23}
            if request[5]==21 and len(request)>=16:e.fixed[21]=6+struct.unpack_from('<H',request,14)[0]
        else:e.fixed={17:9,18:4,19:1};e.free={19}
        try:result=self.ep.current_core.handle(request,0,defer_send=True)
        finally:e.fixed,e.free=fixed,free
        self.waiting=dict(request=request,result=result,token=self.ep.current_core.renewal)
        return dict(ready=result is not None,corr=struct.unpack_from('<H',result,1)[0] if result is not None else None)
    def flush_response(self):
        if self.waiting is None:raise ValueError('no pending response')
        row=self.waiting;self.waiting=None;e=self.extension;result=row['result']
        if result is not None and e.pending:e.lifecycle.append(('response-write',struct.unpack_from('<H',result,1)[0],result))
        self.ep.current_core.response_sent(row['token'])
        for parent in list(e.pending):
            if parent in e.connections:e.close_connection(parent)
        return result
    def handle(self,request):
        if self.waiting is None:self.accept_request(request)
        elif request!=self.waiting['request']:raise ValueError('logical writer is waiting for a different request')
        return self.flush_response()
    def cut_state(self):
        state=self.retry_state();e=self.extension
        state.update(cut_policy='two-line-operations',streak=dict(e.streak),pending=sorted(e.pending),lifecycle=list(e.lifecycle),closed_targets=copy.deepcopy(e.closed_targets),freed_pins=copy.deepcopy(e.freed_pins),writer_waiting=self.waiting is not None)
        return state
    def cut_console_poll(self,rid):
        if type(rid)is not int or rid not in self.extension.streams or self.waiting is not None:raise ValueError('live stream and idle logical writer required')
        return self.extension.console_poll_cut(rid)
    def cut_tick(self):self.ep.current_core.tick()
