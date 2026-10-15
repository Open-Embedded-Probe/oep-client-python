"""Bounded logical save execution, inputs held while it writes and answered after its answer, response-write barrier."""
import struct
from .virtual_config_model import ConfigModel,FIXED,FREE
from .virtual_core import tlv


class ConfigExecutionModel(ConfigModel):
    routes=(0,1,2)
    def __init__(self,*,boot_id=17):
        self.clock_ms=0;self.pending=None;self.in_write=False;self.execution_ms=0;self.probes=[];self.events=[];self.writes=0
        self.held=[]          # (route, request) that came while save wrote: answered, in order, after save's answer (core §4.4)
        super().__init__(boot_id=boot_id);self.ep.now=lambda:self.clock_ms;self.ep.max_op_ms=5000
        self.ep.static[0]=tuple(t for t in self.ep.static[0] if t[0] not in (73,77))+tuple(tlv(73,bytes((r,k,255))) for r,k in ((0,1),(1,4),(2,6)))+(tlv(77,struct.pack('<I',5000)),)
    def arm(self,duration,probes):
        if type(duration)is not int or not 0<=duration<=5000 or self.pending is not None:raise ValueError('idle fixture and0..5000ms required')
        self.execution_ms=duration;self.probes=list(probes)
    def write_config(self,items):
        start=self.clock_ms;duration=self.execution_ms;self.in_write=True
        try:
            self.clock_ms+=duration//2
            for route,request in self.probes:
                reply=self.handle(request,route)
                self.events.append(dict(action='input-result',time_ms=self.clock_ms,route=route,request_hex=request.hex(),reply_hex=None if reply is None else reply.hex()))
            self.clock_ms+=duration-duration//2
            super().write_config(items);self.writes+=1
            self.events.append(dict(action='execution',started_ms=start,finished_ms=self.clock_ms,duration_ms=duration,writes=self.writes))
        finally:self.in_write=False
    def advance(self,delta):
        if type(delta)is not int or delta<0:raise ValueError('forward logical milliseconds required')
        self.clock_ms+=delta;self.ep.current_core.tick()
    def begin(self,request,route=0):
        if route not in self.routes or self.pending is not None:raise ValueError('explicit idle route required')
        e=self.extension;fixed,free=e.fixed,e.free
        fn=int.from_bytes(request[3:5],'little') if len(request)>=6 else 0;n=request[10] if len(request)>10 else 0
        if fn==4:e.fixed,e.free=FIXED,FREE
        elif fn==1:e.fixed={16:0,17:1+2*n};e.free=set()
        else:e.fixed={16:1+3*n,17:1+2*n};e.free={17}
        try:reply=self.ep.current_core.handle(request,route,defer_send=True)
        finally:e.fixed,e.free=fixed,free;self.execution_ms=0;self.probes=[]
        self.pending=dict(reply=reply,token=self.ep.current_core.renewal,request=request,route=route)
        self.events.append(dict(action='prepared',time_ms=self.clock_ms,route=route,request_hex=request.hex(),reply_hex=None if reply is None else reply.hex()))
        return reply
    def finish(self):
        if self.pending is None:raise ValueError('no prepared response')
        p,self.pending=self.pending,None
        if p['reply'] is not None:self.ep.current_core.response_sent(p['token'])
        self.events.append(dict(action='sent',time_ms=self.clock_ms,route=p['route'],request_hex=p['request'].hex(),reply_hex=None if p['reply'] is None else p['reply'].hex()))
        self.answer_held()
        return p['reply']
    def answer_held(self):
        # core §4.4: new requests are taken without loss while an earlier one runs, and answered in the order received.
        held,self.held=self.held,[]
        for route,request in held:
            reply=self.handle(request,route)
            self.events.append(dict(action='held-result',time_ms=self.clock_ms,route=route,request_hex=request.hex(),reply_hex=None if reply is None else reply.hex()))
    def handle(self,request,route=0):
        if self.in_write:
            self.events.append(dict(action='suppressed',time_ms=self.clock_ms,route=route,request_hex=request.hex(),reply_hex=None))
            self.held.append((route,request));return None
        self.begin(request,route);return self.finish()
    def restart_fixture(self):
        if self.pending is not None or self.in_write:raise ValueError('idle fixture required')
        self.clock_ms=0;self.execution_ms=0;self.probes=[];self.held=[]
        return super().restart_fixture()
    def execution_state(self):
        c=self.ep.current_core
        return dict(self.config_state(),time_ms=self.clock_ms,pending=self.pending is not None,writes=self.writes,
                    core=dict(holder=c.holder,deadline=c.deadline,generation=c.generation),events=list(self.events))
