"""Saved identity resolution and whole-config fallback through current wire ops."""
import struct
from .conformance import require,tlvs
from .conformance_config import ConfigChecks,item
from .conformance_registered_ops import RegisteredOpsChecks


class ConfigFallbackChecks(ConfigChecks):
    cases=('FORMAT','MISSING','NAME','INSTANCE','REVISION','APPLY','REMAP','UNRELATED')
    reasons=dict(FORMAT=1,MISSING=2,NAME=2,INSTANCE=2,REVISION=2,APPLY=3,REMAP=0,UNRELATED=0)
    def context(self):
        super().context()
        for name in ('prepare','perturb','storage_snapshot'):
            require(callable(getattr(self.a,name,None)),'explicit config fallback controls required')
    def storage(self,state,h,reason=0):
        p=self.c.success(self.op(21))
        require(len(p)>=6 and p[:6]==struct.pack('<BIB',state,h,reason),'fallback storage state/hash/reason differ')
        tlvs(p[6:])
    def reboot(self):
        old=self.c.boot;fresh=self.a.restart_fixture();self.c.confirm();self.c.identity()
        require(self.c.boot==fresh['boot_id'] and self.c.boot!=old,'fallback requires a new boot')
    def case(self,name):
        fresh=self.a.prepare();self.c.confirm();self.c.identity()
        require(self.c.boot==fresh['boot_id'],'fresh fallback fixture')
        expected=[(1,b'\2\0\1\3\0'),(2,b'\4\0nrst'),(3,b'\1\0\4\0'),(7,b'\6\0')]
        with self.c.holding(60000) as sid:
            h=self.hashed(17,b''.join(item(t|128,v) for t,v in expected),sid)
            require(self.get(expected)==h,'fallback seed set/get hash differs')
            require(self.hashed(18,b'',sid)==h,'fallback save hash differs');self.storage(1,h)
        self.observe(name+'-saved')
        self.a.perturb(name);stored=self.a.storage_snapshot();self.reboot()
        catalog=self.c.list_snapshot()
        # Compare advertised tuples independently of the private saved representation.
        identities={row['fn']:(row['name'],row['instance'],row['revision']) for row in catalog}
        target=next((fn for fn,identity in identities.items() if identity==('oep.fixture.gpio',0,1)),None)
        reason=self.reasons[name]
        if name in ('MISSING','NAME','INSTANCE','REVISION'):require(target is None,'missing identity fixture differs')
        if reason:
            self.get([]);self.storage(2,0,reason)
            s=self.a.state()
            require(s['gpio']['plans']=={} and all(pin==0 for pin in s['gpio']['pins'].values()),
                    'unreadable config partially applied idle/plan')
            # An unrelated, healthy interface must be usable: stale disable must not survive.
            with self.c.holding() as sid:
                require(self.c.success(self.c.request(16,item(144,b'\3\0\1\6\0'),fn=1,session=sid))==b'',
                        'unreadable config partially applied disable')
                require(self.c.success(self.c.request(16,b'\1\6\0\4',fn=3,session=sid))==b'',
                        'fallback GPIO unavailable')
            require(self.a.storage_snapshot()==stored,'boot/normal operations rewrote unreadable storage')
            self.observe(name+'-fallback');self.reboot();self.get([]);self.storage(2,0,reason)
            require(self.a.storage_snapshot()==stored,'second boot rewrote unreadable storage')
            with self.c.holding() as sid:
                erased=name in ('FORMAT','NAME','REVISION')
                if erased:
                    require(self.c.success(self.op(19,sid=sid))==b'','fallback erase response')
                    self.get([]);self.storage(0,0)
                h=self.hashed(17,item(2,b'\4\0repair'),sid)
                if not erased:
                    self.storage(2,0,reason)
                    require(self.a.storage_snapshot()==stored,'set overwrote unreadable storage before save')
                require(self.hashed(18,b'',sid)==h,'fallback repair save hash differs');self.storage(1,h)
            self.reboot();require(self.get([(2,b'\4\0repair')])==h,'fallback repair did not persist');self.storage(1,h)
            self.observe(name+'-repaired')
        else:
            require(target==(3 if name=='REMAP' else 2),'catalog identity fixture differs')
            mapped=[(1,struct.pack('<HBH',target,1,3)),*expected[1:]]
            current=self.get(mapped);self.storage(1,current)
            if name=='REMAP':require(current!=h,'remapped current config hash unchanged')
            require(self.a.storage_snapshot()==stored,'successful boot rewrote saved identifiers')
            s=self.a.state();require(s['gpio']['plans']=={} and s['gpio']['pins'][1]==4,
                                   'restored preset executed or idle missing')
            self.observe(name+'-restored');self.reboot()
            require(self.get(mapped)==current,'second boot did not resolve saved identity');self.storage(1,current)
            require(self.a.storage_snapshot()==stored,'second boot rewrote saved identifiers')
            self.observe(name+'-second-boot')
    def run(self):
        start=len(self.c.results);RegisteredOpsChecks(self.c,self.a.selection).run()
        self.c.check('OEP-CONFIG-FALLBACK-CONTEXT','config §2/3',self.context)
        if self.c.results[-1]['status']!='passed':self.c.abort=True
        for name in self.cases:self.c.check('OEP-CONFIG-FALLBACK-'+name,'config §2/3',lambda name=name:self.case(name))
        rows=self.c.results[start:]
        return dict(status='passed' if all(r['status']=='passed' for r in rows) else 'failed',checks=rows,
                    full_conformance=False,state_observations=self.observations,
                    unchecked=['slot/uart identity references and arbitrary catalogs','all config items/get paging',
                               'save write suppression/concurrency/power loss','physical storage/production implementation'])
