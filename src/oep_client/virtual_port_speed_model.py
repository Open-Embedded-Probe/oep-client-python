"""Logical UART bridge handshake with explicit response-write completion."""
import copy
import struct

from .virtual_link_transfer_model import LinkTransfers,LinkTransferModel
from .virtual_core import Core,Reject,tlv


class PortSpeedExtension(LinkTransfers):
    def __init__(self,model):super().__init__();self.model=model
    def describe(self,fn):
        return (tlv(7,b'\x10\x07'),) if fn==4 else super().describe(fn)
    def release(self):super().release();self.model.release_speed()
    def dispatch_request(self,fn,op,payload,tags):
        if fn==4 and op==18:return self.model.port_speed(payload)
        return super().dispatch_request(fn,op,payload,tags)


class PortSpeedModel(LinkTransferModel):
    rates=(115200,230400,460800)
    kinds={0:1,1:6,2:1}
    limits={0:64,1:256,2:256}

    def __init__(self,*,boot_id=17):
        self.clock_ms=0;self.ports={r:dict(state=0,baud=115200,deadline=None) for r in (0,2)}
        self.pending=None;self.actions=[];self.in_release_request=False;self.events=[];self.active_route=0
        super().__init__(lambda:self.clock_ms,boot_id=boot_id,max_frame=256)
        self.ep.static[0]=tuple(t for t in self.ep.static[0] if t[0]!=73)+tuple(tlv(73,bytes((r,k,255))) for r,k in self.kinds.items())
    def install(self):
        self.extension=PortSpeedExtension(self);self.extension.frame_limit=self.frame_limit
        self.ep.current_core=Core(self.ep,self.extension)
    def reset_port(self,route):self.ports[route]=dict(state=0,baud=115200,deadline=None)
    def reset_all(self):
        for r in self.ports:self.reset_port(r)
    def release_speed(self):
        if self.in_release_request:self.actions.append(('reset',))
        else:self.reset_all()
    def tick(self):
        self.ep.current_core.tick()
        for route,port in list(self.ports.items()):
            if port['state'] and self.clock_ms>=port['deadline']:self.reset_port(route)
    def advance(self,delta):
        if type(delta)is not int or delta<0:raise ValueError('forward integer clock required')
        self.clock_ms+=delta;self.tick()
    def port_speed(self,payload):
        baud,step,verify=struct.unpack('<IBH',payload);route=self.active_route
        if route not in self.ports:raise Reject(4,tlv(1,b'\6'))
        if step>2:raise Reject(11,b'\0')
        if step==0 and verify==0:raise Reject(3)
        port=self.ports[route]
        if step==2:
            if not port['state']:raise Reject(4,tlv(1,b'\6'))
            self.actions.append(('revert',route));return struct.pack('<I',115200)
        actual=min(self.rates,key=lambda value:abs(value-baud))
        if abs(actual-baud)*100>baud*2:raise Reject(11,b'\0')
        if step==0:
            if port['state'] or any(p['state'] for p in self.ports.values()):raise Reject(4,tlv(1,b'\6'))
            self.actions.append(('trial',route,actual,verify))
        else:
            if port['state']!=1 or baud!=port['baud']:raise Reject(4,tlv(1,b'\6'))
            self.actions.append(('commit',route,actual))
        return struct.pack('<I',actual)
    def begin(self,request,route=0):
        if self.pending is not None:raise ValueError('finish prior logical response write first')
        self.tick();self.active_route=route;self.actions=[]
        port=self.ports.get(route)
        if port and port['state']==2 and len(request)>=10 and request[0]==1:port['deadline']=self.clock_ms+3000
        e=self.extension;fixed,free=e.fixed,e.free;frame,limit=self.ep.probe.max_frame,e.frame_limit
        fn=int.from_bytes(request[3:5],'little') if len(request)>=6 else 0
        n=request[10] if len(request)>10 else 0
        if fn==4:
            count=int.from_bytes(request[10:12],'little') if len(request)>=12 else 0
            e.fixed={16:4,17:2+count,18:7};e.free={16,17}
        elif fn==1:e.fixed={16:0,17:1+2*n};e.free=set()
        else:e.fixed={16:1+3*n,17:1+2*n};e.free={17}
        self.in_release_request=fn==0 and len(request)>=6 and request[5] in (16,17)
        self.ep.probe.max_frame=e.frame_limit=self.limits[route]
        try:reply=self.ep.current_core.handle(request,route,defer_send=True)
        finally:
            self.in_release_request=False;e.fixed,e.free=fixed,free;self.ep.probe.max_frame,e.frame_limit=frame,limit
        token=self.ep.current_core.renewal
        self.pending=dict(route=route,request=request,reply=reply,token=token,actions=list(self.actions))
        self.events.append(dict(action='prepared',time_ms=self.clock_ms,route=route,request_hex=request.hex(),reply_hex=reply.hex() if reply else None,baud=port['baud'] if port else None))
        return reply
    def finish(self):
        if self.pending is None:raise ValueError('no prepared response')
        pending,self.pending=self.pending,None;route=pending['route'];port=self.ports.get(route)
        self.events.append(dict(action='sent' if pending['reply'] is not None else 'discard',time_ms=self.clock_ms,route=route,request_hex=pending['request'].hex(),reply_hex=pending['reply'].hex() if pending['reply'] else None,baud=port['baud'] if port else None))
        if pending['reply'] is not None:self.ep.current_core.response_sent(pending['token'])
        for action in pending['actions']:
            name=action[0]
            if name=='reset':self.reset_all();continue
            if name=='revert':self.reset_port(action[1]);continue
            c=self.ep.current_core
            if pending['token']!=(c.holder,c.generation):continue
            if name=='trial':
                _,r,baud,verify=action;self.ports[r]=dict(state=1,baud=baud,deadline=self.clock_ms+verify)
            if name=='commit':
                _,r,baud=action;self.ports[r]=dict(state=2,baud=baud,deadline=self.clock_ms+3000)
        if pending['reply'] is not None and port and self.ports[route]['state']==2:self.ports[route]['deadline']=self.clock_ms+3000
        return pending['reply']
    def handle(self,request,route=0):self.begin(request,route);return self.finish()
    def speed_state(self):return dict(time_ms=self.clock_ms,ports=copy.deepcopy(self.ports),protected=self.link_state(),pending=self.pending is not None)
    def speed_events(self):return list(self.events)
    def restart_fixture(self):
        self.pending=None;self.reset_all();return super().restart_fixture()
