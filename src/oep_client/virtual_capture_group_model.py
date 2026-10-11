"""Independent, explicitly clocked two-track capture-group fixture.

Reuses independent ADC/logic encoders, not the legacy capture-group or Host.
This finite fixture accepts equal-rate groups only (explicit resource limit).
"""
import copy
import struct
from fractions import Fraction
from .virtual_gpio_model import Gpio
from .virtual_logic_model import Logic, LogicModel
from .virtual_analog_model import Analog
from .virtual_core import Core, Reject, tlv

NO_TIME=(1<<64)-1


class CaptureGroup(Gpio):
    functions=(1,2,3,4)
    requires_equal_rate=True
    def __init__(self,ep):
        super().__init__(False);self.ep=ep;self.tracks={2:Analog(ep),3:Logic(ep)}
        self.bound=[];self.source=0;self.generation=0;self.start_ns=NO_TIME;self.trigger_ns=NO_TIME;self.trigger_fn=0
    def list_page(self,first,limit):
        names=[b'oep.probe.plan',b'oep.fixture.analog',b'oep.fixture.logic',b'oep.fixture.capture-group']
        rows=[struct.pack('<HHBBB',fn,0,1,0,len(name))+name for fn,name in enumerate(names,1)];page=bytearray(struct.pack('<HB',4,0))
        for row in rows[first:]:
            if len(page)+len(row)>limit:break
            page.extend(row);page[2]+=1
        return bytes(page)
    def describe(self,fn):
        if fn==1:return super().describe(fn)
        if fn==4:return (tlv(7,b'\x10\x1f'),tlv(5,b'\0'*4),tlv(64,b'\2\2\0\3\0'))
        rows=list(self.tracks[fn].describe(2))
        rows=[tlv(7,b'\x10\xff'+(b'\3' if fn==2 else b'\1')) if r[0]==7 else r for r in rows]
        if fn==3:rows=[tlv(1,bytes((r[3],0,0,12))) if r[0]==1 else r for r in rows if r[0]!=1 or r[3] in (0,1)]
        if fn==3:rows=[tlv(68,b'\2') if r[0]==68 else r for r in rows]
        return tuple(rows)
    def request_tags(self,fn,op,data):
        if fn==1:return super().request_tags(fn,op,data)
        if fn in self.tracks:return self.tracks[fn].request_tags(2,op,data)
        result={};seen=set()
        while data:
            if len(data)<3:raise Reject(3)
            raw,size=struct.unpack_from('<BH',data);key=raw&127
            if not key or len(data)<3+size:raise Reject(3)
            value,data=data[3:3+size],data[3+size:]
            if key in seen:continue
            seen.add(key)
            if op==16 and key==1:
                if size!=2:raise Reject(3)
                result[key]=(raw,int.from_bytes(value,'little'))
            elif raw&128:raise Reject(11,bytes((raw,)))
        return result
    def unbind(self):
        self.bound=[];self.source=0
    def release(self):
        self.unbind();self.release_plans(list(self.plans))
    def release_plans(self,functions):
        for fn in functions:
            if fn in self.tracks:
                t=self.tracks[fn];t.roles=[];t.config=None;t.clear(0)
        super().release_plans(functions)
    @staticmethod
    def refusal(cause,fn=None):return Reject(4,tlv(1,bytes((cause,)))+(tlv(5,struct.pack('<H',fn)) if fn is not None else b''))
    def plan(self,op,payload,tags):
        if op==17:
            count=payload[0];functions=list(struct.unpack('<'+'H'*count,payload[1:])) or list(self.plans)
            for fn in functions:
                if fn in self.bound:raise self.refusal(4,fn)
            self.release_plans(functions);return b''
        rows=[(raw,*struct.unpack('<HBH',value)) for raw,value in tags]
        if len(set((fn,role,ch) for _,fn,role,ch in rows))!=len(rows):raise Reject(3)
        affected={r[1] for r in rows}
        for raw,fn,role,ch in rows:
            if fn==0:raise Reject(3)
            if fn not in self.functions:raise Reject(1)
            if fn in self.bound:raise self.refusal(4,fn)
            if fn not in self.tracks or role not in ((1,3) if fn==2 else (0,1)) or ch not in ((0,1) if fn==2 else (2,3)):raise Reject(11,bytes((raw,)))
        if len({(r[1],r[2]) for r in rows})!=len(rows) or len({r[3] for r in rows})!=len(rows):raise Reject(3)
        self.release_plans(affected)
        for fn in affected:
            selected=sorted((role,ch) for _,owner,role,ch in rows if owner==fn)
            self.tracks[fn].roles=[role for role,ch in selected];self.plans[fn]=[ch for role,ch in selected]
        return b''
    def state(self):
        if not self.bound:return 0
        states=[self.tracks[fn].state for fn in self.bound]
        if 6 in states:return 6
        if self.start_ns!=NO_TIME and all(s==4 for s in states):return 4
        if self.source and self.trigger_ns==NO_TIME and any(s in (2,3) for s in states):return 2
        if any(s in (2,3,5) for s in states):return 3
        return 1
    def bind(self,payload,tags):
        count=payload[0];functions=list(struct.unpack('<'+'H'*count,payload[1:]));source=tags.get(1,(0,0))[1]
        if not count:
            if self.state()==3:raise self.refusal(6)
            self.unbind();return b''
        if len(set(functions))!=len(functions):raise Reject(3,tlv(5,struct.pack('<H',functions[0])))
        for fn in functions:
            if fn not in self.tracks:raise Reject(11,b'\0'+tlv(5,struct.pack('<H',fn)))
        for fn in functions:
            t=self.tracks[fn]
            if t.config is None or t.state not in (1,4,6):raise self.refusal(6,fn)
        first=self.tracks[functions[0]].config
        for fn in functions:
            t=self.tracks[fn];c=t.config
            if c['mode']!=first['mode'] or (fn!=source and c['trigger'][0]):raise self.refusal(6,fn)
            # Explicit equal-rate acquisition resource; does not advertise every combination.
            if self.requires_equal_rate and c['rate']!=first['rate']:raise self.refusal(2,fn)
            if source in functions:
                src=self.tracks[source].config;p=-(-(src['pretrigger']*c['rate'])//src['rate'])
                if fn!=source and (p>8 or p>=c['samples']):raise self.refusal(6,fn)
        if source and source not in functions:raise self.refusal(6,source)
        self.bound=functions;self.source=source;return b''
    def start(self):
        if not self.bound:raise self.refusal(6)
        for fn in self.bound:
            if self.tracks[fn].state not in (1,4,6) or self.tracks[fn].config is None:raise self.refusal(6,fn)
        self.start_ns=self.ep.now_ns();self.trigger_ns=NO_TIME;self.trigger_fn=0;self.generation=(self.generation+1)&0xffffffff or 1
        source=self.tracks.get(self.source);waiting=source is not None and source.config['trigger'][0]!=0
        for fn in self.bound:
            t=self.tracks[fn];t.dispatch_request(2,17,b'',{})
            if waiting:t.state=2
        out=struct.pack('<IQIB',0,self.start_ns,self.generation,len(self.bound))
        return out+b''.join(struct.pack('<HI',fn,self.tracks[fn].generation) for fn in self.bound)
    def fire(self,forced=False):
        source=self.tracks[self.source];self.trigger_ns=self.ep.now_ns();self.trigger_fn=0 if forced else self.source
        for fn in self.bound:
            t=self.tracks[fn];p=-(-(source.config['pretrigger']*t.config['rate'])//source.config['rate'])
            t.state=3;t.current=list(t.history[-p:]) if p else [];t.history=[];t.trigger_index=len(t.current)
            t.start_ns=max(self.start_ns,self.trigger_ns-t.trigger_index*1000000000//t.config['rate'])
    def dispatch_request(self,fn,op,payload,tags):
        if fn==1:return self.plan(op,payload,tags)
        if fn in self.tracks:
            if fn in self.bound and op in (16,17,18,19):raise self.refusal(4,fn)
            t=self.tracks[fn]
            if op==19:
                if t.state==2:
                    t.state=3;t.current=list(t.history);t.trigger_index=len(t.current);t.history=[]
                return b''
            if op==24 and fn==3 and t.state in (2,3,5):
                before=t.state;t.state=1
                try:return t.dispatch_request(2,op,payload,tags)
                finally:t.state=before
            return t.dispatch_request(2,op,payload,tags)
        if op==16:return self.bind(payload,tags)
        if op==17:return self.start()
        if op==18:
            for member in self.bound:self.tracks[member].dispatch_request(2,18,b'',{})
            return b''
        if op==19:
            if self.state()==2:self.fire(True)
            return b''
        return struct.pack('<BQQHI',self.state(),self.start_ns,self.trigger_ns,self.trigger_fn,self.generation)
    def feed(self,rows):
        accepted=0
        for row in rows:
            active=[fn for fn in row if self.tracks[fn].state in (2,3)]
            if not active:break
            if self.bound and self.state()==2:
                src=self.tracks[self.source];sample=row[self.source];kind,role,value=src.config['trigger'];current=sample[src.roles.index(role)];previous=src.previous
                hit=previous is not None and (((kind==3 and previous<value<=current) or (kind==4 and previous>value>=current)) if self.source==2 else (kind==1 and current==value) or (kind==2 and current!=previous and (value==2 or current==1-value)))
                src.previous=current
                if hit:self.fire()
                else:
                    for fn in self.bound:
                        t=self.tracks[fn];p=-(-(src.config['pretrigger']*t.config['rate'])//src.config['rate'])
                        t.history=(t.history+[row[fn]])[-p:] if p else []
            if not self.bound or self.state()!=2:
                for fn in active:
                    t=self.tracks[fn]
                    if not self.bound:t.feed([row[fn]]);continue
                    t.current.append(row[fn]);target=t.config['samples']
                    if t.trigger_index!=0xffffffff:
                        src=self.tracks[self.source].config;p=-(-(src['pretrigger']*t.config['rate'])//src['rate'])
                        target-=p-t.trigger_index
                    if len(t.current)>=target:t.finish()
            accepted+=1
            rate=self.tracks[active[0]].config['rate'];self.ep.group_sample_ns+=Fraction(1000000000,rate)
            self.ep.group_ms=int(self.ep.group_sample_ns//1000000)
        return accepted


class CaptureGroupModel(LogicModel):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs);self.ep.probe.max_frame=256
    def install(self):
        self.ep.group_ms=self.milliseconds;self.ep.group_sample_ns=Fraction(self.milliseconds*1000000);self.ep.now=lambda:self.ep.group_ms
        self.extension=CaptureGroup(self.ep);self.ep.current_core=Core(self.ep,self.extension)
    def handle(self,request):
        self.milliseconds=self.ep.group_ms;e=self.extension;fixed,free=e.fixed,e.free
        if len(request)>=6:
            fn,op=struct.unpack_from('<HB',request,3);n=request[10] if len(request)>10 else 0
            if fn==1:e.fixed={16:0,17:1+2*n};e.free=set()
            elif fn==4:e.fixed={16:1+2*n,17:0,18:0,19:0,20:0};e.free={20}
            else:e.fixed={16:0,17:0,18:0,19:0,20:0,21:16,22:4,23:8,24:0,**({25:0} if fn==2 else {})};e.free={20,21,22,24,25}
        try:return self.ep.handle(request)
        finally:e.fixed,e.free=fixed,free
    def group_state(self):
        e=self.extension
        return copy.deepcopy(dict(bound=e.bound,source=e.source,generation=e.generation,start_ns=e.start_ns,trigger_ns=e.trigger_ns,trigger_fn=e.trigger_fn,
          tracks={fn:dict(state=t.state,generation=t.generation,config=t.config,roles=t.roles,data=bytes(t.data),base=t.base,segments=t.segments,current=t.current,history=t.history,flags=t.flags,error=t.error) for fn,t in e.tracks.items()},
          plans=e.plans,pins=e.pins,idle=e.idle,now_ns=self.ep.now_ns(),clock_scope='explicit-logical-ms'))
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
        if fn in e.bound:
            for other in e.bound:
                if other!=fn:e.tracks[other].dispatch_request(2,18,b'',{})
    def group_seed(self,generation,adc,logic):
        if self.ep.current_core.holder is not None or self.extension.bound or any(type(v)is not int or not 0<=v<=0xffffffff for v in (generation,adc,logic)):raise ValueError('unowned explicit group/track u32 seeds required')
        e=self.extension;e.generation=generation;e.tracks[2].generation=adc;e.tracks[3].generation=logic
    def group_invalidate(self,fn):
        if fn not in self.extension.tracks:raise ValueError('explicit track required')
        t=self.extension.tracks[fn];t.config=None;t.clear(0)
