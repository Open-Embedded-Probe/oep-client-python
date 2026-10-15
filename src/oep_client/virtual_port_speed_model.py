"""Logical UART bridge handshake with explicit response-write completion.

Phases (link §3): 0 boot, 1 trial, 2 committed, 3 a return waiting for the
frame boundary (a frame on the port is still being received or written).
"""
import copy
import struct

from .virtual_link_transfer_model import LinkTransfers,LinkTransferModel
from .virtual_core import Core,Reject,tlv

BOOT=115200
CAUSE6=tlv(1,b'\6')


class PortSpeedExtension(LinkTransfers):
    def __init__(self,model):super().__init__();self.model=model
    def describe(self,fn):
        return (tlv(7,b'\x10\x0f'),) if fn==4 else super().describe(fn)
    def release(self):super().release();self.model.release_speed()
    def dispatch_request(self,fn,op,payload,tags):
        if fn==4 and op==18:return self.model.port_speed(payload)
        if fn==4 and op==19:return self.model.port_state()
        return super().dispatch_request(fn,op,payload,tags)


class PortSpeedModel(LinkTransferModel):
    rates=(115200,230400,460800)
    kinds={0:1,1:6,2:1}
    limits={0:64,1:256,2:256}

    def __init__(self,*,boot_id=17):
        self.clock_ms=0;self.ports={r:self.boot() for r in (0,2)}
        self.pending=None;self.actions=[];self.in_release_request=False;self.events=[];self.active_route=0
        self.request=b'';self.receiving=set();self.handling=None
        super().__init__(lambda:self.clock_ms,boot_id=boot_id,max_frame=256)
        self.ep.static[0]=tuple(t for t in self.ep.static[0] if t[0]!=73)+tuple(tlv(73,bytes((r,k,255))) for r,k in self.kinds.items())
    def install(self):
        self.extension=PortSpeedExtension(self);self.extension.frame_limit=self.frame_limit
        self.ep.current_core=Core(self.ep,self.extension)
    @staticmethod
    def boot():return dict(state=0,baud=BOOT,asked=None,deadline=None,owner=None,why=None)
    def reset_port(self,route):self.ports[route]=self.boot()
    def reset_all(self):
        for r in self.ports:self.reset_port(r)
    def busy(self,route):
        return route in self.receiving or route==self.handling or (self.pending is not None and self.pending['route']==route)
    def fall(self,route,why):
        # A return condition: back at once, or phase 3 until the frame boundary.
        # A session return is never cancelled, so it overrides a pending idle return.
        p=self.ports[route]
        if not p['state']:return
        if not self.busy(route):self.reset_port(route)
        elif p['state']!=3 or why=='session':p.update(state=3,why=why)
    def release_speed(self):
        if self.in_release_request:
            # end/force: answer at the old baud first, then back.
            self.actions.append(('reset',))
            for p in self.ports.values():
                if p['state']:p.update(state=3,why='session')
        else:
            for r in self.ports:self.fall(r,'session')
    def cancel(self):
        # A normal request frame on the port while the session that raised it is still there (whoever sent it:
        # the timer is not the lease, link §3), before the step table.
        p=self.ports.get(self.active_route);holder=self.ep.current_core.holder
        if p and p['why']=='idle' and len(self.request)>=10 and self.request[0]==1 and holder is not None and holder==p['owner']:p.update(state=2,why=None,deadline=self.clock_ms+3000)
    def tick(self):
        self.ep.current_core.tick()
        for route,port in list(self.ports.items()):
            if port['state'] in (1,2) and self.clock_ms>=port['deadline']:self.fall(route,'verify' if port['state']==1 else 'idle')
    def advance(self,delta):
        if type(delta)is not int or delta<0:raise ValueError('forward integer clock required')
        self.clock_ms+=delta;self.tick()
    def receive(self,route):
        # The first bytes of the next frame on `route` arrived; begin/handle completes it.
        if self.pending is not None:raise ValueError('finish prior logical response write first')
        self.receiving.add(route)
    def port_speed(self,payload):
        baud,step,verify=struct.unpack('<IBH',payload);route=self.active_route
        if route not in self.ports:raise Reject(4,CAUSE6)
        if step>2:raise Reject(11,b'\0')
        if step==0 and verify==0:raise Reject(3)
        port=self.ports[route]
        if port['state']==3:raise Reject(4,CAUSE6)
        if step==2:
            if not port['state']:raise Reject(4,CAUSE6)
            self.actions.append(('revert',route));return struct.pack('<I',BOOT)
        if step==1:
            if port['state']!=1 or baud!=port['asked']:raise Reject(4,CAUSE6)
            self.actions.append(('commit',route));return struct.pack('<I',port['baud'])
        actual=min(self.rates,key=lambda value:abs(value-baud))
        if abs(actual-baud)*100>baud*2 or actual<=BOOT:raise Reject(11,b'\0')
        if any(p['state'] for p in self.ports.values()):raise Reject(4,CAUSE6)
        self.actions.append(('trial',route,actual,baud,verify))
        return struct.pack('<I',actual)
    def port_state(self):
        p=self.ports.get(self.active_route)
        if p is None:raise Reject(4,CAUSE6)
        return struct.pack('<IB',p['baud'],p['state'])
    def begin(self,request,route=0):
        if self.pending is not None:raise ValueError('finish prior logical response write first')
        self.tick();self.receiving.discard(route);self.active_route,self.request,self.handling=route,request,route;self.actions=[]
        port=self.ports.get(route)
        if port and port['state']==2 and len(request)>=10 and request[0]==1:port['deadline']=self.clock_ms+3000
        e=self.extension;fixed,free=e.fixed,e.free;frame,limit=self.ep.probe.max_frame,e.frame_limit
        fn=int.from_bytes(request[3:5],'little') if len(request)>=6 else 0
        n=request[10] if len(request)>10 else 0
        if fn==4:
            count=int.from_bytes(request[10:12],'little') if len(request)>=12 else 0
            e.fixed={16:4,17:2+count,18:7,19:0};e.free={16,17,19}
        elif fn==1:e.fixed={16:0,17:1+2*n};e.free=set()
        else:e.fixed={16:1+3*n,17:1+2*n};e.free={17}
        self.in_release_request=fn==0 and len(request)>=6 and request[5] in (16,17)
        self.ep.probe.max_frame=e.frame_limit=self.limits[route]
        started=self.clock_ms;raised=[p for p in self.ports.values() if p['state']]
        try:
            self.cancel()
            reply=self.ep.current_core.handle(request,route,defer_send=True)
        finally:
            self.in_release_request=False;self.handling=None;e.fixed,e.free=fixed,free;self.ep.probe.max_frame,e.frame_limit=frame,limit
        # Execution consumes wall time but neither verify nor idle time, whichever
        # route executes. The prepared response's write wait remains ordinary time.
        for p in raised:
            if p['state'] in (1,2) and any(p is q for q in self.ports.values()):p['deadline']+=self.clock_ms-started
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
                _,r,baud,asked,verify=action;self.ports[r]=dict(state=1,baud=baud,asked=asked,deadline=self.clock_ms+verify,owner=c.holder,why=None)
            if name=='commit':
                if self.ports[action[1]]['state']==1:self.ports[action[1]].update(state=2,deadline=self.clock_ms+3000)
        if pending['reply'] is not None and port and self.ports[route]['state']==2:self.ports[route]['deadline']=self.clock_ms+3000
        # The frame boundary: a pending return happens now.
        for r,p in self.ports.items():
            if p['state']==3 and not self.busy(r):self.reset_port(r)
        return pending['reply']
    def handle(self,request,route=0):self.begin(request,route);return self.finish()
    def speed_state(self):return dict(time_ms=self.clock_ms,ports=copy.deepcopy(self.ports),protected=self.link_state(),pending=self.pending is not None,receiving=sorted(self.receiving))
    def speed_events(self):return list(self.events)
    def restart_fixture(self):
        self.pending=None;self.receiving=set();self.reset_all();return super().restart_fixture()
