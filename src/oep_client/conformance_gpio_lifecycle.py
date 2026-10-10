"""Core session lifetime checks using explicit logical GPIO/plan resources."""
import secrets
import struct
from .conformance import require, tlvs
from .conformance_gpio import GpioChecks


class GpioLifecycleChecks(GpioChecks):
    cases=('EXPIRY','EXPIRED-SET-REPLAY','EXPIRED-PLAN-REPLAY','ENDED-ID','SAME-ID-OPEN','FORCE','FORCE-OLD','KEEPALIVE','REJECTED-RENEWAL','READ-REPLAY-NONRENEWAL')
    def setup_pins(self,sid):
        plan=self.apply_request(sid,[(2,1,0),(3,1,1)]);tlvs(self.c.success(plan));set_req=self.set_request(sid,[(0,3)]);tlvs(self.c.success(set_req));self.set(sid,[(1,4)],fn=3);return plan,set_req
    def lock(self):
        value=self.c.success(self.c.request(self.c.core['lock_state']));require(len(value)>=5,'lock state header');tlvs(value[5:]);return struct.unpack_from('<BI',value)
    def empty(self):
        state=self.a.state();require(state['holder'] is None and not state['plans'] and state['pins']==state['idle'],'ended session retained GPIO plan or pin driving')
        return state
    def expiry(self,kind):
        with self.c.holding(1000) as sid:
            plan,set_req=self.setup_pins(sid);before=self.a.state();self.a.advance(999);require(self.lock()==(1,1),'GPIO lease expired before boundary');require(self.a.state()['plans']==before['plans'] and self.a.state()['pins']==before['pins'],'GPIO state changed before expiry')
            self.a.advance(1);require(self.lock()==(0,0),'GPIO lease did not expire at boundary');expired=self.empty();self.c.session=None
            if kind in ('EXPIRED-SET-REPLAY','EXPIRED-PLAN-REPLAY'):
                req=set_req if kind=='EXPIRED-SET-REPLAY' else plan;tlvs(self.c.success(req));require(self.a.state()==expired,'historical success resurrected GPIO plan/pin or renewed lease')
            self.c.rejected(self.set_request(sid,[(0,4)]),'no_session');require(self.a.state()==expired,'new expired request touched GPIO')
    def ended_id(self):
        with self.c.holding(1000) as sid:
            self.setup_pins(sid);tlvs(self.c.success(self.c.request(self.c.core['end'],session=sid)));self.c.session=None;ended=self.empty()
            for force in (0,1):
                self.c.rejected(self.c.request(self.c.core['open'],struct.pack('<IB',1000,force),session=sid),'no_session');require(self.a.state()==ended,'ended ID reopened/restored GPIO')
    def same_id(self):
        with self.c.holding(1000) as sid:
            _,set_req=self.setup_pins(sid)
            for force in (0,1):
                before=self.a.state();self.a.advance(100);value=self.c.success(self.c.request(self.c.core['open'],struct.pack('<IB',2000,force),session=sid));require(struct.unpack_from('<II',value)==(2000,self.c.boot),'same ID lease update');tlvs(value[8:]);after=self.a.state()
                require(after['plans']==before['plans'] and after['pins']==before['pins'] and after['events']==before['events'] and after['generation']==before['generation'],'same ID open released/reapplied GPIO resources');require(after['deadline']==after['now_ms']+2000,'same ID open did not update lease');tlvs(self.c.success(set_req));require(self.a.state()==after,'same ID open lost replay history or reexecuted cached GPIO set')
    def force(self,old=False):
        with self.c.holding(1000) as sid:
            _,set_req=self.setup_pins(sid);before=self.a.state();replacement=secrets.randbelow(0xffffffff)+1
            while replacement==sid:replacement=secrets.randbelow(0xffffffff)+1
            self.c.rejected(self.c.request(self.c.core['open'],struct.pack('<IB',1000,0),session=replacement),'locked');require(self.a.state()==before,'nonforce contender altered GPIO')
            self.c.corr=0;value=self.c.success(self.c.request(self.c.core['open'],struct.pack('<IB',1000,1),session=replacement));self.c.session=replacement;require(struct.unpack_from('<II',value)==(1000,self.c.boot),'force open result');tlvs(value[8:]);state=self.a.state();require(state['holder']==replacement and not state['plans'] and state['pins']==state['idle'],'force did not release old GPIO before transfer');require(state['generation']>before['generation'],'force did not invalidate old generation')
            self.apply(replacement,[(3,1,0)]);self.set(replacement,[(0,4)],fn=3)
            if old:
                state=self.a.state();self.c.rejected(set_req,'locked');self.c.rejected(self.set_request(sid,[(0,3)]),'locked');require(self.a.state()==state,'old ID accessed or renewed new GPIO plan')
                tlvs(self.c.success(self.c.request(self.c.core['end'],session=replacement)));self.c.session=None;state=self.empty();self.c.rejected(set_req,'no_session');require(self.a.state()==state,'old cache survived replacement session history')
    def renewed(self,rejected=False):
        with self.c.holding(1000) as sid:
            self.setup_pins(sid);before=self.a.state();self.a.advance(800)
            if rejected:self.c.rejected(self.set_request(sid,[(7,3)]),'unavailable')
            else:tlvs(self.c.success(self.c.request(self.c.core['keepalive'],session=sid)))
            after=self.a.state();require(after['deadline']==after['now_ms']+1000 and after['plans']==before['plans'] and after['pins']==before['pins'] and after['events']==before['events'],'keepalive/rejected completion did not renew without touching GPIO')
            self.a.advance(999);require(self.lock()==(1,1),'renewed GPIO session expired early');self.a.advance(1);require(self.lock()==(0,0),'renewed GPIO session expiry');self.empty();self.c.session=None
    def nonrenewal(self):
        with self.c.holding(1000) as sid:
            _,set_req=self.setup_pins(sid);before=self.a.state();self.a.advance(800);self.read([0]);tlvs(self.c.success(set_req));after=self.a.state();require(after['deadline']==before['deadline'] and after['plans']==before['plans'] and after['pins']==before['pins'] and after['events']==before['events'],'SID 0 read or set replay renewed/reexecuted GPIO');self.a.advance(200);require(self.lock()==(0,0),'nonrenewing queries delayed GPIO expiry');self.empty();self.c.session=None
    def run(self):
        start=len(self.c.results)
        def context():
            require(self.a.reset_scope=='logical-model' and self.a.selection=={1:'oep.probe.plan',2:'oep.fixture.gpio',3:'oep.fixture.gpio'},'explicit logical GPIO clock selection required')
            specs={s['name']:s for s in self.c.reg['interface']};require(all(specs[n]['revision']==1 for n in self.a.selection.values()),'selected revision differs')
            self.codes={fn:{r['name']:r['code'] for r in specs[name]['op']} for fn,name in self.a.selection.items()};self.assignment=specs['oep.probe.plan']['tlv']['plan_apply']['role_assignment']
            self.c.confirm();self.c.identity();self.c.declarations();entries=self.c.list_snapshot();require(all(any(r['fn']==fn and r['name']==name and r['revision']==1 for r in entries) for fn,name in self.a.selection.items()),'selected fn identity differs');require(self.a.state()['clock_scope']=='explicit-logical-ms','explicit clock required')
        self.c.check('CORE-GPIO-LIFECYCLE-CONTEXT','core §5.2/6/8/9; logical fixture',context)
        if self.c.results[-1]['status']!='passed':self.c.abort=True
        for kind in self.cases:
            action=(lambda kind=kind:self.expiry(kind)) if kind.startswith('EXPIR') else self.ended_id if kind=='ENDED-ID' else self.same_id if kind=='SAME-ID-OPEN' else (lambda:self.force(False)) if kind=='FORCE' else (lambda:self.force(True)) if kind=='FORCE-OLD' else (lambda:self.renewed(False)) if kind=='KEEPALIVE' else (lambda:self.renewed(True)) if kind=='REJECTED-RENEWAL' else self.nonrenewal
            def isolated(action=action):self.fresh();action()
            first=len(self.a.trace);self.c.check('CORE-GPIO-LIFECYCLE-'+kind,'core §5.2/6/8/9; GPIO plan resources',isolated);self.c.results[-1]['adapter_trace']=self.a.trace[first:]
        rows=self.c.results[start:]
        return dict(status='passed' if all(r['status']=='passed' for r in rows) else 'failed',checks=rows,observed=self.c.observed,adapter_trace=self.a.trace,full_conformance=False,scope='core session ownership/expiry/replay with explicit logical GPIO plan resources',unchecked=['physical pin transition timing and persisted config idle/disable','deferred writer/force concurrency with these GPIO resources','other fixture plan role constraints/channel_group','production Host and other standard operation behaviors'])
