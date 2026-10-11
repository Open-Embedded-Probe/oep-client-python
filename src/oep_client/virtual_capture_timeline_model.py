"""Independent one-shot group engine with a separate rational sample timeline per track."""
import copy
import struct
from fractions import Fraction
from .virtual_capture_group_model import CaptureGroup,CaptureGroupModel,NO_TIME
from .virtual_core import Core,Reject


class TimelineGroup(CaptureGroup):
    requires_equal_rate=False
    def __init__(self,ep):
        super().__init__(ep);self.wave={fn:None for fn in self.tracks};self.grids={}
    def describe(self,fn):
        rows=super().describe(fn)
        return tuple(row for row in rows if fn not in self.tracks or row[0]!=64 or row[3]==1)
    def dispatch_request(self,fn,op,payload,tags):
        if fn in self.tracks and op in (16,24) and 64 in tags and tags[64][1]!=b'\1':raise Reject(11,bytes((tags[64][0],)))
        result=super().dispatch_request(fn,op,payload,tags)
        if fn in self.tracks and op==17:self.grid(fn)
        return result
    def grid(self,fn):
        self.grids[fn]=dict(origin=self.start_ns if fn in self.bound else self.ep.now_ns(),next=0,take=0,target=self.tracks[fn].config['samples'],values={})
    def release_plans(self,functions):
        functions=list(functions);super().release_plans(functions)
        for fn in functions:self.grids.pop(fn,None)
    def start(self):
        result=super().start()
        for fn in self.bound:self.grid(fn)
        return result
    def sample(self,fn,index):
        rise=self.wave[fn];high=rise is not None and index>=rise
        return [((17*index)&4095 if role==1 else 3000 if high else 0) if fn==2 else (int(high) if role==0 else (index>>1)&1) for role in self.tracks[fn].roles]
    def fire(self,forced=False):
        source=self.tracks[self.source];self.trigger_ns=self.ep.now_ns();self.trigger_fn=0 if forced else self.source
        for fn in self.bound:
            t=self.tracks[fn];g=self.grids[fn];rate=t.config['rate'];p=-(-(source.config['pretrigger']*rate)//source.config['rate'])
            nearest=max(0,round(Fraction(self.trigger_ns-g['origin'])*rate/1000000000));available=min(p,nearest)
            g['take']=nearest-available;g['target']=t.config['samples']-p+available
            t.state=3;t.current=[];t.history=[];t.trigger_index=available
            t.start_ns=int(Fraction(g['origin'])+Fraction(g['take']*1000000000,rate))
            self.flush(fn)
    def flush(self,fn):
        t=self.tracks[fn];g=self.grids[fn]
        while t.state==3 and g['take'] in g['values'] and len(t.current)<g['target']:
            t.current.append(g['values'].pop(g['take']));g['take']+=1
        if t.state==3 and len(t.current)==g['target']:t.finish(g['target']<t.config['samples'])
    def arrival(self,fn):
        t=self.tracks[fn];g=self.grids[fn];index=g['next'];value=self.sample(fn,index);g['next']+=1
        if not self.bound:t.feed([value]);return
        g['values'][index]=value
        if self.state()==2:
            src=self.tracks[self.source]
            if fn==self.source:
                kind,role,threshold=src.config['trigger'];raw=value[src.roles.index(role)];old=src.previous;src.previous=raw
                hit=(old is not None and ((old<threshold<=raw) if kind==3 else (old>threshold>=raw))) if fn==2 else ((kind==1 and raw==threshold) or (kind==2 and old is not None and old!=raw and (threshold==2 or raw==1-threshold)))
                if hit:self.fire()
            if self.state()==2:
                p=-(-(src.config['pretrigger']*t.config['rate'])//src.config['rate'])
                g['values']={i:row for i,row in g['values'].items() if i>=g['next']-p-2}
                return
        self.flush(fn)


class CaptureTimelineModel(CaptureGroupModel):
    def __init__(self,*args,**kwargs):self.absolute_ns=Fraction(0);super().__init__(*args,**kwargs)
    def install(self):
        self.ep.group_ms=int(self.absolute_ns//1000000);self.ep.group_sample_ns=self.absolute_ns
        self.ep.now=lambda:int(self.absolute_ns//1000000);self.ep._clock_ns=lambda:int(self.absolute_ns)
        self.extension=TimelineGroup(self.ep);self.ep.current_core=Core(self.ep,self.extension)
    def group_state(self):
        return dict(super().group_state(),clock_scope='explicit-logical-ns',timeline=copy.deepcopy(self.extension.grids),wave=dict(self.extension.wave))
    def timeline_program(self,fn,rise):
        if fn not in self.extension.tracks or (rise is not None and (type(rise)is not int or not 0<=rise<=128)) or self.extension.tracks[fn].state in (2,3,5):raise ValueError('explicit idle track and finite rising sample index required')
        self.extension.wave[fn]=rise
    def set_time(self,ns):
        self.absolute_ns=Fraction(ns);self.ep.group_sample_ns=self.absolute_ns;self.ep.group_ms=int(self.absolute_ns//1000000);self.milliseconds=self.ep.group_ms
    def timeline_advance_to(self,ns):
        if type(ns)is not int or ns<self.ep.now_ns() or ns-self.ep.now_ns()>1000000000:raise ValueError('forward explicit boot-relative ns within one second required')
        target=self.ep._origin_ns+ns;e=self.extension;events=0
        while True:
            due={fn:self.ep._origin_ns+g['origin']+Fraction(g['next']*1000000000,e.tracks[fn].config['rate']) for fn,g in e.grids.items() if e.tracks[fn].state in (2,3)}
            if not due or min(due.values())>target:break
            when=min(due.values());self.set_time(when)
            # Process simultaneous arrivals at the same clock instant.
            # Source first may choose a future nearest peer; its frame is added in the same instant below.
            ordered=sorted((fn for fn,time in due.items() if time==when),key=lambda fn:fn!=e.source)
            for fn in ordered:e.arrival(fn);events+=1
            if events>4096:raise ValueError('finite explicit timeline budget exceeded')
        self.set_time(target);return events
    def group_advance(self,ms):
        if type(ms)is not int or not 0<=ms<=1000:raise ValueError('explicit timeline ms within one second required')
        return self.timeline_advance_to(self.ep.now_ns()+ms*1000000)
    def group_feed(self,rows):raise ValueError('timeline model uses programmed rows and advance_to, not paired feed')
