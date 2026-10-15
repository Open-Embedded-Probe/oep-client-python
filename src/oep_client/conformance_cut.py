"""Independent request response-before-release and console line-loss release checks."""
import struct
from .conformance import require
from .conformance_debug import DebugChecks


class CutChecks(DebugChecks):
    def prepare(self,sid,halt=False):
        parent,_=self.attach(sid);rid,_=self.console(sid,parent,2)
        if halt:self.c.success(self.request(5,'halt',sid,struct.pack('<I',parent)))
        return parent,rid
    def failed(self,sid,parent):
        self.a.plan(parent,[2]);return self.dmi(sid,parent,[self.write_reg(4,42)],0,2,[])
    def verify_closed(self,before,after,parent,rid):
        require(parent not in after['connections'] and rid not in after['streams'] and rid not in after['protocols'] and rid not in after['mailboxes'],'cut did not release parent/child/protocol/mailbox')
        require(after['closed_targets'][parent]==before['targets'][parent],'cut changed target/halt state')
        require(all(after['freed_pins'][p]==dict(mode='free',level=None,pull=None) for p in before['connections'][parent]['pins'] if p!=65535),'closed pins retain idle drive')
        events=after['lifecycle'];child=next(i for i,r in enumerate(events) if r[:2]==('stream-close',rid));connection=next(i for i,r in enumerate(events) if r[:2]==('connection-close',parent));pins=next(i for i,r in enumerate(events) if r[:2]==('pins-free',parent))
        require(child<connection<pins,'dependency release order')
    def request_cut(self,halt=False,partial=False):
        self.fresh()
        with self.c.holding(3000) as sid:
            parent,rid=self.prepare(sid,halt);self.failed(sid,parent)
            self.a.plan(parent,[0,2] if partial else [2])
            # A successful operation clears the streak; a successful step inside
            # one partial operation does not. The fixture counts operations.
            steps=([self.write_reg(5,17)] if partial else [])+[self.write_reg(4,42)]
            req=self.dmi_request(sid,parent,steps);accepted=self.a.accept(req)
            try:
                held=self.a.state();require(accepted==dict(ready=True,corr=struct.unpack_from('<H',req,1)[0]),'writer gate metadata')
                require(held['writer_waiting'] and parent in held['pending'] and parent in held['connections'] and rid in held['streams'],'closed before response writer')
                require(not any(r[:2]==('connection-close',parent) for r in held['lifecycle']),'early connection close event')
            finally:frame=self.c.exchange(req)
            after=self.a.state();done=1 if partial else 0
            require(frame[3:5]==bytes((1,2 if partial else 1)) and struct.unpack_from('<HBH',frame,5)==(done,2,0),'cut operation result')
            self.verify_closed(held,after,parent,rid)
            rows=after['lifecycle'];write=next(i for i,r in enumerate(rows) if r[:2]==('response-write',accepted['corr']));close=next(i for i,r in enumerate(rows) if r[:2]==('stream-close',rid));require(write<close,'response was written after child close')
            require(not any(r[0]=='mark' for r in rows),'line loss left a mark on a released stream')
            packets=after['packets'];targets=after['closed_targets'];require(self.c.replayed(req,frame,frame=True),'cached cut response differs')
            replay=self.a.state();require(replay['packets']==packets and replay['closed_targets']==targets and replay['connections']==after['connections'],'cached cut response reexecuted or reopened')
            self.refused(self.dmi_request(sid,parent,[self.read(4)]),'no_resource')
    def console_cut(self,halt=False):
        self.fresh()
        with self.c.holding(3000) as sid:
            parent,rid=self.prepare(sid,halt);self.a.plan(parent,[2,2]);require(self.a.poll_cut(rid)==2,'first console line status')
            before=self.a.state();require(parent in before['connections'] and rid in before['streams'],'single line operation closed console')
            self.a.plan(parent,[2,2]);require(self.a.poll_cut(rid)==2,'second console line status');after=self.a.state();self.verify_closed(before,after,parent,rid)
            require(not any(r[0]=='mark' for r in after['lifecycle']),'line loss left a mark nobody can read (debug §2)')
            self.refused(self.request(4,'read',0,struct.pack('<IBQH',rid,1,0,16)),'no_resource')   # the stream is gone
    def recovery(self):
        self.fresh()
        with self.c.holding(3000) as sid:
            parent,rid=self.prepare(sid);self.failed(sid,parent);self.dmi(sid,parent,[self.read(4)],1,0,[42]);self.failed(sid,parent)
            state=self.a.state();require(parent in state['connections'] and rid in state['streams'] and state['streak'][parent]==1,'successful operation did not clear loss history')
            before=self.a.state();self.a.tick();require(self.a.state()==before,'timer-only loss decision')
    def isolated(self):
        self.fresh()
        with self.c.holding(3000) as sid:
            parent,rid=self.prepare(sid);other,_=self.attach(sid,pair=self.a.pairs[1][1]);child,_=self.console(sid,other,2);before=self.a.state()
            self.failed(sid,parent);self.failed(sid,parent);after=self.a.state()
            require(other in after['connections'] and child in after['streams'] and after['targets'][other]==before['targets'][other] and after['streams'][child]==before['streams'][child],'wire cut affected another connection')
    def rejected(self):
        self.fresh()
        with self.c.holding(3000) as sid:
            parent,rid=self.prepare(sid);self.failed(sid,parent);before=self.a.state()
            self.refused(self.dmi_request(sid,parent,[self.write_reg(4,99),b'\xff']),'unsupported',b'\0')
            after=self.a.state();require(after['streak']==before['streak'] and parent in after['connections'] and rid in after['streams'],'rejection decided loss')
    def run(self):
        start=len(self.c.results)
        def context():
            require(self.a.reset_scope=='logical-model' and self.a.reference,'explicit cut fixture required')
            selection=dict(self.a.selection);selection[5]='oep.target.riscv-dm'
            self.specs={fn:next(s for s in self.c.reg['interface'] if s['name']==name and s['revision']==1) for fn,name in selection.items()};self.operations={fn:{r['name']:r['code'] for r in s['op']} for fn,s in self.specs.items()}
            require(self.c.reg['status']==dict(ok=0,wait=1,line=2,fault=3,timeout=4,state=5),'selected status map differs')
            require(self.specs[5]['enum']['dmi_step']==dict(write=1,read=2,poll_reads=3,wait_us=4,poll_us=5),'selected DMI grammar differs')
            self.c.confirm();self.c.identity();self.c.declarations();require(self.a.state()['cut_policy']=='two-line-operations','selected logical cut policy differs')
        self.c.check('OEP-CUT-CONTEXT','debug §2; common §1',context)
        if self.c.results[-1]['status']!='passed':self.c.abort=True
        cases=[('REQUEST',self.request_cut),('REQUEST-HALTED',lambda:self.request_cut(True)),('REQUEST-PARTIAL',lambda:self.request_cut(partial=True)),('CONSOLE',self.console_cut),('CONSOLE-HALTED',lambda:self.console_cut(True)),('RECOVERY',self.recovery),('ISOLATED',self.isolated),('REJECTED',self.rejected)]
        for name,case in cases:
            first=len(self.a.trace);self.c.check('OEP-CUT-'+name,'debug §2; common §1/2; core §5.2/9',case);self.c.results[-1]['adapter_trace']=self.a.trace[first:]
        rows=self.c.results[start:]
        return dict(status='passed' if all(r['status']=='passed' for r in rows) else 'failed',checks=rows,adapter_trace=self.a.trace,observed=self.c.observed,full_conformance=False,scope='logical response writer before request wire-cut; console line loss and dependent release',unchecked=['physical writer/pins and actual transport delivery','writer concurrency, lease expiry while blocked and alternate budgets','resync/TO/framing marks','poll/wait DMI failure injection and store-trigger writes','production Host integration'])
