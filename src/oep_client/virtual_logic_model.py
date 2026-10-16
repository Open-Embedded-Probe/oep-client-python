"""Independent logic capture with explicit samples and no acquisition timing claims."""
import copy
import struct

from fractions import Fraction
from .virtual_gpio_model import Gpio, GpioModel
from .virtual_core import Core, Reject, tlv
from . import registry as _reg
from .virtual_names import R, DC, cause
from .catalog import pack_ops

# oep.fixture.logic's numbers by name (capture §3): ops, configure / answer / describe tags, modes, states
_L = _reg.FIXTURE_LOGIC
OP, C, A, D = _L.op, _L.tlv["configure"], _L.tlv["configure_answer"], _L.tlv["describe"]
MODE, TRIG, ST = _L.enum["mode"], _L.enum["trigger"], _L.enum["state"]
PLAN = _reg.PROBE_PLAN.op
SIZES = {C["mode"]: 1, C["trigger"]: 6}                 # the other configure tags are u32
RUNNING = (ST["waiting"], ST["capturing"], ST["paused"])


class Logic(Gpio):
    functions=(1,2)
    def __init__(self,ep):
        super().__init__();self.ep=ep;self.generation=0;self.roles=[];self.config=None;self.clear(0)
    def clear(self,state):
        self.state=state;self.data=bytearray();self.base=0;self.segments=[];self.serial=0;self.flags=0;self.error=0;self.current=[];self.history=[];self.previous=None;self.gap=False;self.trigger_index=0xffffffff;self.start_ns=0
    def list_page(self,first,limit):
        rows=[struct.pack('<HHBB',fn,0,1,len(name))+name for fn,name in ((1,b'oep.probe.plan'),(2,b'oep.fixture.logic'))];page=bytearray(struct.pack('<HB',2,0))
        for row in rows[first:]:
            if len(page)+len(row)>limit:break
            page.extend(row);page[2]+=1
        return bytes(page)
    def describe(self,fn):
        if fn==1:return super().describe(1)
        ops=pack_ops({OP[k] for k in ('configure','start','stop','status','read','segments','release','query')})   # no force
        return (tlv(DC['ops'],ops),*(tlv(DC['role_channels'],bytes((role,0,0,7))) for role in range(3)),tlv(DC['features'],b'\0'*4),tlv(D['mode'],struct.pack('<BII',MODE['one_shot'],16,0)),tlv(D['mode'],struct.pack('<BII',MODE['repeat'],16,2)),tlv(D['rate_range'],struct.pack('<II',100,1000)),tlv(D['channels'],b'\3'),tlv(D['trigger'],struct.pack('<II',(1<<TRIG['level'])|(1<<TRIG['edge'])|1,8)))
    def request_tags(self,fn,op,data):
        if fn==1:return super().request_tags(fn,op,data)
        got={};seen=set();later=[]
        while data:
            if len(data)<3:raise Reject(R['malformed'])
            raw,size=struct.unpack_from('<BH',data);key=raw&127
            if not key or len(data)<3+size:raise Reject(R['malformed'])
            value,data=data[3:3+size],data[3+size:]
            known=op in (OP['configure'],OP['query']) and key in (C['mode'],C['rate'],C['samples'],C['segments'],C['trigger'],C['pretrigger'])
            if known and size!=SIZES.get(key,4):raise Reject(R['malformed'])   # a later copy too (core §2.3)
            if not known:                                  # every copy of an unknown tag: critical -> unsupported (core §2.3)
                if raw&128:raise Reject(R['unsupported'],bytes((raw,)))
                continue
            if key in seen:later.append((key,raw,value));continue    # a later copy: checked in settings, not used (core §2.3)
            seen.add(key);got[key]=(raw,value)
        got['later']=later;return got
    def release_plans(self,functions):
        if 2 in functions:self.config=None;self.roles=[];self.clear(0)
        super().release_plans(functions)
    def plan(self,op,payload,tags):
        if op==PLAN['plan_release']:
            n=payload[0];fns=list(struct.unpack('<'+'H'*n,payload[1:]))
            if any(fn and fn not in self.functions for fn in fns):raise Reject(R['unknown_function'])   # every fn first; fn 0 has no plan (plan §2.2)
            self.release_plans(fns if n else list(self.plans));return b''
        rows=[(raw,*struct.unpack('<HBH',v)) for raw,v in tags];seen=set()
        for raw,fn,role,ch in rows:
            if fn==0 or (fn,role,ch) in seen:raise Reject(R['malformed'])
            seen.add((fn,role,ch))
        for raw,fn,role,ch in rows:
            if fn not in self.functions:raise Reject(R['unknown_function'])
            if fn!=2 or role not in range(3) or ch not in range(3):raise Reject(R['unsupported'],bytes((raw,)))
        if not rows:return b''
        if len({row[2] for row in rows})!=len(rows) or len({row[3] for row in rows})!=len(rows):raise Reject(R['unsupported'],bytes((rows[0][0],)))   # one channel per role: unsupported (plan §2.4)
        if sorted((row[2],row[3]) for row in rows)==sorted(zip(self.roles,self.plans.get(2,[]))):return b''   # the same set: nothing (plan §2.1)
        if self.state in RUNNING:raise Reject(R['unavailable'],cause('wrong_state'))   # running: a change is cause 6 (capture §3.2)
        self.release_plans([2]);rows.sort(key=lambda row:row[2]);self.roles=[row[2] for row in rows];self.plans[2]=[row[3] for row in rows];return b''
    def settings(self,tags):
        mode_t,rate_t,samples_t,segments_t,trigger_t,pre_t=(C[k] for k in ('mode','rate','samples','segments','trigger','pretrigger'))
        u32=lambda v:struct.unpack('<I',v)[0]
        unsupported=lambda raw:Reject(R['unsupported'],bytes((raw,)))
        if not all(key in tags for key in (mode_t,rate_t)):raise Reject(R['malformed'])
        mode=tags[mode_t][1][0];rate=u32(tags[rate_t][1])
        if rate==0 or any(key in tags and u32(tags[key][1])==0 for key in (samples_t,segments_t)):raise Reject(R['malformed'])
        if mode in (MODE['one_shot'],MODE['repeat']) and samples_t not in tags:raise Reject(R['malformed'])
        later=tags.get('later',[])                         # every later copy is checked too, the first used (core §2.3)
        if any(k in (rate_t,samples_t,segments_t) and not u32(v) for k,_,v in later):raise Reject(R['malformed'])
        for key in (samples_t,segments_t,pre_t):
            if key in tags and ((key==samples_t and mode==MODE['streaming']) or (key==segments_t and mode!=MODE['repeat'])
                                or (key==pre_t and (trigger_t not in tags or tags[trigger_t][1][0]==TRIG['immediate']))):raise unsupported(tags[key][0])
        if mode not in (MODE['one_shot'],MODE['repeat']):raise unsupported(tags[mode_t][0])
        if not 100<=rate<=1000:raise unsupported(tags[rate_t][0])
        bad_trigger=lambda t:t[0] not in TRIG.values() or (t[0]==TRIG['level'] and t[2]>1) or (t[0]==TRIG['edge'] and t[2]>2)
        trigger=struct.unpack('<BBI',tags[trigger_t][1]) if trigger_t in tags else (TRIG['immediate'],0,0)
        if bad_trigger(trigger):raise unsupported(tags[trigger_t][0])
        for k,raw,v in later:                              # a value this probe does not handle: that copy's tag as received
            if (k==mode_t and v[0] not in (MODE['one_shot'],MODE['repeat']) or k==rate_t and not 100<=u32(v)<=1000
                    or k==pre_t and u32(v)>8 or k==trigger_t and bad_trigger(struct.unpack('<BBI',v))):raise unsupported(raw)
        if not self.roles or (trigger[0] and trigger[1] not in self.roles):raise Reject(R['unavailable'],cause('wrong_state'))
        samples=min(16,u32(tags[samples_t][1]));segments=min(2,u32(tags[segments_t][1])) if segments_t in tags else 2 if mode==MODE['repeat'] else 1
        pre=u32(tags[pre_t][1]) if pre_t in tags else 0
        if pre>8 or pre>=samples:raise unsupported(tags[pre_t][0])
        return dict(mode=mode,rate=rate,samples=samples,segments=segments,trigger=trigger,pretrigger=pre)
    def answer(self,c):
        rows=[tlv(A['actual_rate'],struct.pack('<II',c['rate'],1)),tlv(A['layout'],bytes((3,len(self.roles),*range(len(self.roles))))),tlv(A['actual_samples'],struct.pack('<I',c['samples']))]
        if c['mode']==MODE['repeat']:rows.append(tlv(A['actual_segments'],struct.pack('<I',c['segments'])))
        return b''.join(rows)
    def finish(self,short=False):
        if not self.current:return
        data=bytearray((len(self.current)*3+7)//8)
        for i,sample in enumerate(self.current):
            value=4 if len(self.roles)<3 else 0
            for k,bit in enumerate(sample):value|=bit<<k
            for k in range(3):data[(3*i+k)//8]|=((value>>k)&1)<<((3*i+k)%8)
        next_ns=self.start_ns+len(self.current)*1000000000//self.config['rate']
        position=self.base+len(self.data);flags=int(self.gap)|(2 if short else 0);self.data.extend(data)
        self.segments.append((self.serial,position,len(self.current),self.start_ns,1000000,self.trigger_index,flags,self.generation));self.serial=(self.serial+1)&0xffffffff;self.current=[];self.gap=False;self.trigger_index=0xffffffff;self.start_ns=next_ns
        if self.config['mode']==MODE['one_shot']:self.state=ST['done']
        elif len(self.segments)>=self.config['segments']:self.state=ST['paused']
    def dispatch_request(self,fn,op,payload,tags):
        if fn==1:return self.plan(op,payload,tags)
        if op in (OP['configure'],OP['query']):
            if op==OP['configure'] and self.state in RUNNING:raise Reject(R['unavailable'],cause('wrong_state'))
            config=self.settings(tags);answer=self.answer(config)
            if op==OP['configure']:self.config=config;self.clear(ST['configured'])
            return answer
        if op==OP['status']:return struct.pack('<BIQBI',self.state,self.serial,self.base+len(self.data),self.flags,self.generation)+(tlv(_L.tlv['status_answer']['error'],bytes((self.error,))) if self.state==ST['error'] else b'')
        if op==OP['start']:
            if self.state not in (ST['configured'],ST['done'],ST['error']):raise Reject(R['unavailable'],cause('wrong_state'))
            if self.generation==0xffffffff:raise Reject(R['unavailable'],cause('limit'))
            self.clear(ST['waiting'] if self.config['trigger'][0] else ST['capturing']);self.generation+=1;self.start_ns=self.ep.now_ns();return struct.pack('<I',self.generation)
        if op==OP['stop']:
            previous=self.state
            if self.state==ST['capturing']:self.finish(True)
            if previous in RUNNING+(ST['error'],):self.state=ST['configured'];self.current=[];self.history=[]
            return b''
        if op==OP['segments']:
            first=struct.unpack('<I',payload)[0]
            if first==self.serial or not self.segments:rows=[]
            else:
                index=next((i for i,row in enumerate(self.segments) if row[0]==first),0);rows=self.segments[index:]
            limit=(self.ep.probe.max_frame-7)//37;page=rows[:limit];return bytes((int(len(rows)>len(page)),len(page)))+b''.join(struct.pack('<IQIQIIBI',*row) for row in page)
        generation=struct.unpack_from('<I',payload)[0]
        if generation!=self.generation:raise Reject(R['unavailable'],cause('wrong_state'))
        if op==OP['release']:
            serial=struct.unpack_from('<I',payload,4)[0]
            if self.config and self.config['mode']==MODE['repeat'] and self.state in (ST['capturing'],ST['paused']):   # the state table: '—' in 0/1/2/4/6 (capture §3.2)
                self.segments[:]=[row for row in self.segments if ((serial-row[0])&0xffffffff)>=0x80000000]
                new=self.segments[0][1] if self.segments else self.base+len(self.data);del self.data[:new-self.base];self.base=new
                if self.state==ST['paused'] and len(self.segments)<self.config['segments']:self.state=ST['capturing'];self.gap=True;self.start_ns=self.ep.now_ns()
            return b''
        position,maximum=struct.unpack_from('<QH',payload,4);end=self.base+len(self.data)
        if position>=end:return struct.pack('<QBH',end,0,0)
        start=max(self.base,position);count=min(maximum,end-start,self.ep.probe.max_frame-16);part=bytes(self.data[start-self.base:start-self.base+count]);flags=(1 if start+count<end else 0)|(2 if position<self.base else 0);return struct.pack('<QBH',start,flags,count)+part
    def feed(self,samples):
        accepted=0
        for sample in samples:
            if self.state not in (ST['waiting'],ST['capturing']):break
            accepted+=1
            if self.state==ST['waiting']:
                kind,role,value=self.config['trigger'];bit=sample[self.roles.index(role)];previous=self.previous
                hit=(kind==TRIG['level'] and bit==value) or (kind==TRIG['edge'] and previous is not None and bit!=previous and (value==2 or bit==1-value))
                self.previous=bit
                if not hit:self.history.append(sample);self.history=self.history[-self.config['pretrigger']:] if self.config['pretrigger'] else [];continue
                self.state=ST['capturing'];self.current=list(self.history);self.trigger_index=len(self.current);self.start_ns=max(0,self.ep.now_ns()-self.trigger_index*1000000000//self.config['rate']);self.history=[]
            self.current.append(sample)
            target=self.config['samples']
            if self.trigger_index!=0xffffffff:target-=self.config['pretrigger']-self.trigger_index
            if len(self.current)>=target:self.finish()
        return accepted


class LogicModel(GpioModel):
    def __init__(self,boot_id=None):self.milliseconds=0;self.sample_ns=Fraction(0);super().__init__(lambda:self.milliseconds,boot_id=boot_id)
    def install(self):self.extension=Logic(self.ep);self.ep.current_core=Core(self.ep,self.extension)
    def handle(self,request):
        e=self.extension;fixed,free=e.fixed,e.free
        if len(request)>=6:
            fn,op=struct.unpack_from('<HB',request,3);n=request[10] if len(request)>10 else 0
            if fn==1:e.fixed={PLAN['plan_apply']:0,PLAN['plan_release']:1+2*n};e.free=set()
            else:e.fixed={OP['configure']:0,OP['start']:0,OP['stop']:0,OP['status']:0,OP['read']:14,OP['segments']:4,OP['release']:8,OP['query']:0};e.free={OP['status'],OP['read'],OP['segments'],OP['query']}
        try:return self.ep.handle(request)
        finally:e.fixed,e.free=fixed,free
    def logic_state(self):
        e=self.extension;return copy.deepcopy({'plans':e.plans,'pins':e.pins,'idle':e.idle,'roles':e.roles,'config':e.config,'state':e.state,'generation':e.generation,'data':bytes(e.data),'base':e.base,'segments':e.segments,'serial':e.serial,'current':e.current,'history':e.history,'flags':e.flags,'error':e.error,'clock_scope':'explicit-logical-ms'})
    def logic_feed(self,samples):
        roles=len(self.extension.roles)
        if type(samples)is not list or len(samples)>128 or any(type(row)is not list or len(row)!=roles or any(type(bit)is not int or bit not in (0,1) for bit in row) for row in samples):raise ValueError('explicit finite binary samples in plan role order required')
        accepted=0
        for sample in samples:
            count=self.extension.feed([sample]);accepted+=count
            if not count:break
            self.sample_ns+=Fraction(1000000000,self.extension.config['rate']);self.milliseconds=int(self.sample_ns//1000000)
        return accepted
    def logic_advance(self,ms):
        if type(ms)is not int or not 0<=ms<=60000:raise ValueError('explicit 0..60000 logical ms required')
        self.milliseconds+=ms;self.sample_ns+=ms*1000000
    def logic_fail(self,error):
        e=self.extension
        if type(error)is not int or error not in _L.enum['error'].values() or e.state not in (ST['waiting'],ST['capturing']):raise ValueError('active logical acquisition and defined error required')
        e.current=[];e.history=[];e.state=ST['error'];e.flags|=1;e.error=error
    def logic_seed(self,generation):
        if self.ep.current_core.holder is not None or self.extension.state!=ST['unconfigured'] or type(generation)is not int or not 0<=generation<=0xffffffff:raise ValueError('unowned state0 and explicit u32 generation seed required')
        self.extension.generation=generation
