"""Independent, explicitly clocked capture-group fixture (immediate common start).

Reuses independent ADC/logic encoders, not the legacy capture-group or Host.
This finite fixture accepts equal-rate groups only (explicit resource limit).
fn 4 is the checked group and fn 5 a peer group that only holds tracks; fns
6..12 are one-role logic tracks used to exceed a 64-byte route frame.
"""
import copy
import struct
from fractions import Fraction
from .virtual_gpio_model import Gpio
from .virtual_logic_model import Logic, LogicModel
from .virtual_analog_model import Analog, refuse_output_idle
from .virtual_core import Core, Reject, tlv

NO_TIME=(1<<64)-1
LAST=0xffffffff
GROUPS=(4,5)
EXTRA=tuple(range(6,13))


class Group:
    def __init__(self):self.bound=[];self.generation=0;self.start_ns=NO_TIME


class CaptureGroup(Gpio):
    functions=(1,2,3,*GROUPS,*EXTRA)
    requires_equal_rate=True
    def __init__(self,ep):
        super().__init__();self.ep=ep;self.tracks={2:Analog(ep),3:Logic(ep),**{fn:Logic(ep) for fn in EXTRA}}
        self.idle.update({ch:0 for ch in (0,1,*range(8,16))});self.pins=dict(self.idle);self.external.update({ch:ch%2 for ch in range(8,16)})   # the analog's 0, 1: no output idle (capture §1.2)
        self.groups={fn:Group() for fn in GROUPS}
    # fn 4 is the group the checker drives; these keep its fields directly reachable.
    bound=property(lambda self:self.groups[4].bound,lambda self,value:setattr(self.groups[4],'bound',value))
    generation=property(lambda self:self.groups[4].generation,lambda self,value:setattr(self.groups[4],'generation',value))
    start_ns=property(lambda self:self.groups[4].start_ns,lambda self,value:setattr(self.groups[4],'start_ns',value))
    def names(self):
        return [(fn,b'oep.probe.plan' if fn==1 else b'oep.fixture.analog' if fn==2 else b'oep.fixture.capture-group' if fn in GROUPS else b'oep.fixture.logic') for fn in self.functions]
    def list_page(self,first,limit):
        rows=[];seen={}
        for fn,name in self.names():
            instance=seen.get(name,0);seen[name]=instance+1;rows.append(struct.pack('<HHBB',fn,instance,1,len(name))+name)
        page=bytearray(struct.pack('<HB',len(rows),0))
        for row in rows[first:]:
            if len(page)+len(row)>limit:break
            page.extend(row);page[2]+=1
        return bytes(page)
    def describe(self,fn):
        if fn==1:return (tlv(7,b'\x10\x03'),tlv(64,struct.pack('<I',16)))
        if fn in GROUPS:return (tlv(7,b'\x10\x17'),tlv(64,bytes((len(self.tracks),))+b''.join(struct.pack('<H',t) for t in sorted(self.tracks))))
        rows=list(self.tracks[fn].describe(2))
        rows=[tlv(7,b'\x10\xff'+(b'\3' if fn==2 else b'\1')) if r[0]==7 else r for r in rows]
        if fn==3:rows=[tlv(1,bytes((r[3],0,0,12))) if r[0]==1 else r for r in rows if r[0]!=1 or r[3] in (0,1)]
        if fn in EXTRA:rows=[tlv(1,struct.pack('<BHH',0,0,1<<(fn-2))) if r[0]==1 else r for r in rows if r[0]!=1 or r[3]==0]
        if fn==3:rows=[tlv(68,b'\2') if r[0]==68 else r for r in rows]
        if fn in EXTRA:rows=[tlv(68,b'\1') if r[0]==68 else r for r in rows]
        return tuple(rows)
    def request_tags(self,fn,op,data):
        if fn==1:return super().request_tags(fn,op,data)
        if fn in self.tracks:return self.tracks[fn].request_tags(2,op,data)
        while data:
            if len(data)<3:raise Reject(3)
            raw,size=struct.unpack_from('<BH',data);key=raw&127
            if not key or len(data)<3+size:raise Reject(3)
            data=data[3+size:]
            if raw&128:raise Reject(11,bytes((raw,)))   # v1 defines no group request TLV: every copy (core §2.3)
        return {}
    def owner(self,fn):return next((gid for gid,g in self.groups.items() if fn in g.bound),None)
    def unbind(self,gid=4):self.groups[gid].bound=[]
    def release(self):
        for gid in self.groups:self.unbind(gid)
        self.release_plans(list(self.plans))
    def release_plans(self,functions):
        for fn in functions:
            if fn in self.tracks:
                t=self.tracks[fn];t.roles=[];t.config=None;t.clear(0)
        super().release_plans(functions)
    @staticmethod
    def refusal(cause,fn=None):return Reject(4,tlv(1,bytes((cause,)))+(tlv(5,struct.pack('<H',fn)) if fn is not None else b''))
    def assigned(self,fn):return sorted(zip(self.tracks[fn].roles,self.plans.get(fn,[]))) if fn in self.tracks else []
    def plan(self,op,payload,tags):
        if op==17:
            count=payload[0];functions=list(struct.unpack('<'+'H'*count,payload[1:])) or list(self.plans)
            if any(fn and fn not in self.functions for fn in functions):raise Reject(1)   # every fn first; fn 0 has no plan (plan §2.2)
            for fn in functions:
                if self.owner(fn) is not None:raise self.refusal(4,fn)
            self.release_plans(functions);return b''
        rows=[(raw,*struct.unpack('<HBH',value)) for raw,value in tags]
        if len(set((fn,role,ch) for _,fn,role,ch in rows))!=len(rows):raise Reject(3)
        affected={r[1] for r in rows}
        for raw,fn,role,ch in rows:
            if fn==0:raise Reject(3)
            if fn not in self.functions:raise Reject(1)
            roles,channels=((1,3),(0,1)) if fn==2 else ((0,1),(2,3)) if fn==3 else ((0,),(fn-2,))
            if fn not in self.tracks or role not in roles or ch not in channels:raise Reject(11,bytes((raw,)))
        if len({(r[1],r[2]) for r in rows})!=len(rows) or len({r[3] for r in rows})!=len(rows):raise Reject(11,bytes((rows[0][0],)))   # one channel per role: unsupported (plan §2.4)
        refuse_output_idle(self.idle,(r[3] for r in rows if r[1]==2))   # the analog track (capture §1.2)
        wanted={fn:sorted((role,ch) for _,owner,role,ch in rows if owner==fn) for fn in affected}
        for fn in sorted(affected):
            if self.owner(fn) is not None and wanted[fn]!=self.assigned(fn):raise self.refusal(4,fn)
        changed=[fn for fn in affected if wanted[fn]!=self.assigned(fn)]   # the same set is a no-op (plan §2.1)
        for fn in sorted(changed):                                 # a running track's set does not change (capture §3.2)
            if self.tracks[fn].state in (2,3,5):raise self.refusal(6,fn)
        self.release_plans(changed)
        for fn in changed:
            self.tracks[fn].roles=[role for role,ch in wanted[fn]];self.plans[fn]=[ch for role,ch in wanted[fn]]
        return b''
    def state(self,gid=4):
        g=self.groups[gid]
        if not g.bound:return 0
        states=[self.tracks[fn].state for fn in g.bound]
        if 6 in states:return 6
        if g.start_ns!=NO_TIME and all(s==4 for s in states):return 4
        if any(s in (2,3,5) for s in states):return 3
        return 1
    def bind(self,payload,tags,gid=4):
        g=self.groups[gid];count=payload[0];functions=list(struct.unpack('<'+'H'*count,payload[1:]))
        if len(set(functions))!=len(functions):raise Reject(3)
        for fn in functions:
            if fn not in self.tracks:raise Reject(11,b'\0'+tlv(5,struct.pack('<H',fn)))
        if 18+6*count>self.ep.probe.max_frame:raise Reject(11,b'\0')   # the start answer must fit this route
        if functions==g.bound:return b''
        if self.state(gid)==3:raise self.refusal(6)
        if not count:self.unbind(gid);return b''
        for fn in functions:
            other=self.owner(fn)
            if other is not None and other!=gid:raise self.refusal(4,fn)
        for fn in functions:
            t=self.tracks[fn]
            if t.config is None or t.state not in (1,4,6) or t.config['trigger'][0]:raise self.refusal(6,fn)
        first=self.tracks[functions[0]].config
        for fn in functions:
            if self.tracks[fn].config['mode']!=first['mode']:raise self.refusal(6,fn)
        for fn in functions:
            # Explicit equal-rate acquisition resource; does not advertise every combination.
            if self.requires_equal_rate and self.tracks[fn].config['rate']!=first['rate']:raise self.refusal(2,fn)
        g.bound=functions;return b''
    def start(self,gid=4):
        g=self.groups[gid]
        if not g.bound:raise self.refusal(6)
        for fn in g.bound:
            if self.tracks[fn].state not in (1,4,6) or self.tracks[fn].config is None:raise self.refusal(6,fn)
        if g.generation==LAST:raise self.refusal(2)
        for fn in g.bound:
            if self.tracks[fn].generation==LAST:raise self.refusal(2,fn)
        g.start_ns=self.ep.now_ns();g.generation+=1
        for fn in g.bound:self.tracks[fn].dispatch_request(2,17,b'',{})
        return struct.pack('<QIB',g.start_ns,g.generation,len(g.bound))+b''.join(struct.pack('<HI',fn,self.tracks[fn].generation) for fn in g.bound)
    def stop(self,gid=4):
        for member in self.groups[gid].bound:self.tracks[member].dispatch_request(2,18,b'',{})
        return b''
    def status(self,gid=4):
        g=self.groups[gid];return struct.pack('<BQI',self.state(gid),g.start_ns,g.generation)
    def dispatch_request(self,fn,op,payload,tags):
        if fn==1:return self.plan(op,payload,tags)
        if fn in self.tracks:
            if self.owner(fn) is not None and op in (16,17,18,19):raise self.refusal(4,fn)
            t=self.tracks[fn]
            if op==19:
                if t.state==2:
                    t.state=3;t.current=list(t.history);t.trigger_index=len(t.current);t.history=[]
                return b''
            if op==24 and t.state in (2,3,5):
                before=t.state;t.state=1
                try:return t.dispatch_request(2,op,payload,tags)
                finally:t.state=before
            return t.dispatch_request(2,op,payload,tags)
        if op==16:return self.bind(payload,tags,fn)
        if op==17:return self.start(fn)
        if op==18:return self.stop(fn)
        return self.status(fn)
    def feed(self,rows):
        accepted=0
        for row in rows:
            active=[fn for fn in row if self.tracks[fn].state in (2,3)]
            if not active:break
            for fn in active:self.tracks[fn].feed([row[fn]])
            accepted+=1
            rate=self.tracks[active[0]].config['rate'];self.ep.group_sample_ns+=Fraction(1000000000,rate)
            self.ep.group_ms=int(self.ep.group_sample_ns//1000000)
        return accepted


class CaptureGroupModel(LogicModel):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs);self.ep.probe.max_frame=256
        self.ep.static[0]=tuple(row for row in self.ep.static[0] if row[0]!=67)+(tlv(67,struct.pack('<H',16)),)   # channels 0..15
    def install(self):
        self.ep.group_ms=self.milliseconds;self.ep.group_sample_ns=Fraction(self.milliseconds*1000000);self.ep.now=lambda:self.ep.group_ms
        self.ep.probe.max_frame=256;self.extension=CaptureGroup(self.ep);self.ep.current_core=Core(self.ep,self.extension)
    def handle(self,request):
        self.milliseconds=self.ep.group_ms;e=self.extension;fixed,free=e.fixed,e.free
        if len(request)>=6:
            fn,op=struct.unpack_from('<HB',request,3);n=request[10] if len(request)>10 else 0
            if fn==1:e.fixed={16:0,17:1+2*n};e.free=set()
            elif fn in GROUPS:e.fixed={16:1+2*n,17:0,18:0,20:0};e.free={20}
            else:e.fixed={16:0,17:0,18:0,19:0,20:0,21:14,22:4,23:8,24:0,**({25:0} if fn==2 else {})};e.free={20,21,22,24,25}
        try:return self.ep.handle(request)
        finally:e.fixed,e.free=fixed,free
    def group_state(self):
        e=self.extension
        return copy.deepcopy(dict(bound=e.bound,generation=e.generation,start_ns=e.start_ns,groups={gid:dict(bound=g.bound,generation=g.generation,start_ns=g.start_ns) for gid,g in e.groups.items()},
          tracks={fn:dict(state=t.state,generation=t.generation,config=t.config,roles=t.roles,data=bytes(t.data),base=t.base,segments=t.segments,current=t.current,history=t.history,flags=t.flags,error=t.error) for fn,t in e.tracks.items()},
          plans=e.plans,pins=e.pins,idle=e.idle,now_ns=self.ep.now_ns(),max_frame=self.ep.probe.max_frame,clock_scope='explicit-logical-ms'))
    def group_feed(self,rows):
        if type(rows)is not list or len(rows)>128:raise ValueError('finite explicit paired rows required')
        e=self.extension
        for row in rows:
            if type(row)is not dict or not row or set(row)-set(e.tracks) or (e.bound and set(row)!=set(e.bound)):raise ValueError('explicit selected track rows required')
            for fn,values in row.items():
                if type(values)is not list or len(values)!=len(e.tracks[fn].roles) or any(type(v)is not int or not 0<=v<=(4095 if fn==2 else 1) for v in values):raise ValueError('raw samples in increasing role order required')
        count=e.feed(rows);self.milliseconds=self.ep.group_ms;return count
    def group_advance(self,ms):
        if type(ms)is not int or not 0<=ms<=60000:raise ValueError('explicit logical ms required')
        self.ep.group_ms+=ms;self.ep.group_sample_ns+=ms*1000000;self.milliseconds=self.ep.group_ms
    def group_fail(self,fn,error):
        e=self.extension
        if fn not in e.tracks or type(error)is not int or error not in (1,2,3) or e.tracks[fn].state not in (2,3):raise ValueError('active track and defined error required')
        t=e.tracks[fn];t.current=[];t.history=[];t.state=6;t.flags|=1;t.error=error
        gid=e.owner(fn)
        if gid is not None:
            for other in e.groups[gid].bound:
                if other!=fn:e.tracks[other].dispatch_request(2,18,b'',{})
    def group_seed(self,generation,adc,logic):
        if self.ep.current_core.holder is not None or any(g.bound for g in self.extension.groups.values()) or any(type(v)is not int or not 0<=v<=0xffffffff for v in (generation,adc,logic)):raise ValueError('unowned explicit group/track u32 seeds required')
        e=self.extension;e.generation=generation;e.tracks[2].generation=adc;e.tracks[3].generation=logic
    def group_invalidate(self,fn):
        if fn not in self.extension.tracks:raise ValueError('explicit track required')
        t=self.extension.tracks[fn];t.config=None;t.clear(0)
    def group_frame(self,limit):
        if self.ep.current_core.holder is not None or type(limit)is not int or not 64<=limit<=256:raise ValueError('unowned explicit 64..256 route frame required')
        self.ep.probe.max_frame=limit
