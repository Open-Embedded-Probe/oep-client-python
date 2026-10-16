"""Independent ADC fixture using explicit raw rows; no physical sampling claims.

Shares the finite capture lifecycle of the independent logic model, not the
legacy virtual bench sampler or Host capture decoder.
"""
import struct

from fractions import Fraction
from .virtual_logic_model import Logic, LogicModel
from .virtual_core import Core, Reject, tlv
from . import registry as _reg
from .catalog import pack_ops
from .virtual_names import R, DC, cause

# oep.fixture.analog's numbers by name (capture §3)
_A = _reg.FIXTURE_ANALOG
OP, C, A, D, CAL = _A.op, _A.tlv["configure"], _A.tlv["configure_answer"], _A.tlv["describe"], _A.tlv["calibration_answer"]
MODE, TRIG, ST, REF = _A.enum["mode"], _A.enum["trigger"], _A.enum["state"], _A.enum["reference_source"]
PLAN = _reg.PROBE_PLAN.op
SIZES = {C["mode"]: 1, C["trigger"]: 6, C["frontend"]: 2}      # the other configure tags are u32
RUNNING = (ST["waiting"], ST["capturing"], ST["paused"])
IDLE = _reg.PROBE_CONFIG.enum["idle_mode"]


def refuse_output_idle(idle,channels):
    """An analog plan onto a channel whose idle is an output (mode 3 / 4): rejected unavailable cause 5 naming the
    channel, before anything changes (capture §1.2, plan §2.4)."""
    for ch in channels:
        if idle.get(ch) in (IDLE['output_low'],IDLE['output_high']):raise Reject(R['unavailable'],cause('held_by_settings',ch))


