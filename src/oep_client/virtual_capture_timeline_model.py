"""Independent cross-rate capture-group fixture with a separate rational sample grid per track.

Self-contained ADC/logic track engine of the capture family (oep-spec capture §2-§4): one-shot and streaming,
immediate trigger only, no repeat/force. It does not reuse the legacy capture-group, the bench sampler or a Host
decoder. Notifications are queued for an explicit drain. Every protocol number is looked up by name.
"""
import copy
import struct

from fractions import Fraction
from .virtual_gpio_model import Gpio, GpioModel
from .virtual_core import Completed, Core, Reject, tlv
from .virtual_analog_model import refuse_output_idle
from . import registry as _reg
from .catalog import pack_ops
from .virtual_names import R, DC, cause, UNAVAILABLE_TLV

# the capture family's numbers by name (capture §2-§4; one space for the family, core §2.5)
_L, _A, _G = _reg.FIXTURE_LOGIC, _reg.FIXTURE_ANALOG, _reg.FIXTURE_CAPTURE_GROUP
OP, GOP, PLAN = _A.op, _G.op, _reg.PROBE_PLAN.op
C, ANS, D, DATA = _A.tlv["configure"], _A.tlv["configure_answer"], _A.tlv["describe"], _A.tlv["data"]
ST, MODE, TRIG, STOPPED, EV = _L.enum["state"], _L.enum["mode"], _L.enum["trigger"], _L.enum["stopped_reason"], _L.event
ERRORS = set(_L.enum["error"].values())
REF = _A.enum["reference_source"]
ROLE_EVENT, ROLE_DATA = _reg.ROLES["event"], _reg.ROLES["data"]
SIZES = {C["mode"]: 1, C["trigger"]: 6, C["frontend"]: 2}      # the other configure tags are u32
RUNNING = (ST["waiting"], ST["capturing"], ST["paused"])
STARTABLE = (ST["configured"], ST["done"], ST["error"])
ADC, LOGIC, GROUP = 2, 3, 4                                     # this fixture's fns (list order)

NO_TIME=(1<<64)-1
NONE=0xffffffff
LAST=0xffffffff
UNCERTAINTY=1000
MAX_FRAME=96
STREAM_SAMPLES={2:12,3:5}   # probe-chosen streaming segment sizes (ADC splits across frames, logic pads)
ROLES={ADC:(1,3),LOGIC:(0,1)}
CHANNELS={ADC:(0,1),LOGIC:(2,3)}


