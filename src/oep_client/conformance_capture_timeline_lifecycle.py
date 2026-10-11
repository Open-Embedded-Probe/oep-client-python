"""Selected-SPEC asynchronous group cancellation, replay and ownership checks."""
import secrets
import struct
from fractions import Fraction
from .conformance import require,tlvs
from .conformance_capture_timeline import CaptureTimelineChecks
from .conformance_capture_group import NO_TIME


class CaptureTimelineLifecycleChecks(CaptureTimelineChecks):
    cases=('STOP-WAIT','STOP-ACTIVE','STOP-MIXED','ERROR-SOURCE','ERROR-PEER',
           'REPLAY-START-WAIT','REPLAY-START-ACTIVE','REPLAY-STOP',
           'QUERY-WAIT','QUERY-ACTIVE','QUERY-MIXED','END-WAIT','END-ACTIVE',
           'EXPIRY-WAIT','EXPIRY-RETAINED','SESSION-FORCE')
    scenarios=tuple((source,kind) for kind in cases for source in (2,3))
    def context(self):
        super().context()
        self.cause_tag=self.c.reg['core']['tlv']['unavailable_payload']['cause'];self.fn_tag=self.c.reg['core']['tlv']['unavailable_payload']['fn']
    def prepare(self,sid,source,kind):
        self.plan(sid);self.config={}
        for fn in (source,5-source):
            rate=1000 if fn==source else 333
            p=self.c.success(self.req(fn,'configure',sid,self.settings(fn,rate=rate,samples=16,trigger=(3,3,1500) if fn==2 else (2,0,0),pre=2) if fn==source else self.settings(fn,rate=rate,samples=16)))
            actual=dict(tlvs(p));require(actual[self.answers[fn]['actual_rate']]==struct.pack('<II',rate,1),'lifecycle explicit rate')
            if fn==2:self.config[fn]=self.payload.configure(p,roles=(1,3),frontends={0,1},mode=1,max_op_ms=self.maximum)
            self.a.program(fn,3 if fn==source and kind!='EXPIRY-WAIT' else None)
        self.bind(sid,(source,5-source),source)
        return self.start(sid,(source,5-source))
    def raw(self,fn,generation,source,count):
        rows=self.segments(fn)
        if not count:
            require(not rows and self.read(fn,generation)==(0,0,b''),'empty/failed acquisition published partial data')
            require(self.status(fn)[0][1:3]==(0,0),'empty/failed serial_done/write_pos');return
        require(len(rows)==1,'cancelled group segment count')
        row=rows[0];rate=1000 if fn==source else 333;pre=2 if fn==source else 1;first=1 if fn==source else 0
        expected_start=self.origin+int(Fraction(first*1000000000,rate))
        require(row[0:3]==(0,0,count) and row[3]==expected_start and row[5]==pre and row[6]==(2 if count<16 else 0) and row[7]==generation,'partial segment geometry/generation/short')
        position,flags,data=self.read(fn,generation);require(position==0 and flags==0,'cancelled read bounds')
        samples=[self.values(fn,i,3 if fn==source else None) for i in range(first,first+count)]
        if fn==2:self.payload.data(data,self.config[fn],samples)
        else:
            require(len(data)==(count*3+7)//8,'partial logic bytes')
            for i,values in enumerate(samples):
                for bit,v in enumerate(values):require((data[(i*3+bit)//8]>>((i*3+bit)%8))&1==v,'partial logic samples')
        require(self.status(fn)[0][1:3]==(1,len(data)),'serial_done/write_pos after cancellation')
    def stable_advance(self,ns):
        before=self.a.state();self.a.advance_to(ns);after=self.a.state()
        require(after['tracks']==before['tracks'] and after['timeline']==before['timeline'],'stopped acquisition continued sampling')
    def released(self,generation,gens):
        state=self.a.state();require(not state['bound'] and not state['plans'] and not state['timeline'] and state['pins']==state['idle'],'ownership loss retained group/cache/plans/pins')
        require(state['generation']==generation and all(t['state']==0 and not t['config'] and not t['data'] and not t['current'] and not t['history'] and not t['segments'] and t['generation']==gens[fn] for fn,t in state['tracks'].items()),'ownership loss retained capture/reset generation')
    def scenario(self,source,kind):
        self.fresh()
        with self.c.holding(1000) as sid:
            generation,gens,request,reply=self.prepare(sid,source,kind);self.origin=self.status()[0][1]
            waiting=kind.endswith('WAIT');mixed=kind.endswith('MIXED');retained=kind=='EXPIRY-RETAINED'
            elapsed=2000000 if waiting else 100000000 if retained else 20000000 if mixed else 9000000
            self.a.advance_to(self.origin+elapsed)
            status=self.status()[0];require(status[0]==(2 if waiting else 4 if retained else 3),'lifecycle phase')
            require(status[2]==(NO_TIME if waiting else self.origin+3000000),'lifecycle trigger observation')
            if kind.startswith('QUERY'):
                for fn in (source,5-source):
                    before=self.a.state();p=self.c.success(self.req(fn,'query',payload=self.settings(fn,rate=537,samples=5)))
                    require(self.a.state()==before,'active query changed timeline/settings/data')
                    if fn==2:self.payload.configure(p,roles=(1,3),frontends={0,1},mode=1,max_op_ms=self.maximum)
                    values=dict(tlvs(p));require(values[self.answers[fn]['actual_rate']]==struct.pack('<II',537,1) and values[self.answers[fn]['actual_samples']]==struct.pack('<I',5),'query did not answer proposed settings')
                self.a.advance_to(self.origin+100000000);require(self.status()[0][0]==4,'query disrupted capture');self.raw(source,gens[source],source,16);self.raw(5-source,gens[5-source],source,16);return
            if kind.startswith('REPLAY-START'):
                before=self.a.state();require(self.c.success(request)==reply and self.a.state()==before,'start replay changed asynchronous capture')
                self.a.advance_to(self.origin+100000000);require(self.status()[0][0]==4,'start replay disrupted capture');self.raw(source,gens[source],source,16);self.raw(5-source,gens[5-source],source,16);return
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
            counts={source:0 if waiting else 16 if mixed else 9,5-source:0 if waiting else 7 if mixed else 3}
            if kind.startswith('ERROR'):
                failed=source if kind=='ERROR-SOURCE' else 5-source;peer=5-failed
                self.a.fail(failed,2);require(self.status()[0][0]==6 and self.status(failed)[0][0]==6 and self.status(peer)[0][0]==1,'error failed to stop peer')
                require(dict(tlvs(self.c.success(self.req(failed,'status'))[18:])).get(1)==b'\2','track error payload')
                self.raw(failed,gens[failed],source,0);self.raw(peer,gens[peer],source,counts[peer])
            else:
                stop=self.req(4,'stop',sid);answer=self.ok(stop)
                require(self.status()[0][0]==1,'stop left active group')
                for fn in (source,5-source):
                    require(self.status(fn)[0][0]==(4 if mixed and fn==source else 1),'stop changed completed/active track state');self.raw(fn,gens[fn],source,counts[fn])
                if kind=='REPLAY-STOP':
                    before=self.a.state();require(self.c.success(stop)==answer and self.a.state()==before,'stop replay changed completed partials')
            self.stable_advance(self.origin+100000000)
            new,newgens,_,_=self.start(sid,(source,5-source));require(new==generation+1 and all(newgens[fn]==gens[fn]+1 for fn in gens),'restart did not advance generations')
            for fn in gens:
                require(not self.segments(fn) and self.read(fn,newgens[fn])==(0,0,b''),'restart retained previous data')
                self.refused(self.req(fn,'read',payload=struct.pack('<IQI',gens[fn],0,1)),'unavailable',6)
            self.origin=self.status()[0][1];self.a.advance_to(self.origin+100000000)
            require(self.status()[0][0]==4 and self.status()[0][2]==self.origin+3000000,'restart completion/trigger clock')
            for fn in gens:self.raw(fn,newgens[fn],source,16)
    def run(self):
        start=len(self.c.results);self.c.check('OEP-TIMELINE-LIFE-CONTEXT','capture §4; core §7',self.context)
        if self.c.results[-1]['status']!='passed':self.c.abort=True
        for at,args in enumerate(self.scenarios):
            first=len(self.a.trace);self.c.check('OEP-TIMELINE-LIFE-'+str(at),'capture §3.2/4.1; core §6/8/9',lambda args=args:self.scenario(*args));self.c.results[-1]['adapter_trace']=self.a.trace[first:]
        rows=self.c.results[start:]
        return dict(status='passed' if all(r['status']=='passed' for r in rows) else 'failed',checks=rows,observed=self.c.observed,adapter_trace=self.a.trace,full_conformance=False,
                    scenarios=[dict(source=source,case=kind) for source,kind in self.scenarios],scope='known cross-rate one-shot cancellation/query/replay/restart and ownership loss',
                    unchecked=['notifications/repeat/streaming/other group resources','true simultaneous requests and force/error races','other rates/ADC layouts/skew, serial retention bounds','physical timing and production Host'])
