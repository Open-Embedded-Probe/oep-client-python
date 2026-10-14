"""Saved items checked one by one at boot: partial application, unreadable containers and capacity, through current wire ops."""
import struct
from .conformance import require
from .conformance_config import ConfigChecks,item
from .conformance_registered_ops import RegisteredOpsChecks


SAVED=[(2,b'\4\0nrst'),(3,b'\1\0\4'),(7,b'\6\0')]


class ConfigFallbackChecks(ConfigChecks):
    cases=('FORMAT','UNKNOWN','CHANNEL','MODE','LABEL','CONFLICT','DUPLICATE','CAPACITY')
    # (storage_state, reason, the items applied at boot) per perturbation of the saved settings (§3).
    outcomes=dict(FORMAT=(2,1,[]),UNKNOWN=(3,2,SAVED),CHANNEL=(3,2,SAVED),MODE=(3,2,SAVED),LABEL=(3,2,SAVED),
                  CONFLICT=(3,2,[SAVED[0],(7,b'\1\0'),SAVED[2]]),DUPLICATE=(3,2,SAVED[1:]),CAPACITY=(3,3,[(2,b'\0\0pwr'),*SAVED[1:]]))
    def context(self):
        super().context()
        for name in ('prepare','perturb','storage_snapshot'):
            require(callable(getattr(self.a,name,None)),'explicit config fallback controls required')
    def reboot(self):
        old=self.c.boot;fresh=self.a.restart_fixture();self.c.confirm();self.c.identity()
        require(self.c.boot==fresh['boot_id'] and self.c.boot!=old,'fallback requires a new boot')
    def gpio(self,applied):
        # Disabled channels are not driven; the others are parked at their idle (Hi-Z without one); no live plan.
        s=self.a.state()['gpio'];idle={int.from_bytes(v[:2],'little'):v[2] for t,v in applied if t==3}
        off={int.from_bytes(v[:2],'little') for t,v in applied if t==7}
        require(s['plans']=={} and all(s['pins'][ch]==(None if ch in off else idle.get(ch,0)) for ch in range(8)),'boot idle/disable application differs')
        with self.c.holding() as sid:
            for ch in (1,6):
                req=self.c.request(16,item(144,struct.pack('<HBH',3,1,ch)),fn=1,session=sid)
                if ch in off:self.unavailable(req,5,ch)
                else:
                    require(self.c.success(req)==b'','channel not disabled is unusable')
                    require(self.c.success(self.c.request(17,b'\1\3\0',fn=1,session=sid))==b'','release plan')
    def case(self,name):
        fresh=self.a.prepare();self.c.confirm();self.c.identity()
        require(self.c.boot==fresh['boot_id'],'fresh fallback fixture')
        with self.c.holding(60000) as sid:
            g=self.set(sid,*(item(t|128,v) for t,v in SAVED))
            require(self.get(SAVED)==g,'fallback seed set/get generation differs')
            require(self.changed(18,b'',sid)==g,'fallback save generation differs');self.storage(1,g)
        self.observe(name+'-saved')
        self.a.perturb(name);stored=self.a.storage_snapshot();self.reboot()
        state,reason,applied=self.outcomes[name]
        require(self.get(applied)==1,'boot applied other items than the valid saved ones (or generation not 1)');self.storage(state,0,reason)
        self.gpio(applied)
        require(self.a.storage_snapshot()==stored,'boot/normal operations rewrote the saved settings')
        self.observe(name+'-fallback');self.reboot();self.get(applied);self.storage(state,0,reason)
        require(self.a.storage_snapshot()==stored,'second boot rewrote the saved settings')
        with self.c.holding() as sid:
            if name in ('UNKNOWN','DUPLICATE'):
                require(self.c.success(self.op(19,sid=sid))==b'','fallback erase response')
                self.get(applied);self.storage(0,0)
            ch=b'\0\0' if name=='CAPACITY' else b'\4\0'           # within the held capacity: replace, not add
            g=self.set(sid,item(2,ch+b'repair'))
            repaired=sorted([(2,ch+b'repair')]+[r for r in applied if (r[0],r[1][:2])!=(2,ch)],key=lambda r:(r[0],int.from_bytes(r[1][:2],'little')))
            if name not in ('UNKNOWN','DUPLICATE'):
                self.storage(state,0,reason)
                require(self.a.storage_snapshot()==stored,'set overwrote the saved settings before save')
            require(self.changed(18,b'',sid)==g,'fallback repair save generation differs');self.storage(1,g)
        self.reboot();require(self.get(repaired)==1,'fallback repair did not persist');self.storage(1,1)
        self.observe(name+'-repaired')
    def run(self):
        start=len(self.c.results);RegisteredOpsChecks(self.c,self.a.selection).run()
        self.c.check('OEP-CONFIG-FALLBACK-CONTEXT','config §2/3',self.context)
        if self.c.results[-1]['status']!='passed':self.c.abort=True
        for name in self.cases:self.c.check('OEP-CONFIG-FALLBACK-'+name,'config §1/3',lambda name=name:self.case(name))
        rows=self.c.results[start:]
        return dict(status='passed' if all(r['status']=='passed' for r in rows) else 'failed',checks=rows,
                    full_conformance=False,state_observations=self.observations,
                    unchecked=['the order of several reasons together','erase power loss (storage_state 2 after erase)',
                               'save write suppression/concurrency/power loss','physical storage/production implementation'])
