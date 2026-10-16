"""Core lease semantics on standard connection/console dependency resources."""
import struct
from .conformance import require
from .conformance_cut import CutChecks


class WriterLeaseChecks(CutChecks):
    def cleanup_waiter(self,request):
        if self.a.state()['writer_waiting']:self.c.exchange(request)
        if self.a.state()['holder'] is None:self.c.session=None
    def delivery(self,rejected=False,replay_expired=False):
        self.fresh()
        with self.c.holding(1000) as sid:
            parent,rid=self.prepare(sid);old=self.a.state();self.a.advance(400)
            req=self.dmi_request(sid,parent,[b'\x01\x05'] if rejected else [self.write_reg(4,42)])
            self.a.accept(req)
            try:
                # core §6.1 (a09fa45): the lease restarts when the answer is generated (rejected ones too); waiting
                # for the writer and the write's completion change nothing after that
                held=self.a.state();require(held['deadline']==held['now_ms']+1000 and held['holder']==sid and parent in held['connections'],'lease did not restart at response generation, or ownership lost')
                self.a.advance(300);require(self.a.state()['deadline']==held['deadline'],'writer wait paused or renewed lease');frame=self.c.exchange(req);sent=self.a.state()
                require(frame[3:5]==bytes((self.c.reg['resolutions']['rejected'],self.c.reasons['malformed'])) if rejected else frame[3:5]==b'\x01\0','deferred response resolution')
                require(sent['deadline']==held['deadline'] and parent in sent['connections'] and rid in sent['streams'],'writer completion renewed lease')
                if replay_expired:
                    self.a.advance(1000);require(self.c.replayed(req,frame,frame=True),'expired replay lost cached response')
                    after=self.a.state();require(after['holder'] is None and not after['connections'] and not after['streams'],'cached response restored expired ownership')
                else:
                    self.a.advance(100);require(self.c.replayed(req,frame,frame=True),'replay response changed')
                    require(self.a.state()['deadline']==sent['deadline'],'replayed response renewed lease')
            finally:self.cleanup_waiter(req)
    def execution(self):
        self.fresh()
        with self.c.holding(1000) as sid:
            parent,rid=self.prepare(sid);before=self.a.state();self.a.execution(1500)
            req=self.dmi_request(sid,parent,[self.read(4)]);self.a.accept(req)
            try:
                held=self.a.state();require(held['now_ms']==before['now_ms']+1500 and held['deadline']==held['now_ms']+1000 and held['holder']==sid,'execution consumed lease instead of pausing it, or the answer did not restart it')
                self.a.advance(999);self.a.tick();require(parent in self.a.state()['connections'] and rid in self.a.state()['streams'],'lease expired before the deadline its answer set')
                frame=self.c.exchange(req);after=self.a.state();require(frame[3:5]==b'\x01\0' and after['deadline']==held['deadline'],'post-execution writer completion renewed lease')
            finally:self.cleanup_waiter(req)
    def expiry(self,cut=False):
        self.fresh()
        with self.c.holding(1000) as sid:
            parent,rid=self.prepare(sid,halt=True);other,_=self.attach(sid,pair=self.a.pairs[1][1]);child,_=self.console(sid,other,'dmseq')
            if cut:self.failed(sid,parent);self.a.plan(parent,[2])
            req=self.dmi_request(sid,parent,[self.write_reg(4,42)]);self.a.accept(req)
            try:
                held=self.a.state();self.a.advance(held['deadline']-held['now_ms']);self.a.tick();expired=self.a.state()
                require(expired['holder'] is None and expired['generation']>held['generation'] and expired['writer_waiting'],'expiry did not retire owner while retaining queued result')
                require(not expired['connections'] and not expired['streams'] and not expired['protocols'] and not expired['mailboxes'],'expiry left live dependency resources')
                for p,r in ((parent,rid),(other,child)):
                    self.verify_closed(held,expired,p,r)                # released with its buffer: no closed mark (console §2)
                frame=self.c.exchange(req);after=self.a.state();require(frame[3:5]==bytes((1,1 if cut else 0)),'late original result was discarded or changed')
                require(after['holder'] is None and after['generation']==expired['generation'] and not after['connections'] and after['packets']==expired['packets'],'late writer token resurrected owner/resources or reexecuted')
                require(self.c.replayed(req,frame,frame=True) and self.a.state()['holder'] is None,'cached late result resurrected owner')
                self.c.rejected(self.dmi_request(sid,parent,[]),'no_session')
                self.c.session=None
            finally:self.cleanup_waiter(req)
        with self.c.holding(1000) as new:
            require(new!=sid,'new session accidentally reused old identifier')
            replacement,_=self.attach(new);require(replacement>max(parent,rid,other,child),'expired resource IDs were reused')
            self.c.rejected(req,'locked');require(self.a.state()['holder']==new and replacement in self.a.state()['connections'],'old request replaced new ownership')
    def run(self):
        start=len(self.c.results)
        def context():
            require(self.a.reset_scope=='logical-model' and self.a.reference,'explicit logical writer lease fixture required')
            selection=dict(self.a.selection);selection[5]='oep.target.riscv-dm'
            self.specs={fn:next(s for s in self.c.reg['interface'] if s['name']==name and s['revision']==1) for fn,name in selection.items()};self.operations={fn:{r['name']:r['code'] for r in s['op']} for fn,s in self.specs.items()}
            require('closed' not in self.c.reg['common']['enum']['mark_kind'],'selected SPEC still marks released streams')   # released with its buffer (console §2)
            self.c.confirm();self.c.identity();self.c.declarations();state=self.a.state();require(state['clock_scope']=='explicit-logical-ms' and state['max_op_ms']==5000,'selected execution/clock fixture differs')
        self.c.check('CORE-WRITER-LEASE-CONTEXT','core §6.1/9; common §1',context)
        if self.c.results[-1]['status']!='passed':self.c.abort=True
        cases=[('DELIVERY',self.delivery),('REJECTED',lambda:self.delivery(True)),('REPLAY-EXPIRED',lambda:self.delivery(replay_expired=True)),('EXECUTION',self.execution),('EXPIRED',self.expiry),('CUT-EXPIRED',lambda:self.expiry(True))]
        for name,case in cases:
            first=len(self.a.trace);self.c.check('CORE-WRITER-LEASE-'+name,'core §5.2/6.1/9; common §1/2',case);self.c.results[-1]['adapter_trace']=self.a.trace[first:]
        rows=self.c.results[start:]
        return dict(status='passed' if all(r['status']=='passed' for r in rows) else 'failed',checks=rows,adapter_trace=self.a.trace,observed=self.c.observed,full_conformance=False,scope='core lease pause/delivery/expiry with logical standard dependency resources and stale writer token',unchecked=['physical transport completion and wall-clock timing','concurrent writers/force during queued response','other execution budgets/geometries','production Host integration'])
