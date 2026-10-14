"""Independent logical ambiguous-delivery and failed-wire rest checks."""
from .conformance import require
from .conformance_debug import DebugChecks


class RetryChecks(DebugChecks):
    def packets(self,parent):return [r for r in self.a.state()['packets'] if r['parent']==parent]
    def host(self,kind,status=2):
        self.fresh()
        with self.c.holding(3000) as sid:
            parent,_=self.attach(sid);self.a.registers(parent,{4:17})
            self.a.plan(parent,[status,0]);step=self.read(4) if kind=='READ' else self.write_reg(0x10 if kind=='RESUME' else 4,0x40000000 if kind=='RESUME' else 42)
            req,result=self.dmi(sid,parent,[step,self.write_reg(5,99)],0,status,[])
            state=self.a.state();rows=self.packets(parent)
            require(len(rows)==1 and rows[0]['status']==status,'ambiguous host DMI step was retried')
            require(state['plans'][parent]==[0] and state['targets'][parent]['registers'].get(5)==None,'failed step consumed retry or suffix')
            if kind!='READ' and status==2:require(state['targets'][parent]['registers'][0x10 if kind=='RESUME' else 4]==(0x40000000 if kind=='RESUME' else 42),'fixture did not represent ambiguous delivered write')
            before=self.a.state();require(self.c.exchange(req)==result and self.a.state()==before,'failed result replay repeated wire activity')
            require(parent in state['connections'],'status line/fault alone closed connection')
    def busy(self,kind,exhaust=False):
        self.fresh()
        with self.c.holding(3000) as sid:
            parent,_=self.attach(sid);self.a.registers(parent,{4:17});self.a.plan(parent,[1,1 if exhaust else 0,0])
            step=self.read(4) if kind=='READ' else self.write_reg(4,42)
            self.dmi(sid,parent,[step],0 if exhaust else 1,1 if exhaust else 0,[] if exhaust or kind=='WRITE' else [17])
            rows=self.packets(parent);state=self.a.state()
            require(len(rows)==2 and rows[0]['status']==1 and not rows[0]['delivered'],'busy was delivered or not retried')
            require(state['plans'][parent]==[0],'retry budget was exceeded')
            require(state['targets'][parent]['registers'][4]==(42 if kind=='WRITE' and not exhaust else 17),'busy write mutated target')
    def owned(self,idle,exhaust=False):
        self.fresh()
        with self.c.holding(3000) as sid:
            parent,_=self.attach(sid);self.a.idle(parent,idle);self.a.plan(parent,[2,2 if exhaust else 0,0]);before=self.a.state()
            result=self.a.probe_read(parent);after=self.a.state();rows=self.packets(parent)
            require(result==dict(status=2 if exhaust else 0,value=None if exhaust else 0xc00),'probe-owned read outcome')
            require(len(rows)==2 and rows[0]['speed']==before['connections'][parent]['speed'] and rows[1]['speed']<rows[0]['speed'],'logical slow retry absent')
            require(after['connections']==before['connections'] and after['targets']==before['targets'],'retry changed connection speed or target state')
            require(all(p['mode']=='released' and p['pull'] in (None,1-idle if i==1 else 1) for i,p in enumerate(rows[0]['rest'])),'failed exchange kept driving or wrong pull')
            if exhaust:require(all(p['mode']=='released' for p in rows[-1]['rest']),'failed retry restored drive')
            else:require([p['level'] for p in rows[-1]['rest']]==[1,1-idle] and all(p['mode']=='wire' for p in rows[-1]['rest']),'successful retry did not restore idle levels')
            require(after['plans'][parent]==[0] and parent in after['connections'],'retry exceeded budget or cut without selected policy')
    def progress(self):
        self.fresh()
        with self.c.holding(3000) as sid:
            parent,_=self.attach(sid);self.a.plan(parent,[0,2,0]);self.dmi(sid,parent,[self.write_reg(4,17),self.read(4),self.write_reg(5,99)],1,2,[])
            require(len(self.packets(parent))==2 and self.a.state()['targets'][parent]['registers'].get(5)==None,'partial operation repeated failed step/suffix')
    def isolated(self):
        self.fresh()
        with self.c.holding(3000) as sid:
            parent,_=self.attach(sid);other,_=self.attach(sid,pair=self.a.pairs[1][1]);self.a.plan(parent,[2]);self.a.registers(other,{4:123})
            self.dmi(sid,parent,[self.write_reg(4,42)],0,2,[]);before=self.a.state()
            self.dmi(sid,other,[self.read(4)],1,0,[123]);after=self.a.state()
            require(after['levels'][parent]==before['levels'][parent] and after['targets'][parent]==before['targets'][parent],'other connection repaired or changed failed connection')
    def invalid(self):
        self.fresh()
        with self.c.holding(3000) as sid:
            parent,_=self.attach(sid);self.a.plan(parent,[2,0]);self.refused(self.dmi_request(sid,parent,[self.write_reg(4,42),b'\xff']),'unsupported',b'\0')
    def run(self):
        start=len(self.c.results)
        def context():
            require(self.a.reset_scope=='logical-model' and self.a.reference,'explicit logical retry fixture required')
            selection=dict(self.a.selection);selection[5]='oep.target.riscv-dm'
            self.specs={fn:next(s for s in self.c.reg['interface'] if s['name']==name and s['revision']==1) for fn,name in selection.items()}
            self.operations={fn:{r['name']:r['code'] for r in spec['op']} for fn,spec in self.specs.items()}
            require(self.c.reg['status']==dict(ok=0,wait=1,line=2,fault=3,timeout=4,state=5),'selected status meanings differ')
            require(self.specs[5]['enum']['dmi_step']==dict(write=1,read=2,poll_reads=3,wait_us=4,poll_us=5),'selected DMI grammar differs')
            self.c.confirm();self.c.identity();self.c.declarations();state=self.a.state();require(state['retry_limit']==2 and state['cut_policy']=='retain','selected fixture policy differs')
        self.c.check('OEP-RETRY-CONTEXT','debug §2/4.1',context)
        if self.c.results[-1]['status']!='passed':self.c.abort=True
        cases=[(kind,lambda kind=kind:self.host(kind)) for kind in ('READ','WRITE','RESUME')]+[('FAULT',lambda:self.host('WRITE',3))]
        cases += [('BUSY-'+kind,lambda kind=kind:self.busy(kind)) for kind in ('READ','WRITE')]+[('BUSY-EXHAUST',lambda:self.busy('WRITE',True))]
        cases += [('OWNED-'+str(idle),lambda idle=idle:self.owned(idle)) for idle in (0,1)]+[('OWNED-EXHAUST',lambda:self.owned(1,True)),('PROGRESS',self.progress),('ISOLATED',self.isolated),('PREFLIGHT',self.invalid)]
        for name,case in cases:
            first=len(self.a.trace);self.c.check('OEP-RETRY-'+name,'debug §2/4.1; core §5.2',case);self.c.results[-1]['adapter_trace']=self.a.trace[first:]
        rows=self.c.results[start:]
        return dict(status='passed' if all(r['status']=='passed' for r in rows) else 'failed',checks=rows,adapter_trace=self.a.trace,observed=self.c.observed,full_conformance=False,scope='logical host DMI ambiguous delivery and probe-owned read retry',unchecked=['wire-cut response/write/release ordering and console link-lost','physical packet drive, pull and timing','resync/TO/framing marks','poll/wait DMI failure injection and store-trigger writes','other retry budgets and production Host integration'])
