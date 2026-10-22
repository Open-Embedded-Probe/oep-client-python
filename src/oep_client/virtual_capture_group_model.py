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
from . import registry as _reg
from .catalog import pack_ops
from .virtual_names import R, DC, cause, UNAVAILABLE_TLV

# the capture family's numbers by name (capture §2-§4)
_L, _A, _G = _reg.FIXTURE_LOGIC, _reg.FIXTURE_ANALOG, _reg.FIXTURE_CAPTURE_GROUP
OP, GOP, PLAN = _A.op, _G.op, _reg.PROBE_PLAN.op
D, ST, GS, TRIG = _A.tlv["describe"], _L.enum["state"], _G.enum["state"], _L.enum["trigger"]
RUNNING = (ST["waiting"], ST["capturing"], ST["paused"])
STARTABLE = (ST["configured"], ST["done"], ST["error"])
ADC, LOGIC = 2, 3                                       # this fixture's track fns (list order)

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
        super().__init__();self.ep=ep;self.tracks={ADC:Analog(ep),LOGIC:Logic(ep),**{fn:Logic(ep) for fn in EXTRA}}
        self.idle.update({ch:_reg.CORE.enum['channel_idle']['hi_z'] for ch in (0,1,*range(8,16))});self.pins=dict(self.idle);self.external.update({ch:ch%2 for ch in range(8,16)})   # the analog's 0, 1: no output idle (capture §1.2)
        self.groups={fn:Group() for fn in GROUPS}
    # fn 4 is the group the checker drives; these keep its fields directly reachable.
    bound=property(lambda self:self.groups[4].bound,lambda self,value:setattr(self.groups[4],'bound',value))
    generation=property(lambda self:self.groups[4].generation,lambda self,value:setattr(self.groups[4],'generation',value))
    start_ns=property(lambda self:self.groups[4].start_ns,lambda self,value:setattr(self.groups[4],'start_ns',value))
    def names(self):
        return [(fn,b'oep.probe.plan' if fn==1 else b'oep.fixture.analog' if fn==ADC else b'oep.fixture.capture-group' if fn in GROUPS else b'oep.fixture.logic') for fn in self.functions]
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
        if fn==1:return (tlv(DC['ops'],pack_ops(set(PLAN.values()))),tlv(_reg.PROBE_PLAN.tlv['describe']['plan_roles'],struct.pack('<I',16)))
        if fn in GROUPS:return (tlv(DC['ops'],pack_ops({GOP[k] for k in ('bind','start','stop','status')})),tlv(_G.tlv['describe']['tracks'],bytes((len(self.tracks),))+b''.join(struct.pack('<H',t) for t in sorted(self.tracks))))
        rows=list(self.tracks[fn].describe(2))
        track_ops={OP[k] for k in ('configure','start','stop','force','status','read','segments','release','query')}|({OP['calibration']} if fn==ADC else set())
        rows=[tlv(DC['ops'],pack_ops(track_ops)) if r[0]==DC['ops'] else r for r in rows]
        if fn==LOGIC:rows=[tlv(DC['role_channels'],bytes((r[3],0,0,12))) if r[0]==DC['role_channels'] else r for r in rows if r[0]!=DC['role_channels'] or r[3] in (0,1)]
        if fn in EXTRA:rows=[tlv(DC['role_channels'],struct.pack('<BHH',0,0,1<<(fn-2))) if r[0]==DC['role_channels'] else r for r in rows if r[0]!=DC['role_channels'] or r[3]==0]
        if fn==LOGIC:rows=[tlv(D['channels'],b'\2') if r[0]==D['channels'] else r for r in rows]
        if fn in EXTRA:rows=[tlv(D['channels'],b'\1') if r[0]==D['channels'] else r for r in rows]
        return tuple(rows)
    def request_tags(self,fn,op,data):
        if fn==1:return super().request_tags(fn,op,data)
        if fn in self.tracks:return self.tracks[fn].request_tags(2,op,data)
        while data:
            if len(data)<3:raise Reject(R['malformed'])
            raw,size=struct.unpack_from('<BH',data);key=raw&127
            if not key or len(data)<3+size:raise Reject(R['malformed'])
            data=data[3+size:]
            if raw&128:raise Reject(R['unsupported'],bytes((raw,)))   # v1 defines no group request TLV: every copy (core §2.3)
        return {}
    def owner(self,fn):return next((gid for gid,g in self.groups.items() if fn in g.bound),None)
    def unbind(self,gid=4):self.groups[gid].bound=[]
    def release(self):
        for gid in self.groups:self.unbind(gid)
        self.release_plans(list(self.plans))
    def release_plans(self,functions):
        for fn in functions:
            if fn in self.tracks:
                t=self.tracks[fn];t.roles=[];t.config=None;t.clear(ST['unconfigured'])
        super().release_plans(functions)
    @staticmethod
    def refusal(name,fn=None):return Reject(R['unavailable'],cause(name)+(tlv(UNAVAILABLE_TLV['fn'],struct.pack('<H',fn)) if fn is not None else b''))
    def assigned(self,fn):return sorted(zip(self.tracks[fn].roles,self.plans.get(fn,[]))) if fn in self.tracks else []
    def plan(self,op,payload,tags):
        if op==PLAN['plan_release']:
            count=payload[0];functions=list(struct.unpack('<'+'H'*count,payload[1:])) or list(self.plans)
            if any(fn and fn not in self.functions for fn in functions):raise Reject(R['unknown_function'])   # every fn first; fn 0 has no plan (plan §2.2)
            for fn in functions:
                if self.owner(fn) is not None:raise self.refusal('bound_in_group',fn)
            self.release_plans(functions);return b''
        rows=[(raw,*struct.unpack('<HBH',value)) for raw,value in tags]
        if len(set((fn,role,ch) for _,fn,role,ch in rows))!=len(rows):raise Reject(R['malformed'])
        affected={r[1] for r in rows}
        for raw,fn,role,ch in rows:
            if fn==0:raise Reject(R['malformed'])
            if fn not in self.functions:raise Reject(R['unknown_function'])
            roles,channels=((1,3),(0,1)) if fn==ADC else ((0,1),(2,3)) if fn==LOGIC else ((0,),(fn-2,))
            if fn not in self.tracks or role not in roles or ch not in channels:raise Reject(R['unsupported'],bytes((raw,)))
        if len({(r[1],r[2]) for r in rows})!=len(rows) or len({r[3] for r in rows})!=len(rows):raise Reject(R['unsupported'],bytes((rows[0][0],)))   # one channel per role: unsupported (plan §2.4)
        refuse_output_idle(self.idle,(r[3] for r in rows if r[1]==ADC))   # the analog track (capture §1.2)
        wanted={fn:sorted((role,ch) for _,owner,role,ch in rows if owner==fn) for fn in affected}
        for fn in sorted(affected):
            if self.owner(fn) is not None and wanted[fn]!=self.assigned(fn):raise self.refusal('bound_in_group',fn)
        changed=[fn for fn in affected if wanted[fn]!=self.assigned(fn)]   # the same set is a no-op (plan §2.1)
        for fn in sorted(changed):                                 # a running track's set does not change (capture §3.2)
            if self.tracks[fn].state in RUNNING:raise self.refusal('wrong_state',fn)
        self.release_plans(changed)
        for fn in changed:
            self.tracks[fn].roles=[role for role,ch in wanted[fn]];self.plans[fn]=[ch for role,ch in wanted[fn]]
        return b''
    def state(self,gid=4):
        g=self.groups[gid]
        if not g.bound:return GS['unconfigured']
        states=[self.tracks[fn].state for fn in g.bound]
        if ST['error'] in states:return GS['error']
        if g.start_ns!=NO_TIME and all(s==ST['done'] for s in states):return GS['done']
        if any(s in RUNNING for s in states):return GS['capturing']
        return GS['configured']
    def bind(self,payload,tags,gid=4):
        g=self.groups[gid];count=payload[0];functions=list(struct.unpack('<'+'H'*count,payload[1:]))
        if len(set(functions))!=len(functions):raise Reject(R['malformed'])
        for fn in functions:
            if fn not in self.tracks:raise Reject(R['unsupported'],b'\0'+tlv(UNAVAILABLE_TLV['fn'],struct.pack('<H',fn)))
        if 18+6*count>self.ep.probe.max_frame:raise Reject(R['unsupported'],b'\0')   # the start answer must fit this route
        if functions==g.bound:return b''
        if self.state(gid)==GS['capturing']:raise self.refusal('wrong_state')
        if not count:self.unbind(gid);return b''
        for fn in functions:
            other=self.owner(fn)
            if other is not None and other!=gid:raise self.refusal('bound_in_group',fn)
        for fn in functions:
            t=self.tracks[fn]
            if t.config is None or t.state not in STARTABLE or t.config['trigger'][0]!=TRIG['immediate']:raise self.refusal('wrong_state',fn)
        first=self.tracks[functions[0]].config
        for fn in functions:
            if self.tracks[fn].config['mode']!=first['mode']:raise self.refusal('wrong_state',fn)
        for fn in functions:
            # Explicit equal-rate acquisition resource; does not advertise every combination.
            if self.requires_equal_rate and self.tracks[fn].config['rate']!=first['rate']:raise self.refusal('limit',fn)
        g.bound=functions;return b''
    def start(self,gid=4):
        g=self.groups[gid]
        if not g.bound:raise self.refusal('wrong_state')
        for fn in g.bound:
            if self.tracks[fn].state not in STARTABLE or self.tracks[fn].config is None:raise self.refusal('wrong_state',fn)
        if g.generation==LAST:raise self.refusal('limit')
        for fn in g.bound:
            if self.tracks[fn].generation==LAST:raise self.refusal('limit',fn)
        g.start_ns=self.ep.now_ns();g.generation+=1
        for fn in g.bound:self.tracks[fn].dispatch_request(2,OP['start'],b'',{})
        return struct.pack('<QIB',g.start_ns,g.generation,len(g.bound))+b''.join(struct.pack('<HI',fn,self.tracks[fn].generation) for fn in g.bound)
    def stop(self,gid=4):
        for member in self.groups[gid].bound:self.tracks[member].dispatch_request(2,OP['stop'],b'',{})
        return b''
    def status(self,gid=4):
        g=self.groups[gid];return struct.pack('<BQI',self.state(gid),g.start_ns,g.generation)
    def dispatch_request(self,fn,op,payload,tags):
        if fn==1:return self.plan(op,payload,tags)
        if fn in self.tracks:
            if self.owner(fn) is not None and op in (OP['configure'],OP['start'],OP['stop'],OP['force']):raise self.refusal('bound_in_group',fn)
            t=self.tracks[fn]
            if op==OP['force']:
                if t.state==ST['waiting']:
                    t.state=ST['capturing'];t.current=list(t.history);t.trigger_index=len(t.current);t.history=[]
                return b''
            if op==OP['query'] and t.state in RUNNING:
                before=t.state;t.state=ST['configured']
                try:return t.dispatch_request(2,op,payload,tags)
                finally:t.state=before
            return t.dispatch_request(2,op,payload,tags)
        if op==GOP['bind']:return self.bind(payload,tags,fn)
        if op==GOP['start']:return self.start(fn)
        if op==GOP['stop']:return self.stop(fn)
        return self.status(fn)
    def feed(self,rows):
        accepted=0
        for row in rows:
            active=[fn for fn in row if self.tracks[fn].state in (ST['waiting'],ST['capturing'])]
            if not active:break
            for fn in active:self.tracks[fn].feed([row[fn]])
            accepted+=1
            rate=self.tracks[active[0]].config['rate'];self.ep.group_sample_ns+=Fraction(1000000000,rate)
            self.ep.group_ms=int(self.ep.group_sample_ns//1000000)
        return accepted


class CaptureGroupModel(LogicModel):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs);self.ep.probe.max_frame=256
        CH=_reg.CORE.tlv['describe']['channels']
        self.ep.static[0]=tuple(row for row in self.ep.static[0] if row[0]!=CH)+(tlv(CH,struct.pack('<H',16)),)   # channels 0..15
    def install(self):
        self.ep.group_ms=self.milliseconds;self.ep.group_sample_ns=Fraction(self.milliseconds*1000000);self.ep.now=lambda:self.ep.group_ms
        self.ep.probe.max_frame=256;self.extension=CaptureGroup(self.ep);self.ep.current_core=Core(self.ep,self.extension)
    def handle(self,request):
        self.milliseconds=self.ep.group_ms;e=self.extension;fixed,free=e.fixed,e.free
        if len(request)>=6:
            fn,op=struct.unpack_from('<HB',request,3);n=request[10] if len(request)>10 else 0
            if fn==1:e.fixed={PLAN['plan_apply']:0,PLAN['plan_release']:1+2*n};e.free=set()
            elif fn in GROUPS:e.fixed={GOP['bind']:1+2*n,GOP['start']:0,GOP['stop']:0,GOP['status']:0};e.free={GOP['status']}
            else:e.fixed={OP['configure']:0,OP['start']:0,OP['stop']:0,OP['force']:0,OP['status']:0,OP['read']:14,OP['segments']:4,OP['release']:8,OP['query']:0,**({OP['calibration']:0} if fn==ADC else {})};e.free={OP['status'],OP['read'],OP['segments'],OP['query'],OP['calibration']}
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
                if type(values)is not list or len(values)!=len(e.tracks[fn].roles) or any(type(v)is not int or not 0<=v<=(4095 if fn==ADC else 1) for v in values):raise ValueError('raw samples in increasing role order required')
        count=e.feed(rows);self.milliseconds=self.ep.group_ms;return count
    def group_advance(self,ms):
        if type(ms)is not int or not 0<=ms<=60000:raise ValueError('explicit logical ms required')
        self.ep.group_ms+=ms;self.ep.group_sample_ns+=ms*1000000;self.milliseconds=self.ep.group_ms
    def group_fail(self,fn,error):
        e=self.extension
        if fn not in e.tracks or type(error)is not int or error not in _L.enum['error'].values() or e.tracks[fn].state not in (ST['waiting'],ST['capturing']):raise ValueError('active track and defined error required')
        t=e.tracks[fn];t.current=[];t.history=[];t.state=ST['error'];t.flags|=_L.enum['status_flag']['dropped'];t.error=error
        gid=e.owner(fn)
        if gid is not None:
            for other in e.groups[gid].bound:
                if other!=fn:e.tracks[other].dispatch_request(2,OP['stop'],b'',{})
    def group_seed(self,generation,adc,logic):
        if self.ep.current_core.holder is not None or any(g.bound for g in self.extension.groups.values()) or any(type(v)is not int or not 0<=v<=0xffffffff for v in (generation,adc,logic)):raise ValueError('unowned explicit group/track u32 seeds required')
        e=self.extension;e.generation=generation;e.tracks[ADC].generation=adc;e.tracks[LOGIC].generation=logic
    def group_invalidate(self,fn):
        if fn not in self.extension.tracks:raise ValueError('explicit track required')
        t=self.extension.tracks[fn];t.config=None;t.clear(ST['unconfigured'])
    def group_frame(self,limit):
        if self.ep.current_core.holder is not None or type(limit)is not int or not 64<=limit<=256:raise ValueError('unowned explicit 64..256 route frame required')
        self.ep.probe.max_frame=limit
