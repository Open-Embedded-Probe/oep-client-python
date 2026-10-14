"""Selected-SPEC cross-rate group stop/error/replay, generation exhaustion, start failure, state table and ownership checks."""
import secrets
import struct
from .conformance import require,tlvs
from .conformance_capture_timeline import CaptureTimelineChecks,NO_TIME,NONE,arrived

LAST=0xffffffff


class CaptureTimelineLifecycleChecks(CaptureTimelineChecks):
    cases=('STOP-ACTIVE','STOP-MIXED','ERROR-FAST','ERROR-SLOW','ERROR-MIXED',
           'REPLAY-START','REPLAY-STOP','QUERY-ACTIVE','QUERY-MIXED','END-ACTIVE',
           'EXPIRY-ACTIVE','EXPIRY-RETAINED','SESSION-FORCE','GEN-EXHAUST-GROUP','GEN-EXHAUST-TRACK',
           'START-FAIL-FIRST','START-FAIL-SECOND','START-FAIL-TRACK','STATE-TABLE','STREAM-ERROR')
    scenarios=tuple((fast,kind) for kind in cases for fast in (2,3))
    def prepare(self,sid,fast,mode=1,start=True):
        self.plan(sid);self.members=(fast,5-fast);self.rates={fast:1000,5-fast:333}
        for fn in self.members:self.configure(sid,fn,mode,self.rates[fn])
        self.bind(sid,self.members)
        if mode==3:self.subscribe(sid,self.members)
        return self.start(sid,self.members) if start else None
    def raw(self,fn,generation,count):
        rows=self.segments(fn)
        if not count:
            require(not rows and self.read(fn,generation)==(0,0,b''),'empty/failed acquisition published data')
            require(self.status(fn)[1:3]==(0,0),'empty/failed serial_done/write_pos');return
        require(len(rows)==1,'cancelled group segment count');row=rows[0]
        require(row[0:4]==(0,0,count,self.origin) and row[5]==NONE and row[6]==(2 if count<16 else 0) and row[7]==generation,'partial segment geometry/generation/short')
        position,flags,data=self.read(fn,generation);require(position==0 and flags==0 and len(data)==self.full(fn,count),'cancelled read bounds')
        self.expect(fn,data,0,count,'partial raw samples')
        require(self.status(fn)[1:3]==(1,len(data)),'serial_done/write_pos after cancellation')
    def stable_advance(self,ns):
        before=self.a.state();self.a.advance_to(ns);after=self.a.state()
        require(after['tracks']==before['tracks'] and after['start_ns']==before['start_ns'],'stopped acquisition continued sampling')
    def unchanged(self,request,message):
        before=self.a.state();self.ok(request);require(self.a.state()==before,message)
    def track_start(self,sid,fn,failed=False):
        before=self.a.state();request=self.req(fn,'start',sid)
        if failed:
            reply=self.c.exchange(request);r=self.c.reg
            require(reply[3:5]==bytes((r['resolutions']['completed'],r['outcomes']['failed'])),'failed track initialisation must complete failed');p=reply[5:]
        else:p=self.c.success(request)
        require(len(p)>=4,'track start generation');tlvs(p[4:]);generation=struct.unpack_from('<I',p)[0]
        require(generation==before['tracks'][fn]['generation']+1,'track generation progression without wrap')
        self.origin=before['now_ns'];require(self.status(fn)[4]==generation,'track start generation readback');return generation
    def exhaust(self,sid,fast,kind):
        self.prepare(sid,fast,start=False);start=self.req(4,'start',sid)
        if kind=='GEN-EXHAUST-GROUP':
            self.refused(start,'unavailable',2);require(self.status()==(1,NO_TIME,LAST),'exhausted group generation/state kept')
            require(all(self.status(fn)[:5]==(1,0,0,0,5) for fn in self.members),'exhausted group start changed tracks')
            self.bind(sid,());require(self.track_start(sid,fast)==6,'track generation independent of the group')
            return
        self.refused(start,'unavailable',2,fast);require(self.status()==(1,NO_TIME,5),'exhausted track changed the group generation')
        self.bind(sid,());self.refused(self.req(fast,'start',sid),'unavailable',2)
        require(self.status(fast)[:5]==(1,0,0,0,LAST),'exhausted track generation wrapped or state changed')
        require(self.track_start(sid,5-fast)==6,'peer generation independent of exhausted track')
    def start_failure(self,sid,fast,kind):
        if kind=='START-FAIL-TRACK':
            self.prepare(sid,fast,start=False);self.bind(sid,());self.a.fail_next_start(fast,3);generation=self.track_start(sid,fast,failed=True)
            require(self.status(fast)[:3]==(6,0,0) and self.status(fast)[4:]==(generation,b'\3'),'failed track start state/error/write_pos')
            require(not self.segments(fast) and self.read(fast,generation)==(0,0,b''),'failed track start published data')
            self.stable_advance(self.a.state()['now_ns']+50000000)
            generation=self.track_start(sid,fast);self.a.advance_to(self.origin+100000000);require(self.status(fast)[0]==4,'restart after failed track start');self.raw(fast,generation,16);return
        self.prepare(sid,fast,start=False);target=self.members[0 if kind=='START-FAIL-FIRST' else 1];other=5-target
        self.a.fail_next_start(target,1);generation,gens,request,reply=self.start(sid,self.members,failed=True)
        again=self.c.exchange(request);require(again[5:]==reply and again[3:5]==bytes((self.c.reg['resolutions']['completed'],self.c.reg['outcomes']['failed'])),'failed start replay answer')
        require(self.status()[0]==6,'failed group start state')
        require(self.status(target)[:3]==(6,0,0) and self.status(target)[4:]==(gens[target],b'\1'),'failed track state/error after group start')
        require(self.status(other)[:3]==(1,0,0) and self.status(other)[4]==gens[other],'unstarted/stopped peer state after failed group start')
        for fn in self.members:require(not self.segments(fn) and self.read(fn,gens[fn])==(0,0,b''),'failed group start published data')
        self.stable_advance(self.a.state()['now_ns']+50000000)
        new,newgens,_,_=self.start(sid,self.members);self.origin=self.status()[1];self.a.advance_to(self.origin+100000000)
        require(self.status()[0]==4,'group restart after failed start')
        for fn in self.members:self.raw(fn,newgens[fn],16)
    def table(self,sid,fn):
        """capture §3.2 state table for one unbound track (states 2/5 and force are not offered by this fixture)."""
        settings=self.settings(fn,1,1000,16);release=lambda generation:self.req(fn,'release',sid,struct.pack('<II',generation,0))
        self.refused(self.req(fn,'configure',sid,settings),'unavailable',6);self.refused(self.req(fn,'query',payload=settings),'unavailable',6)
        self.plan(sid);require(self.status(fn)[:5]==(0,0,0,0,0),'planned unconfigured state')
        self.refused(self.req(fn,'start',sid),'unavailable',6);self.unchanged(self.req(fn,'stop',sid),'stop in state 0 must do nothing')
        self.unchanged(release(0),'release in state 0 must do nothing');self.refused(release(1),'unavailable',6)
        self.configure(sid,fn,1,1000);require(self.status(fn)[:5]==(1,0,0,0,0),'configure in state 0')
        self.unchanged(self.req(fn,'stop',sid),'stop in state 1 must do nothing');self.unchanged(release(0),'release in state 1 must do nothing')
        g=self.track_start(sid,fn);require(g==1 and self.status(fn)[0]==3,'start in state 1')
        self.refused(self.req(fn,'configure',sid,settings),'unavailable',6);self.refused(self.req(fn,'start',sid),'unavailable',6)
        self.refused(release(0),'unavailable',6);self.unchanged(release(g),'one-shot release must check generation then do nothing')
        self.a.advance_to(self.origin+100000000);require(self.status(fn)[:3]==(4,1,self.full(fn,16)),'one-shot completion')
        self.unchanged(self.req(fn,'stop',sid),'stop in state 4 must keep data');self.unchanged(release(g),'release in state 4 must do nothing')
        g=self.track_start(sid,fn);require(g==2 and self.status(fn)[:3]==(3,0,0) and not self.segments(fn),'start in state 4 clears data')
        self.a.fail(fn,1);require(self.status(fn)[0]==6 and self.status(fn)[5]==b'\1','acquisition error state')
        g=self.track_start(sid,fn);require(g==3 and self.status(fn)[0]==3 and self.status(fn)[5] is None,'start in state 6')
        self.a.fail(fn,2);self.ok(self.req(fn,'stop',sid));require(self.status(fn)[:3]==(1,0,0) and self.status(fn)[4:]==(3,None),'stop in state 6')
        g=self.track_start(sid,fn);self.a.advance_to(self.origin+5000000);self.ok(self.req(fn,'stop',sid))
        rows=self.segments(fn);require(self.status(fn)[0]==1 and len(rows)==1 and rows[0][2]==6 and rows[0][6]==2,'stop in state 3 publishes a short segment')
        self.configure(sid,fn,1,1000);require(self.status(fn)[:5]==(1,0,0,0,g) and not self.segments(fn),'configure in state 1 clears data and keeps generation')
        g=self.track_start(sid,fn);self.a.advance_to(self.origin+100000000);self.configure(sid,fn,1,1000)
        require(self.status(fn)[:5]==(1,0,0,0,g) and not self.segments(fn),'configure in state 4')
        g=self.track_start(sid,fn);self.a.fail(fn,1);self.configure(sid,fn,1,1000);require(self.status(fn)[:5]==(1,0,0,0,g) and self.status(fn)[5] is None,'configure in state 6')
    def stream_error(self,sid,fast,gens):
        store={fn:[] for fn in self.members};at=self.origin+17000000
        self.a.advance_to(at);self.collect(store,at);self.a.fail(fast,2);self.collect(store,at)
        require(self.status()[0]==6 and self.status(fast)[0]==6 and self.status(fast)[5]==b'\2' and self.status(5-fast)[0]==1,'streaming error stops the peer')
        for fn in self.members:
            row=self.status(fn);rows=self.segments(fn);failed=fn==fast
            total=self.stream(fn,store[fn],gens[fn],self.origin,self.rates[fn],row[2],rows,(3,2) if failed else (1,0),exact=not failed)
            if failed:
                ends=[i['position']+len(i['data']) for i in store[fn] if i['kind']=='data']
                require(max(ends)>row[2],'fixture sent part of the failed segment before the error')
                require(total<arrived(1000,17000000),'failed streaming segment published')
            else:require(total==arrived(333,17000000),'stopped streaming peer sample count')
            self.read_rules(fn,gens[fn],row[2],stream=True)
    def scenario(self,fast,kind):
        self.fresh()
        if kind=='GEN-EXHAUST-GROUP':self.a.seed(LAST,5,5)
        if kind=='GEN-EXHAUST-TRACK':self.a.seed(5,LAST if fast==2 else 5,LAST if fast==3 else 5)
        with self.c.holding(1000) as sid:
            if kind.startswith('GEN-EXHAUST'):return self.exhaust(sid,fast,kind)
            if kind=='STATE-TABLE':return self.table(sid,fast)
            if kind.startswith('START-FAIL'):return self.start_failure(sid,fast,kind)
            generation,gens,request,reply=self.prepare(sid,fast,3 if kind=='STREAM-ERROR' else 1);self.origin=self.status()[1];slow=5-fast
            if kind=='STREAM-ERROR':return self.stream_error(sid,fast,gens)
            mixed=kind.endswith('MIXED');retained=kind=='EXPIRY-RETAINED'
            elapsed=100000000 if retained else 20000000 if mixed else 9000000
            self.a.advance_to(self.origin+elapsed);require(self.status()[0]==(4 if retained else 3),'lifecycle phase')
            counts={fn:arrived(self.rates[fn],elapsed,16) for fn in self.members}
            if kind.startswith('QUERY'):
                for fn in self.members:
                    before=self.a.state();p=self.c.success(self.req(fn,'query',payload=self.settings(fn,1,537,5)))
                    require(self.a.state()==before,'active query changed timeline/settings/data');self.answer(fn,p,1,537,5)
                self.a.advance_to(self.origin+100000000);require(self.status()[0]==4,'query disrupted capture')
                for fn in self.members:self.raw(fn,gens[fn],16)
                return
            if kind=='REPLAY-START':
                before=self.a.state();require(self.c.success(request)==reply and self.a.state()==before,'start replay changed asynchronous capture')
                self.a.advance_to(self.origin+100000000);require(self.status()[0]==4,'start replay disrupted capture')
                for fn in self.members:self.raw(fn,gens[fn],16)
                return
            if kind.startswith('END') or kind.startswith('EXPIRY') or kind=='SESSION-FORCE':
                if kind.startswith('END'):self.ok(self.c.request(self.c.core['end'],session=sid));self.c.session=None
                elif kind.startswith('EXPIRY'):
                    self.a.advance_to(self.origin+999000000);require(self.a.state()['bound'],'lease released before deadline')
                    self.a.advance_to(self.origin+1000000000);self.c.session=None;self.released(generation,gens)
                    self.c.success(self.c.request(self.c.core['lock_state']));self.c.session=None
                else:
                    new=secrets.randbelow(0xffffffff)+1
                    while new==sid:new=secrets.randbelow(0xffffffff)+1
                    self.c.corr=0;self.c.success(self.c.request(self.c.core['open'],struct.pack('<IB',1000,1),session=new));self.c.session=new
                self.released(generation,gens);self.stable_advance(self.a.state()['now_ns']+1000000);return
            if kind.startswith('ERROR'):
                failed=slow if kind!='ERROR-FAST' else fast;peer=5-failed
                self.a.fail(failed,2);require(self.status()[0]==6 and self.status(failed)[0]==6 and self.status(peer)[0]==(4 if mixed else 1),'error failed to stop active peer/preserve completed peer')
                require(self.status(failed)[5]==b'\2','track error payload');self.read_rules(failed,gens[failed],0)
                self.raw(failed,gens[failed],0);self.raw(peer,gens[peer],counts[peer])
            else:
                stop=self.req(4,'stop',sid);answer=self.ok(stop);require(self.status()[0]==1,'stop left active group')
                for fn in self.members:
                    require(self.status(fn)[0]==(4 if mixed and fn==fast else 1),'stop changed completed/active track state');self.raw(fn,gens[fn],counts[fn])
                if kind=='REPLAY-STOP':
                    before=self.a.state();require(self.c.success(stop)==answer and self.a.state()==before,'stop replay changed completed partials')
            self.stable_advance(self.origin+100000000)
            new,newgens,_,_=self.start(sid,self.members);require(new==generation+1 and all(newgens[fn]==gens[fn]+1 for fn in gens),'restart did not advance generations')
            for fn in gens:
                require(not self.segments(fn) and self.read(fn,newgens[fn])==(0,0,b''),'restart retained previous data')
                self.refused(self.req(fn,'read',payload=struct.pack('<IQH',gens[fn],0,1)),'unavailable',6)
            self.origin=self.status()[1];self.a.advance_to(self.origin+100000000);require(self.status()[0]==4,'restart completion')
            for fn in gens:self.raw(fn,newgens[fn],16)
    def run(self):
        start=len(self.c.results);self.c.check('OEP-TIMELINE-LIFE-CONTEXT','capture §4; core §7',self.context)
        if self.c.results[-1]['status']!='passed':self.c.abort=True
        for at,args in enumerate(self.scenarios):
            first=len(self.a.trace);self.c.check('OEP-TIMELINE-LIFE-'+str(at),'capture §2.1/3.2/3.4/4.1; core §6/8/9/10',lambda args=args:self.scenario(*args));self.c.results[-1]['adapter_trace']=self.a.trace[first:]
        rows=self.c.results[start:]
        return dict(status='passed' if all(r['status']=='passed' for r in rows) else 'failed',checks=rows,observed=self.c.observed,adapter_trace=self.a.trace,full_conformance=False,
                    scenarios=[dict(fast=fast,case=kind) for fast,kind in self.scenarios],scope='known cross-rate one-shot/streaming cancellation, error, query, replay, restart, generation exhaustion, start failure, state table and ownership loss',
                    unchecked=['repeat/release resumption, force and trigger waits (states 2/5)','true simultaneous requests and error races','other rates/ADC layouts/skew, serial retention bounds','physical timing and production Host'])
