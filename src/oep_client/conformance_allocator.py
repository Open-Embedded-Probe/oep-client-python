"""Core resource ID exhaustion, session retention and reboot boundary checks."""
import struct
from .conformance import require,tlvs
from .conformance_resources import ResourceChecks


class AllocatorChecks(ResourceChecks):
    def fresh(self, seed=65535):
        reset=self.a.restart_fixture();require(reset['scope']=='logical-model','logical reset required')
        self.c.confirm();self.c.identity();self.c.declarations();require(self.c.boot==reset['boot_id'],'reset boot differs')
        self.a.seed(seed)
    def exhausted(self, sid, slot):
        before=self.inventory();request=self.a.request('create',sid,slot)
        self.cause(request,2);require(self.inventory()==before,'exhaustion left a live resource')
        return request
    def boundary(self):
        self.fresh(65534)
        with self.c.holding(60000) as sid:
            allocations=[(self.a.slots[0],self.create(sid,self.a.slots[0])[0]),(self.a.slots[-1],self.create(sid,self.a.slots[-1])[0])]
            require([rid for _,rid in allocations]==[65534,65535],'global allocator u16 boundary differs')
            for slot in self.a.slots:self.exhausted(sid,slot)
            for slot,rid in allocations:self.use(sid,slot,rid)
            self.cause(self.a.request('use',sid,self.a.slots[-1],allocations[0][1]),6)
            for slot,rid in allocations:
                self.close(sid,slot,rid);self.c.rejected(self.a.request('use',sid,slot,rid),'no_resource')
                self.exhausted(sid,slot)
            self.empty()
    def ended_boundary(self, expiry=False):
        self.fresh()
        with self.c.holding(1000 if expiry else 60000) as sid:
            slot=self.a.slots[0];rid,created=self.create(sid,slot);require(rid==65535,'last ID differs')
            self.exhausted(sid,slot)
            if expiry:
                self.a.advance(1000);p=self.c.success(self.c.request(self.c.core['lock_state']))
                require(len(p)>=5 and p[:5]==b'\0'*5,'lease boundary did not release lock');tlvs(p[5:])
            else:tlvs(self.c.success(self.c.request(self.c.core['end'],session=sid)))
            self.c.session=None;self.empty()
            require(self.a.decode_created(self.c.success(created))==rid,'historical last-ID response lost')
            self.empty();self.c.rejected(self.a.request('use',sid,slot,rid),'no_session')
        with self.c.holding(60000) as sid:self.exhausted(sid,slot)
    def force_boundary(self):
        self.fresh()
        with self.c.holding(60000) as sid:
            slot=self.a.slots[0];rid,created=self.create(sid,slot);replacement=sid%0xffffffff+1
            self.c.corr=0
            p=self.c.success(self.c.request(self.c.core['open'],struct.pack('<IB',60000,1),session=replacement))
            self.c.session=replacement
            require(len(p)>=8 and struct.unpack_from('<II',p)==(60000,self.c.boot),'force identity');tlvs(p[8:])
            self.empty();self.exhausted(replacement,slot)
            self.c.rejected(self.a.request('use',replacement,slot,rid),'no_resource')
            self.c.rejected(created,'locked')
    def replay_boundary(self):
        self.fresh()
        with self.c.holding(60000) as sid:
            slot=self.a.slots[0];rid,_=self.create(sid,slot)
            request=self.a.request('create',sid,slot);frame=self.c.exchange(request)
            require(frame[3:5]==bytes((0,self.c.reasons['unavailable'])) and dict(reversed(tlvs(frame[5:]))).get(1)==b'\x02','exhaustion cause')
            self.close(sid,slot,rid);before=self.a.state()
            require(self.c.exchange(request)==frame and self.a.state()==before,'failed-create replay executed/changed allocator')
            self.empty();self.exhausted(sid,slot)
    def reboot_boundary(self):
        self.fresh(65534)
        with self.c.holding(60000) as sid:
            slot=self.a.slots[0];rid,created=self.create(sid,slot);self.create(sid,self.a.slots[-1])
            boot=self.c.boot;reset=self.a.restart_fixture();self.c.session=None
            self.c.confirm();self.c.identity();require(self.c.boot==reset['boot_id']!=boot,'reboot identity')
            self.empty();self.c.rejected(created,'no_session')
        with self.c.holding(60000) as sid:
            fresh,_=self.create(sid,slot);require(fresh==1,'new boot did not restart ID space')
            self.c.rejected(self.a.request('use',sid,slot,rid),'no_resource')
    def run(self):
        start=len(self.c.results)
        def context():
            require(self.a.reset_scope=='logical-model' and self.a.reference,'explicit logical allocator fixture required')
            require(self.a.slots==((1,1),(1,2),(2,1),(2,2)),'explicit cross-fn/kind fixture required')
            require(all(callable(getattr(self.a,k,None)) for k in ('restart_fixture','seed','state','advance')),'allocator controls required')
            self.c.confirm();self.c.identity();self.c.declarations()
        self.c.check('CORE-ALLOCATOR-CONTEXT','core §6/9; explicit logical fixture',context)
        if self.c.results[-1]['status']!='passed':self.c.abort=True
        for name,case in [('BOUNDARY',self.boundary),('END',self.ended_boundary),('EXPIRY',lambda:self.ended_boundary(True)),
                          ('FORCE',self.force_boundary),('REPLAY',self.replay_boundary),('REBOOT',self.reboot_boundary)]:
            first=len(self.a.trace);self.c.check('CORE-ALLOCATOR-'+name,'core §5.2/6/9',case);self.c.results[-1]['adapter_trace']=self.a.trace[first:]
        rows=self.c.results[start:]
        return dict(status='passed' if all(x['status']=='passed' for x in rows) else 'failed',checks=rows,adapter_trace=self.a.trace,
                    observed=self.c.observed,full_conformance=False,scope='logical seeded u16 allocation boundary and session/reboot invariants',
                    unchecked=['65535 actual allocations without seed','failed reservations and arbitrary resource geometry',
                               'physical resources and concurrent allocators','complete core/common normative audit'])