class Analog(Logic):
    def __init__(self,ep):
        super().__init__(ep);self.calibration_ns=None;self.factory_raw=b'\x12\x34'
        self.idle={ch:IDLE['hi_z'] for ch in range(8)};self.pins=dict(self.idle)   # the analog channels' idle is no output (capture §1.2)
    def list_page(self,first,limit):
        rows=[struct.pack('<HHBB',fn,0,1,len(name))+name for fn,name in
              ((1,b'oep.probe.plan'),(2,b'oep.fixture.analog'))]
        page=bytearray(struct.pack('<HB',2,0))
        for row in rows[first:]:
            if len(page)+len(row)>limit:break
            page.extend(row);page[2]+=1
        return bytes(page)
    def describe(self,fn):
        if fn==1:return super().describe(fn)
        ops=pack_ops({OP[k] for k in ('configure','start','stop','status','read','segments','release','query','calibration')})   # no force
        return (tlv(DC['ops'],ops),*(tlv(DC['role_channels'],bytes((role,0,0,3))) for role in (1,3)),
                tlv(DC['features'],b'\0'*4),tlv(D['mode'],struct.pack('<BII',MODE['one_shot'],16,0)),tlv(D['mode'],struct.pack('<BII',MODE['repeat'],16,2)),
                tlv(D['rate_range'],struct.pack('<II',100,1000)),tlv(D['channels'],b'\2'),
                tlv(D['trigger'],struct.pack('<II',(1<<TRIG['immediate'])|(1<<TRIG['cross_up'])|(1<<TRIG['cross_down']),8)),
                tlv(D['frontend'],struct.pack('<Bii',0,0,3300)),tlv(D['frontend'],struct.pack('<Bii',1,-1000,1000)))
    def plan(self,op,payload,tags):
        if op==PLAN['plan_release']:return super().plan(op,payload,tags)
        rows=[(raw,*struct.unpack('<HBH',v)) for raw,v in tags]
        if len(set((fn,role,ch) for _,fn,role,ch in rows))!=len(rows):raise Reject(R['malformed'])
        for raw,fn,role,ch in rows:
            if fn==0:raise Reject(R['malformed'])
            if fn not in self.functions:raise Reject(R['unknown_function'])
            if fn!=2 or role not in (1,3) or ch not in (0,1):raise Reject(R['unsupported'],bytes((raw,)))
        if not rows:return b''
        if len({r[2] for r in rows})!=len(rows) or len({r[3] for r in rows})!=len(rows):raise Reject(R['unsupported'],bytes((rows[0][0],)))   # one channel per role: unsupported (plan §2.4)
        refuse_output_idle(self.idle,(r[3] for r in rows))
        if sorted((r[2],r[3]) for r in rows)==sorted(zip(self.roles,self.plans.get(2,[]))):return b''   # the same set: nothing (plan §2.1)
        if self.state in RUNNING:raise Reject(R['unavailable'],cause('wrong_state'))   # running: a change is cause 6 (capture §3.2)
        self.release_plans([2]);rows.sort(key=lambda r:r[2]);self.roles=[r[2] for r in rows];self.plans[2]=[r[3] for r in rows]
        return b''
    def request_tags(self,fn,op,data):
        if fn==1:return super().request_tags(fn,op,data)
        got={};seen=set();frontends=[];later=[]
        while data:
            if len(data)<3:raise Reject(R['malformed'])
            raw,size=struct.unpack_from('<BH',data);key=raw&127
            if not key or len(data)<3+size:raise Reject(R['malformed'])
            value,data=data[3:3+size],data[3+size:]
            known=op in (OP['configure'],OP['query']) and key in C.values()
            if known and size!=SIZES.get(key,4):raise Reject(R['malformed'])   # a later copy too (core §2.3)
            if not known:                                  # every copy of an unknown tag: critical -> unsupported (core §2.3)
                if raw&128:raise Reject(R['unsupported'],bytes((raw,)))
                continue
            if key in seen and key!=C['frontend']:later.append((key,raw,value));continue   # a later copy: checked in settings (core §2.3)
            seen.add(key)
            if key==C['frontend']:frontends.append((raw,value))
            else:got[key]=(raw,value)
        got[C['frontend']]=frontends;got['later']=later
        return got
    def settings(self,tags):
        mode_t,rate_t,samples_t,segments_t,trigger_t,pre_t,fe_t=(C[k] for k in ('mode','rate','samples','segments','trigger','pretrigger','frontend'))
        u32=lambda v:struct.unpack('<I',v)[0]
        unsupported=lambda raw:Reject(R['unsupported'],bytes((raw,)))
        if not self.roles:raise Reject(R['unavailable'],cause('wrong_state'))
        if not all(k in tags for k in (mode_t,rate_t)):raise Reject(R['malformed'])
        mode=tags[mode_t][1][0];rate=u32(tags[rate_t][1])
        if rate==0 or any(k in tags and u32(tags[k][1])==0 for k in (samples_t,segments_t)):raise Reject(R['malformed'])
        if mode in (MODE['one_shot'],MODE['repeat']) and samples_t not in tags:raise Reject(R['malformed'])
        later=tags.get('later',[])                         # every later copy is checked too, the first used (core §2.3)
        if any(k in (rate_t,samples_t,segments_t) and not u32(v) for k,_,v in later):raise Reject(R['malformed'])
        for key in (samples_t,segments_t,pre_t):
            if key in tags and ((key==samples_t and mode==MODE['streaming']) or (key==segments_t and mode!=MODE['repeat'])
                                or (key==pre_t and (trigger_t not in tags or tags[trigger_t][1][0]==TRIG['immediate']))):raise unsupported(tags[key][0])
        if mode not in (MODE['one_shot'],MODE['repeat']):raise unsupported(tags[mode_t][0])
        if not 100<=rate<=1000:raise unsupported(tags[rate_t][0])
        bad_trigger=lambda t:t[0] not in TRIG.values() or (t[0]!=TRIG['immediate'] and t[2]>4095)
        trigger=struct.unpack('<BBI',tags[trigger_t][1]) if trigger_t in tags else (TRIG['immediate'],0,0)
        if bad_trigger(trigger):raise unsupported(tags[trigger_t][0])
        for k,raw,v in later:                              # a value this probe does not handle: that copy's tag as received
            if (k==mode_t and v[0] not in (MODE['one_shot'],MODE['repeat']) or k==rate_t and not 100<=u32(v)<=1000 or k==pre_t and u32(v)>8
                    or k==trigger_t and bad_trigger(struct.unpack('<BBI',v))):raise unsupported(raw)
        if trigger[0]!=TRIG['immediate'] and trigger[1] not in self.roles:raise Reject(R['unavailable'],cause('wrong_state'))
        samples=min(16,u32(tags[samples_t][1]));segments=min(2,u32(tags[segments_t][1])) if segments_t in tags else 2 if mode==MODE['repeat'] else 1
        pre=u32(tags[pre_t][1]) if pre_t in tags else 0
        if pre>8 or pre>=samples:raise unsupported(tags[pre_t][0])
        frontend={role:0 for role in self.roles};seen=set()
        for raw,value in tags[fe_t]:
            role,fe=value
            if role in seen:raise Reject(R['malformed'])
            seen.add(role)
            if role not in self.roles:raise unsupported(raw)   # capture §3.3: an unlisted refusal is unsupported
            if fe not in (0,1):raise unsupported(raw)
            frontend[role]=fe
        return dict(mode=mode,rate=rate,samples=samples,segments=segments,trigger=trigger,pretrigger=pre,frontend=frontend)
    def answer(self,c):
        rows=[tlv(A['actual_rate'],struct.pack('<II',c['rate'],1)),tlv(A['layout'],bytes((16,2,12,len(self.roles),*reversed(self.roles)))),
              tlv(A['actual_samples'],struct.pack('<I',c['samples'])),tlv(A['reference'],struct.pack('<BIB',REF['external'],1100,_A.enum['reference_how']['nominal']))]
        if c['mode']==MODE['repeat']:rows.append(tlv(A['actual_segments'],struct.pack('<I',c['segments'])))
        for role in self.roles:rows.extend((tlv(A['scale'],struct.pack('<Bii',role,-17,-1000000 if c['frontend'][role] else 1000000)),
                                           tlv(A['skew'],struct.pack('<BI',role,0)),tlv(A['frontend_used'],bytes((role,c['frontend'][role])))))
        return b''.join(rows)
    def finish(self,short=False):
        if not self.current:return
        data=b''.join(struct.pack('<H',(row[self.roles.index(role)]<<2)|0xc003) for row in self.current for role in reversed(self.roles))
        next_ns=self.start_ns+len(self.current)*1000000000//self.config['rate']
        position=self.base+len(self.data);flags=int(self.gap)|(2 if short else 0);self.data.extend(data)
        self.segments.append((self.serial,position,len(self.current),self.start_ns,1000000,self.trigger_index,flags,self.generation))
        self.serial=(self.serial+1)&0xffffffff;self.current=[];self.gap=False;self.trigger_index=0xffffffff;self.start_ns=next_ns
        if self.config['mode']==MODE['one_shot']:self.state=ST['done']
        elif len(self.segments)>=self.config['segments']:self.state=ST['paused']
    def dispatch_request(self,fn,op,payload,tags):
        if fn==2 and op==OP['configure'] and self.state in RUNNING:raise Reject(R['unavailable'],cause('wrong_state'))
        if fn==2 and op==OP['calibration']:
            scheme=b'org.example.adc';out=tlv(CAL['factory'],bytes((255,len(scheme)))+scheme+struct.pack('<H',len(self.factory_raw))+self.factory_raw)
            if self.calibration_ns is not None:out+=tlv(CAL['vrefint'],struct.pack('<IQI',1365,self.calibration_ns,1100))
            if 5+len(out)>self.ep.probe.max_frame:raise Reject(R['unsupported'],b'\0')
            return out
        out=super().dispatch_request(fn,op,payload,tags)
        if fn==2 and op==OP['start']:self.calibration_ns=self.ep.now_ns()
        return out
    def feed(self,samples):
        accepted=0
        for sample in samples:
            if self.state not in (ST['waiting'],ST['capturing']):break
            accepted+=1
            if self.state==ST['waiting']:
                kind,role,value=self.config['trigger'];raw=sample[self.roles.index(role)];previous=self.previous
                hit=previous is not None and ((kind==TRIG['cross_up'] and previous<value<=raw) or (kind==TRIG['cross_down'] and previous>value>=raw))
                self.previous=raw
                if not hit:
                    self.history.append(sample);self.history=self.history[-self.config['pretrigger']:] if self.config['pretrigger'] else []
                    continue
                self.state=ST['capturing'];self.current=list(self.history);self.trigger_index=len(self.current)
                self.start_ns=max(0,self.ep.now_ns()-self.trigger_index*1000000000//self.config['rate']);self.history=[]
            self.current.append(sample);target=self.config['samples']
            if self.trigger_index!=0xffffffff:target-=self.config['pretrigger']-self.trigger_index
            if len(self.current)>=target:self.finish()
        return accepted


class AnalogModel(LogicModel):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs);self.ep.probe.max_frame=256
    def install(self):self.extension=Analog(self.ep);self.ep.current_core=Core(self.ep,self.extension)
    def handle(self,request):
        e=self.extension;fixed,free=e.fixed,e.free
        if len(request)>=6:
            fn,op=struct.unpack_from('<HB',request,3);n=request[10] if len(request)>10 else 0
            if fn==1:e.fixed={PLAN['plan_apply']:0,PLAN['plan_release']:1+2*n};e.free=set()
            else:e.fixed={OP['configure']:0,OP['start']:0,OP['stop']:0,OP['status']:0,OP['read']:14,OP['segments']:4,OP['release']:8,OP['query']:0,OP['calibration']:0};e.free={OP['status'],OP['read'],OP['segments'],OP['query'],OP['calibration']}
        try:return self.ep.handle(request)
        finally:e.fixed,e.free=fixed,free
    def analog_state(self):return dict(self.logic_state(),calibration_ns=self.extension.calibration_ns,factory_raw=self.extension.factory_raw,now_ns=self.ep.now_ns())
    def analog_factory(self,size):
        if type(size)is not int or not 0<=size<=4096:raise ValueError('explicit 0..4096 factory raw byte count required')
        self.extension.factory_raw=bytes(i*7&255 for i in range(size));return self.extension.factory_raw
    def analog_feed(self,samples):
        if type(samples)is not list or len(samples)>128 or any(type(row)is not list or len(row)!=len(self.extension.roles) or any(type(value)is not int or not 0<=value<=4095 for value in row) for row in samples):raise ValueError('explicit finite u12 raw samples in increasing plan role order required')
        accepted=0
        for sample in samples:
            count=self.extension.feed([sample]);accepted+=count
            if not count:break
            self.sample_ns+=Fraction(1000000000,self.extension.config['rate']);self.milliseconds=int(self.sample_ns//1000000)
        return accepted
    def analog_advance(self,ms):return self.logic_advance(ms)
    def analog_fail(self,error):return self.logic_fail(error)
    def analog_seed(self,generation):return self.logic_seed(generation)