def wave(fn,index):
    """Explicit fixture waveform, in increasing plan role order."""
    return [(17*index+3)&4095,(4095-29*index)%4096] if fn==ADC else [index&1,(index//3)&1]


def refusal(name,fn=None):
    """Rejected unavailable with the cause (by name) and the fn when given (core §4.3)."""
    return Reject(R['unavailable'],cause(name)+(tlv(UNAVAILABLE_TLV['fn'],struct.pack('<H',fn)) if fn is not None else b''))


class Track:
    def __init__(self,fn,ep,out):
        self.fn=fn;self.ep=ep;self.out=out;self.generation=0;self.roles=[];self.config=None
        self.subscribed=False;self.seq=0;self.fail_start=None;self.clear(ST['unconfigured'])
    def clear(self,state):
        self.state=state;self.data=bytearray();self.base=0;self.segments=[];self.serial=0;self.flags=0;self.error=0
        self.current=[];self.first=0;self.position=0;self.start_ns=0;self.sent=0;self.origin=None;self.next=0
    # layout: ADC s=16 o=2 b=12 order [3,1]; logic w=3 pos [0,1], bit 2 undefined
    def encode(self,rows):
        if self.fn==ADC:return b''.join(struct.pack('<H',(row[self.roles.index(role)]<<2)|0xc003) for row in rows for role in reversed(self.roles))
        out=bytearray((3*len(rows)+7)//8)
        for i,row in enumerate(rows):
            value=4|row[0]|(row[1]<<1)
            for k in range(3):out[(3*i+k)//8]|=((value>>k)&1)<<((3*i+k)%8)
        return bytes(out)
    def confirmed(self,n):return 4*n if self.fn==ADC else 3*n//8
    def full(self,n):return 4*n if self.fn==ADC else (3*n+7)//8
    def size(self):return self.config['samples'] if self.config['mode']==MODE['one_shot'] else STREAM_SAMPLES[self.fn]
    def write_pos(self):return self.position+(self.confirmed(len(self.current)) if self.state==ST['capturing'] else 0)
    def view(self):return bytes(self.data)+(self.encode(self.current)[:self.confirmed(len(self.current))] if self.state==ST['capturing'] else b'')
    def due(self):return self.origin+Fraction(self.next*1000000000,self.config['rate'])
    def grid_ns(self,index):return int(self.origin+Fraction(index*1000000000,self.config['rate']))
    def request_tags(self,op,data):
        got={};seen=set();frontends=[];later=[]
        known_tags=(C['mode'],C['rate'],C['samples'],C['segments'],C['trigger'],C['pretrigger'])
        while data:
            if len(data)<3:raise Reject(R['malformed'])
            raw,size=struct.unpack_from('<BH',data);key=raw&127
            if not key or len(data)<3+size:raise Reject(R['malformed'])
            value,data=data[3:3+size],data[3+size:]
            known=op in (OP['configure'],OP['query']) and (key in known_tags or (key==C['frontend'] and self.fn==ADC))
            if known and size!=SIZES.get(key,4):raise Reject(R['malformed'])   # a later copy too (core §2.3)
            if not known:                                  # every copy of an unknown tag: critical -> unsupported (core §2.3)
                if raw&128:raise Reject(R['unsupported'],bytes((raw,)))
                continue
            if key in seen and key!=C['frontend']:later.append((key,raw,value));continue   # a later copy: checked in settings (core §2.3)
            seen.add(key)
            if key==C['frontend']:frontends.append((raw,value))
            else:got[key]=(raw,value)
        got[C['frontend']]=frontends;got['later']=later;return got
    def settings(self,tags):
        u32=lambda v:struct.unpack('<I',v)[0]
        unsupported=lambda raw:Reject(R['unsupported'],bytes((raw,)))
        if not self.roles:raise refusal('wrong_state')
        if not all(k in tags for k in (C['mode'],C['rate'])):raise Reject(R['malformed'])
        mode=tags[C['mode']][1][0];rate=u32(tags[C['rate']][1])
        if rate==0 or any(k in tags and u32(tags[k][1])==0 for k in (C['samples'],C['segments'])):raise Reject(R['malformed'])
        if mode==MODE['one_shot'] and C['samples'] not in tags:raise Reject(R['malformed'])
        later=tags.get('later',[])                         # every later copy is checked too, the first used (core §2.3)
        if any(k in (C['rate'],C['samples'],C['segments']) and not u32(v) for k,_,v in later):raise Reject(R['malformed'])
        if mode not in (MODE['one_shot'],MODE['streaming']):raise unsupported(tags[C['mode']][0])
        for key in (C['samples'],C['segments'],C['pretrigger']):
            if key in tags and ((key==C['samples'] and mode==MODE['streaming']) or key==C['segments']
                                or (key==C['pretrigger'] and (C['trigger'] not in tags or tags[C['trigger']][1][0]==TRIG['immediate']))):
                raise unsupported(tags[key][0])
        if not 100<=rate<=1000:raise unsupported(tags[C['rate']][0])
        if C['trigger'] in tags and tags[C['trigger']][1][0]!=TRIG['immediate']:raise unsupported(tags[C['trigger']][0])
        for k,raw,v in later:                              # a value this probe does not handle: that copy's tag as received
            if (k==C['mode'] and v[0] not in (MODE['one_shot'],MODE['streaming']) or k==C['rate'] and not 100<=u32(v)<=1000
                    or k==C['trigger'] and v[0]!=TRIG['immediate']):raise unsupported(raw)
        frontend={role:0 for role in self.roles};seen=set()
        for raw,(role,fe) in tags[C['frontend']]:
            if role in seen:raise Reject(R['malformed'])
            seen.add(role)
            if role not in self.roles:raise unsupported(raw)   # capture §3.3: an unlisted refusal is unsupported
            if fe not in (0,1):raise unsupported(raw)
            frontend[role]=fe
        return dict(mode=mode,rate=rate,samples=min(16,u32(tags[C['samples']][1])) if mode==MODE['one_shot'] else 0,frontend=frontend)
    def answer(self,c):
        if self.fn==LOGIC:layout=bytes((3,len(self.roles),*range(len(self.roles))))
        else:layout=bytes((16,2,12,len(self.roles),*reversed(self.roles)))
        rows=[tlv(ANS['actual_rate'],struct.pack('<II',c['rate'],1)),tlv(ANS['layout'],layout)]
        if c['mode']==MODE['one_shot']:rows.append(tlv(ANS['actual_samples'],struct.pack('<I',c['samples'])))
        if self.fn==ADC:
            rows.append(tlv(ANS['reference'],struct.pack('<BIB',REF['external'],1100,_A.enum['reference_how']['nominal'])))
            for role in self.roles:rows.extend((tlv(ANS['scale'],struct.pack('<Bii',role,-17,-1000000 if c['frontend'][role] else 1000000)),tlv(ANS['frontend_used'],bytes((role,c['frontend'][role])))))
        return b''.join(rows)
    def event(self,kind,fixed):
        if self.subscribed:self.out.append(struct.pack('<BHHB',ROLE_EVENT,self.fn,self.seq,kind)+fixed);self.seq=(self.seq+1)&0xffff
    def begin(self,now,state=ST['capturing']):
        self.generation+=1;self.clear(state);self.origin=now;self.start_ns=now
    def arrive(self):
        self.current.append(wave(self.fn,self.next));self.next+=1
        if len(self.current)>=self.size():self.finish()
    def finish(self,short=False):
        if not self.current:return
        n=len(self.current);row=(self.serial,self.position,n,self.start_ns,UNCERTAINTY,NONE,2 if short else 0,self.generation)
        self.data.extend(self.encode(self.current));self.segments=(self.segments+[row])[-16:]
        self.serial=(self.serial+1)&0xffffffff;self.position+=self.full(n);self.first+=n;self.current=[];self.start_ns=self.grid_ns(self.first)
        if self.config['mode']==MODE['one_shot']:
            self.state=ST['done'];self.event(EV['segment'],struct.pack('<IQIQIIBI',*row));self.event(EV['stopped'],struct.pack('<BBI',STOPPED['complete'],0,self.generation))
    def stop(self):
        previous=self.state
        if previous==ST['capturing']:self.finish(True)
        if previous in RUNNING+(ST['error'],):self.state=ST['configured'];self.current=[]
        if previous==ST['capturing']:self.flush();self.event(EV['stopped'],struct.pack('<BBI',STOPPED['host'],0,self.generation))
    def fail(self,error):
        self.current=[];self.state=ST['error'];self.flags|=_L.enum['status_flag']['dropped'];self.error=error;self.flush()
        self.event(EV['stopped'],struct.pack('<BBI',STOPPED['error'],error,self.generation))   # capture §3.4
    def chunks(self):
        """Confirmed, unsent byte ranges, each inside one segment."""
        end=self.write_pos();rows=[(row[1],row[1]+self.full(row[2]),row[2],row[3]) for row in self.segments]
        if self.state==ST['capturing'] and self.current:rows.append((self.position,self.position+self.confirmed(len(self.current)),len(self.current),self.start_ns))
        for start,stop,samples,ns in rows:
            lo,hi=max(start,self.sent),min(stop,end)
            if lo<hi:yield start,lo,hi,samples,ns
    def flush(self):
        if not self.config or self.config['mode']!=MODE['streaming']:return
        limit=self.ep.probe.max_frame-15-7-27;view=self.view()
        for start,lo,hi,samples,ns in list(self.chunks()):
            while lo<hi:
                part=view[lo-self.base:min(hi,lo+limit)-self.base]
                if self.subscribed:
                    info=struct.pack('<QIQI',start,samples,ns,UNCERTAINTY)
                    self.out.append(struct.pack('<BHHQH',ROLE_DATA,self.fn,self.seq,lo,len(part))+part+tlv(DATA['generation'],struct.pack('<I',self.generation))+tlv(DATA['segment_info'],info));self.seq=(self.seq+1)&0xffff
                else:self.flags|=_L.enum['status_flag']['dropped']
                lo+=len(part);self.sent=lo
        cut=min(self.sent,self.base+len(self.data))-self.base
        if cut>0:del self.data[:cut];self.base+=cut
    def read(self,payload):
        generation,position,maximum=struct.unpack('<IQH',payload)
        if generation!=self.generation:raise refusal('wrong_state')
        end=self.write_pos();low=min(self.sent if self.config and self.config['mode']==MODE['streaming'] else self.base,end);gap=0
        if position>=end:return struct.pack('<QBH',end,0,0)
        if position<low:
            if low>=end:return struct.pack('<QBH',end,2,0)
            position=low;gap=2
        count=min(maximum,end-position,self.ep.probe.max_frame-5-11);part=self.view()[position-self.base:position-self.base+count]
        return struct.pack('<QBH',position,gap|int(position+count<end),count)+part
    def dispatch(self,op,payload,tags):
        if op in (OP['configure'],OP['query']):
            if op==OP['configure'] and self.state in RUNNING:raise refusal('wrong_state')
            config=self.settings(tags);answer=self.answer(config)
            if op==OP['configure']:self.config=config;self.clear(ST['configured'])
            return answer
        if op==OP['start']:
            if self.config is None or self.state not in STARTABLE:raise refusal('wrong_state')
            if self.config['mode']==MODE['streaming'] and not self.subscribed:raise refusal('wrong_state')
            if self.generation==LAST:raise refusal('limit')
            self.begin(self.ep.now_ns())
            if self.fail_start is not None:
                error,self.fail_start=self.fail_start,None;self.fail(error);return Completed(struct.pack('<I',self.generation),_reg.OUTCOMES['failed'])
            return struct.pack('<I',self.generation)
        if op==OP['stop']:self.stop();return b''
        if op==OP['status']:return struct.pack('<BIQBI',self.state,self.serial,self.write_pos(),self.flags,self.generation)+(tlv(_A.tlv['status_answer']['error'],bytes((self.error,))) if self.state==ST['error'] else b'')
        if op==OP['read']:return self.read(payload)
        if op==OP['segments']:
            first=struct.unpack('<I',payload)[0];rows=self.segments
            index=next((i for i,row in enumerate(rows) if row[0]==first),None)
            rows=[] if first==self.serial else rows[index or 0:]
            limit=(self.ep.probe.max_frame-7)//37;page=rows[:limit]
            return bytes((int(len(rows)>len(page)),len(page)))+b''.join(struct.pack('<IQIQIIBI',*row) for row in page)
        if op==OP['release']:
            if struct.unpack_from('<I',payload)[0]!=self.generation:raise refusal('wrong_state')
            return b''
        if op==OP['calibration']:return b''
        if op==OP['subscribe']:
            if struct.unpack('<HI',payload)!=(0,0):raise refusal('limit')
            self.drop();self.subscribed=True;self.seq=0;return b''
        self.drop();self.subscribed=False;return b''
    def drop(self):self.out[:]=[f for f in self.out if struct.unpack_from('<H',f,1)[0]!=self.fn]


class TimelineGroup(Gpio):
    functions=(1,2,3,4)
    def __init__(self,ep):
        super().__init__();self.ep=ep;self.pending=[];self.tracks={fn:Track(fn,ep,self.pending) for fn in (ADC,LOGIC)}
        self.idle.update({ch:_reg.CORE.enum['channel_idle']['hi_z'] for ch in CHANNELS[ADC]});self.pins=dict(self.idle)   # the analog's channels: no output idle (capture §1.2)
        self.bound=[];self.generation=0;self.start_ns=NO_TIME
    def list_page(self,first,limit):
        names=[b'oep.probe.plan',b'oep.fixture.analog',b'oep.fixture.logic',b'oep.fixture.capture-group']
        rows=[struct.pack('<HHBB',fn,0,1,len(name))+name for fn,name in enumerate(names,1)];page=bytearray(struct.pack('<HB',4,0))
        for row in rows[first:]:
            if len(page)+len(row)>limit:break
            page.extend(row);page[2]+=1
        return bytes(page)
    def describe(self,fn):
        if fn==1:return super().describe(fn)
        if fn==GROUP:return (tlv(DC['ops'],pack_ops({GOP[k] for k in ('bind','start','stop','status')})),tlv(_G.tlv['describe']['tracks'],struct.pack('<BHH',2,ADC,LOGIC)))
        track_ops={OP[k] for k in ('subscribe','unsubscribe','configure','start','stop','status','read','segments','release','query')}
        rows=[tlv(DC['ops'],pack_ops(track_ops|({OP['calibration']} if fn==ADC else set()))),
              *(tlv(DC['role_channels'],bytes((role,0,0,3 if fn==ADC else 12))) for role in ROLES[fn]),
              tlv(D['mode'],struct.pack('<BII',MODE['one_shot'],16,0)),tlv(D['mode'],struct.pack('<BII',MODE['streaming'],STREAM_SAMPLES[fn],0)),
              tlv(D['rate_range'],struct.pack('<II',100,1000)),tlv(D['channels'],b'\2'),tlv(D['trigger'],struct.pack('<II',1<<TRIG['immediate'],0))]
        if fn==ADC:rows+=[tlv(D['frontend'],struct.pack('<Bii',0,0,3300)),tlv(D['frontend'],struct.pack('<Bii',1,-1000,1000))]
        return tuple(rows)
    def request_tags(self,fn,op,data):
        if fn==1:return super().request_tags(fn,op,data)
        if fn in self.tracks:return self.tracks[fn].request_tags(op,data)
        while data:
            if len(data)<3:raise Reject(R['malformed'])
            raw,size=struct.unpack_from('<BH',data)
            if not raw&127 or len(data)<3+size:raise Reject(R['malformed'])
            if raw&128:raise Reject(R['unsupported'],bytes((raw,)))
            data=data[3+size:]
        return {}
    def release(self):
        self.bound=[];self.release_plans(list(self.plans))
        for t in self.tracks.values():t.subscribed=False
        self.pending.clear()
    def release_plans(self,functions):
        for fn in functions:
            if fn in self.tracks:t=self.tracks[fn];t.roles=[];t.config=None;t.clear(ST['unconfigured'])
        super().release_plans(functions)
    def plan(self,op,payload,tags):
        if op==PLAN['plan_release']:
            count=payload[0];functions=list(struct.unpack('<'+'H'*count,payload[1:])) or list(self.plans)
            if any(fn and fn not in self.functions for fn in functions):raise Reject(R['unknown_function'])   # every fn first; fn 0 has no plan (plan §2.2)
            for fn in functions:
                if fn in self.bound:raise refusal('bound_in_group',fn)
            self.release_plans(functions);return b''
        rows=[(raw,*struct.unpack('<HBH',value)) for raw,value in tags]
        if len(set((fn,role,ch) for _,fn,role,ch in rows))!=len(rows):raise Reject(R['malformed'])
        for raw,fn,role,ch in rows:
            if fn==0:raise Reject(R['malformed'])
            if fn not in self.functions:raise Reject(R['unknown_function'])
            if fn not in self.tracks or role not in ROLES[fn] or ch not in CHANNELS[fn]:raise Reject(R['unsupported'],bytes((raw,)))
        if len({(r[1],r[2]) for r in rows})!=len(rows) or len({r[3] for r in rows})!=len(rows):raise Reject(R['unsupported'],bytes((rows[0][0],)))   # one channel per role: unsupported (plan §2.4)
        refuse_output_idle(self.idle,(r[3] for r in rows if r[1]==ADC))   # the analog track (capture §1.2)
        affected={r[1] for r in rows};changed=set()
        for fn in affected:
            selected=sorted((role,ch) for _,owner,role,ch in rows if owner==fn)
            if selected!=sorted(zip(self.tracks[fn].roles,self.plans.get(fn,[]))):
                if fn in self.bound:raise refusal('bound_in_group',fn)
                if self.tracks[fn].state in RUNNING:raise refusal('wrong_state',fn)
                changed.add(fn)
        self.release_plans(changed)
        for fn in changed:
            selected=sorted((role,ch) for _,owner,role,ch in rows if owner==fn)
            self.tracks[fn].roles=[role for role,ch in selected];self.plans[fn]=[ch for role,ch in selected]
        return b''
    def state(self):
        GS=_G.enum['state']
        if not self.bound:return GS['unconfigured']
        states=[self.tracks[fn].state for fn in self.bound]
        if ST['error'] in states:return GS['error']
        if self.start_ns!=NO_TIME and all(s==ST['done'] for s in states):return GS['done']
        if any(s in RUNNING for s in states):return GS['capturing']
        return GS['configured']
    def bind(self,payload):
        count=payload[0];functions=list(struct.unpack('<'+'H'*count,payload[1:]))
        if functions==self.bound:return b''
        if self.state()==_G.enum['state']['capturing']:raise refusal('wrong_state')
        if not count:self.bound=[];return b''
        fn_tlv=lambda fn:tlv(UNAVAILABLE_TLV['fn'],struct.pack('<H',fn))
        if len(set(functions))!=len(functions):raise Reject(R['malformed'],fn_tlv(functions[0]))
        for fn in functions:
            if fn not in self.tracks:raise Reject(R['unsupported'],b'\0'+fn_tlv(fn))
        for fn in functions:
            t=self.tracks[fn]
            if t.config is None or t.state not in STARTABLE or t.config['mode']!=self.tracks[functions[0]].config['mode']:raise refusal('wrong_state',fn)
        self.bound=functions;return b''
    def start(self):
        if not self.bound:raise refusal('wrong_state')
        for fn in self.bound:
            t=self.tracks[fn]
            if t.config is None or t.state not in STARTABLE or (t.config['mode']==MODE['streaming'] and not t.subscribed):raise refusal('wrong_state',fn)
        if self.generation==LAST:raise refusal('limit')
        for fn in self.bound:
            if self.tracks[fn].generation==LAST:raise refusal('limit',fn)
        now=self.ep.now_ns();self.generation+=1;self.start_ns=now;failed=None
        for fn in self.bound:self.tracks[fn].begin(now,ST['configured'])
        for fn in self.bound:
            t=self.tracks[fn];t.state=ST['capturing']
            if t.fail_start is not None:
                error,t.fail_start=t.fail_start,None;t.fail(error);failed=fn;break
        if failed:
            for fn in self.bound:
                if self.tracks[fn].state==ST['capturing']:self.tracks[fn].stop()
        out=struct.pack('<QIB',now,self.generation,len(self.bound))+b''.join(struct.pack('<HI',fn,self.tracks[fn].generation) for fn in self.bound)
        return Completed(out,_reg.OUTCOMES['failed']) if failed else out
    def dispatch_request(self,fn,op,payload,tags):
        if fn==1:return self.plan(op,payload,tags)
        if fn in self.tracks:
            if fn in self.bound and op in (OP['configure'],OP['start'],OP['stop']):raise refusal('bound_in_group',fn)
            return self.tracks[fn].dispatch(op,payload,tags)
        if op==GOP['bind']:return self.bind(payload)
        if op==GOP['start']:return self.start()
        if op==GOP['stop']:
            for member in self.bound:self.tracks[member].stop()
            return b''
        return struct.pack('<BQI',self.state(),self.start_ns,self.generation)
    def failure(self,fn,error):
        self.tracks[fn].fail(error)
        if fn in self.bound:
            for other in self.bound:
                if other!=fn:self.tracks[other].stop()


class CaptureTimelineModel(GpioModel):
    def __init__(self,boot_id=None):
        self.absolute_ns=Fraction(0);super().__init__(lambda:int(self.absolute_ns//1000000),boot_id=boot_id);self.ep.probe.max_frame=MAX_FRAME
    def install(self):
        self.ep._clock_ns=lambda:int(self.absolute_ns)
        self.extension=TimelineGroup(self.ep);self.ep.current_core=Core(self.ep,self.extension)
    def handle(self,request):
        e=self.extension;fixed,free=e.fixed,e.free
        if len(request)>=6:
            fn,op=struct.unpack_from('<HB',request,3);n=request[10] if len(request)>10 else 0
            if fn==1:e.fixed={PLAN['plan_apply']:0,PLAN['plan_release']:1+2*n};e.free=set()
            elif fn==GROUP:e.fixed={GOP['bind']:1+2*n,GOP['start']:0,GOP['stop']:0,GOP['status']:0};e.free={GOP['status']}
            else:e.fixed={OP['subscribe']:6,OP['unsubscribe']:0,OP['configure']:0,OP['start']:0,OP['stop']:0,OP['status']:0,OP['read']:14,OP['segments']:4,OP['release']:8,OP['query']:0,**({OP['calibration']:0} if fn==ADC else {})};e.free={OP['status'],OP['read'],OP['segments'],OP['query'],OP['calibration']}
        try:return self.ep.handle(request)
        finally:e.fixed,e.free=fixed,free
    def timeline_state(self):
        e=self.extension
        return copy.deepcopy(dict(bound=e.bound,generation=e.generation,start_ns=e.start_ns,pending=len(e.pending),
          tracks={fn:dict(state=t.state,generation=t.generation,config=t.config,roles=t.roles,data=bytes(t.data),base=t.base,segments=t.segments,current=t.current,
                         flags=t.flags,error=t.error,subscribed=t.subscribed,sent=t.sent,origin=t.origin,next=t.next) for fn,t in e.tracks.items()},
          plans=e.plans,pins=e.pins,idle=e.idle,now_ns=self.ep.now_ns(),clock_scope='explicit-logical-ns'))
    def timeline_advance_to(self,ns):
        if type(ns)is not int or ns<self.ep.now_ns() or ns-self.ep.now_ns()>1000000000:raise ValueError('forward explicit boot-relative ns within one second required')
        target=self.ep._origin_ns+ns;e=self.extension;events=0
        while True:
            due={fn:self.ep._origin_ns+t.due() for fn,t in e.tracks.items() if t.state==ST['capturing']}
            core=self.ep.current_core
            expiry=max(self.absolute_ns,Fraction(core.deadline*1000000)) if core.holder is not None else None
            if expiry is not None and expiry<=target and (not due or expiry<=min(due.values())):
                self.absolute_ns=expiry;core.tick();continue
            if not due or min(due.values())>target:break
            when=min(due.values());self.absolute_ns=when
            for fn in sorted(fn for fn,time in due.items() if time==when):e.tracks[fn].arrive();events+=1
            if events>4096:raise ValueError('finite explicit timeline budget exceeded')
        self.absolute_ns=Fraction(target);return events
    def timeline_drain(self):
        e=self.extension
        for t in e.tracks.values():t.flush()
        frames=list(e.pending);e.pending.clear();return frames
    def timeline_fail(self,fn,error):
        e=self.extension
        if fn not in e.tracks or type(error)is not int or error not in ERRORS or e.tracks[fn].state!=ST['capturing']:raise ValueError('active track and defined error required')
        e.failure(fn,error)
    def timeline_fail_next_start(self,fn,error):
        e=self.extension
        if fn not in e.tracks or type(error)is not int or error not in ERRORS or e.tracks[fn].state in RUNNING:raise ValueError('idle track and defined error required')
        e.tracks[fn].fail_start=error
    def timeline_seed(self,generation,adc,logic):
        e=self.extension
        if self.ep.current_core.holder is not None or e.bound or any(type(v)is not int or not 0<=v<=LAST for v in (generation,adc,logic)):raise ValueError('unowned explicit group/track u32 seeds required')
        e.generation=generation;e.tracks[ADC].generation=adc;e.tracks[LOGIC].generation=logic
