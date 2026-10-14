"""Port-speed session release and stale deferred response checks."""
import struct
from .conformance import require,tlvs
from .conformance_port_speed import PortSpeedChecks


class PortSpeedLifecycleChecks(PortSpeedChecks):
    cases=('FORCE-TRIAL','FORCE-COMMITTED','EXPIRY-TRIAL','EXPIRY-COMMITTED','EXPIRED-TRY-WRITE','EXPIRED-COMMIT-WRITE',
           'EXPIRY-PENDING','EXPIRY-PENDING-OLD','EXPIRY-PENDING-OPEN')

    def case(self,name):
        self.a.select(self.c,0)
        old=self.c.boot;fresh=self.a.restart_fixture();self.c.confirm();self.c.identity()
        require(fresh['boot_id']==self.c.boot!=old,'fresh logical boot required')
        with self.c.holding(1000) as sid:
            try:
                self.a.seed(self.c,sid)
                if name.startswith('EXPIRY-PENDING'):return self.expiry_pending(name,sid)
                if name=='EXPIRED-TRY-WRITE':self.prepared(self.request(sid,verify=5000),230400)
                else:
                    self.trial(sid,5000)
                    if name in ('FORCE-COMMITTED','EXPIRY-COMMITTED'):self.commit(sid)
                    if name=='EXPIRED-COMMIT-WRITE':self.prepared(self.request(sid,step=1),230400)
                if name.startswith('FORCE'):
                    new=1 if sid!=1 else 2
                    req=self.c.request(self.c.core['open'],struct.pack('<IB',1000,1),session=new,corr=1)
                    original=self.c.send;self.c.send=self.a.begin
                    try:p=self.c.success(req)
                    finally:self.c.send=original
                    # The successful forced open already replaced the owner;
                    # cleanup must use that owner even if the following check fails.
                    self.c.session,self.c.corr=new,1
                    require(len(p)>=8 and struct.unpack_from('<II',p)==(1000,self.c.boot),'force open response differs');tlvs(p[8:])
                    self.speed(3,230400)
                    state=self.observe('force-prepared')['protected']
                    require(state['ownership']['holder']==new and state['plans']=={},'force did not replace owner/release plan')
                    self.a.finish();self.speed(0,115200)
                    require(self.a.events()[-1]['baud']==230400,'force response not sent at old baud');self.phase(0,115200)
                    self.c.rejected(self.c.request(self.c.core['keepalive'],session=sid,corr=2),'locked')
                else:
                    before=self.observe('before-expiry')
                    self.a.advance(999)
                    state=self.observe('before-expiry-boundary')
                    require(state['protected']['ownership']['holder']==sid,'lease expired early')
                    require(state['ports']==before['ports'],'speed changed before lease boundary')
                    self.a.advance(1)
                    # A commit answer still being written keeps the raised baud until its boundary.
                    self.speed(3,230400) if name=='EXPIRED-COMMIT-WRITE' else self.speed(0,115200)
                    state=self.observe('expired')['protected']
                    require(state['ownership']['holder'] is None and state['plans']=={},'expiry retained owner/plan')
                    self.c.session=None
                    if name.startswith('EXPIRED-'):
                        self.a.finish();self.speed(0,115200)
                        state=self.observe('stale-write-complete')['protected']
                        require(state['ownership']['holder'] is None and state['plans']=={},'stale response resurrected owner/plan')
                        self.a.advance(1);self.speed(0,115200)
                    self.phase(0,115200)
                    self.c.rejected(self.c.request(self.c.core['keepalive'],session=sid),'no_session')
            finally:
                if self.a.speed()['pending']:self.a.finish()
                # Preserve a boundary violation if the observed owner expired:
                # cleanup must not hide it with a second no_session rejection.
                if self.a.state()['ownership']['holder'] is None:self.c.session=None
        self.speed(0,115200)

    def expiry_pending(self,name,sid):
        # The lease ends while the port's next frame is arriving: a session
        # return waits for that frame's boundary and nothing cancels it.
        self.trial(sid,5000);self.commit(sid);self.straddle(1000)
        require(self.observe('expired')['protected']['ownership']['holder'] is None,'expiry retained owner');self.c.session=None
        if name=='EXPIRY-PENDING':require(self.during(self.state_request())[:5]==struct.pack('<IB',230400,3),'port_state did not report phase3')
        elif name=='EXPIRY-PENDING-OLD':self.during(self.request(sid,step=2,verify=0),'no_session')
        else:
            new=1 if sid!=1 else 2
            p=self.during(self.c.request(self.c.core['open'],struct.pack('<IB',1000,0),session=new,corr=1))
            self.c.session,self.c.corr=new,1
            require(len(p)>=8 and struct.unpack_from('<II',p)==(1000,self.c.boot),'open response differs');tlvs(p[8:])
        self.speed(0,115200);self.phase(0,115200)

    def run(self):
        start=len(self.c.results)
        self.c.check('OEP-PORT-SPEED-LIFECYCLE-CONTEXT','link §3; core §6',self.context)
        if self.c.results[-1]['status']!='passed':self.c.abort=True
        for name in self.cases:self.c.check('OEP-PORT-SPEED-'+name,'link §3; core §6',lambda name=name:self.case(name))
        checks=self.c.results[start:]
        return dict(status='passed' if all(r['status']=='passed' for r in checks) else 'failed',checks=checks,full_conformance=False,
                    state_observations=self.observations,write_events=self.a.events(),
                    unchecked=['execution-time idle pause','simultaneous inflight writes and force','Host speed-switch/wait','physical transport'])
