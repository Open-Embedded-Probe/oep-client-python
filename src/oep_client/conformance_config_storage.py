"""Save suppression, identity-aware equality and storage capacity boundaries."""
import struct
from .conformance import require,tlvs
from .conformance_config import ConfigChecks,item
from .conformance_registered_ops import RegisteredOpsChecks


SEED=[(1,b'\2\0\1\3\0'),(2,b'\4\0nrst'),(3,b'\1\0\1\0')]


class ConfigStorageChecks(ConfigChecks):
    cases=('SAME','EDIT-RETURN','CHANGE','ERASE','LIMIT-63','LIMIT-64','LIMIT-65','REMAP','IDENTITY')
    def context(self):
        super().context()
        for name in ('prepare','storage_snapshot','change_catalog'):require(callable(getattr(self.a,name,None)),'explicit config storage controls required')
        require('backend' in self.a.state(),'actual config storage backend observation required')
    def writes(self,n):require(self.a.state()['backend']['write_count']==n,'config backend write count differs')
    def reboot(self):
        old=self.c.boot;blob=self.a.storage_snapshot();count=self.a.state()['backend']['write_count']
        fresh=self.a.restart_fixture();self.c.confirm();self.c.identity()
        require(self.c.boot==fresh['boot_id'] and self.c.boot!=old,'storage requires new boot')
        self.writes(count);require(self.a.storage_snapshot()==blob,'boot rewrote saved backend')
    def save(self,sid,h,n):
        live=self.a.state()['gpio']
        require(self.hashed(18,b'',sid)==h,'storage save/current hash differs');self.storage(1,h);self.writes(n)
        require(self.a.state()['gpio']==live,'save changed live GPIO/plan')
    def case(self,name):
        fresh=self.a.prepare();self.c.confirm();self.c.identity();require(self.c.boot==fresh['boot_id'],'fresh storage fixture')
        expected=SEED
        with self.c.holding(60000) as sid:
            self.a.seed(self.c,sid);h=self.hashed(17,b''.join(item(t|128,v) for t,v in SEED),sid)
            self.get(SEED);self.save(sid,h,1);blob=self.a.storage_snapshot();self.observe(name+'-saved')
            if name=='SAME':
                # Replay the actual save request, then distinct corr values of the same op.
                req=next(bytes.fromhex(row['request_hex']) for row in reversed(self.c.trace) if bytes.fromhex(row['request_hex'])[3:6]==b'\4\0\x12')
                require(int.from_bytes(self.c.success(req)[:4],'little')==h,'save replay response differs');self.writes(1)
                self.save(sid,h,1);self.save(sid,h,1)
            elif name=='EDIT-RETURN':
                changed=self.hashed(17,item(130,b'\4\0other'),sid);require(changed!=h,'edit did not change hash');self.storage(1,h);self.writes(1)
                require(self.hashed(17,b''.join(item(t,v) for t,v in reversed(SEED)),sid)==h,'return-to-saved hash differs')
                self.save(sid,h,1)
            elif name=='CHANGE':
                expected=[SEED[0],(2,b'\4\0changed'),SEED[2]]
                changed=self.hashed(17,item(130,expected[1][1]),sid);require(changed!=h,'changed content hash unchanged');h=changed
                self.save(sid,h,2);self.save(sid,h,2)
            elif name=='ERASE':
                before=self.a.state()['items'];require(self.c.success(self.op(19,sid=sid))==b'','erase response')
                self.storage(0,0);require(self.a.state()['items']==before and self.a.storage_snapshot() is None,'erase changed current config or retained backend blob')
                self.writes(1);self.save(sid,h,2)
            elif name.startswith('LIMIT-'):
                size=int(name[-2:]);short=size-57
                # plan8 + label37 + label(5+short) + idle7 = requested byte capacity.
                candidate=[SEED[0],(2,b'\4\0'+b'a'*32),(2,b'\5\0'+b'b'*short),SEED[2]]
                require(sum(3+len(v) for t,v in candidate)==size,'capacity fixture byte arithmetic')
                changed=self.hashed(17,item(130,candidate[1][1])+item(130,candidate[2][1]),sid);before=self.a.state()
                if size>64:
                    p=self.c.rejected(self.op(18,sid=sid),'unavailable')
                    require(p==b'\1\1\0\3','save capacity cause differs')
                    require(self.a.state()==before and self.a.storage_snapshot()==blob,'oversize save changed current/storage/resources/backend')
                    self.storage(1,h);self.writes(1)
                else:expected=candidate;h=changed;self.save(sid,h,2)
            elif name in ('REMAP','IDENTITY'):
                pass
            if name not in ('REMAP','IDENTITY'):
                if name in ('SAME','EDIT-RETURN'):require(self.a.storage_snapshot()==blob,'same semantic content rewrote saved representation')
                self.observe(name+'-checked')
        if name in ('REMAP','IDENTITY'):
            self.a.change_catalog(name);self.reboot();self.writes(1)
            with self.c.holding() as sid:
                if name=='REMAP':
                    expected=[(1,b'\3\0\1\3\0'),*SEED[1:]];h=self.get(expected);self.storage(1,h)
                    self.save(sid,h,1);require(self.a.storage_snapshot()==blob,'renumbering identical interface caused storage rewrite')
                else:
                    self.get([])
                    p=self.c.success(self.op(21));require(p[:6]==struct.pack('<BIB',2,0,2),'revision change did not invalidate saved identity');tlvs(p[6:])
                    new_hash=self.hashed(17,b''.join(item(t,v) for t,v in SEED),sid)
                    require(new_hash==h,'same fn bytes must have same fixture hash');self.save(sid,h,2)
                    require(self.a.state()['backend']['blob']['identities']=={'2':['oep.fixture.gpio',0,2]},'save kept old interface revision')
                self.observe(name+'-checked')
        self.reboot();require(self.get(expected)==h,'storage restart returned wrong committed content');self.storage(1,h)
        self.observe(name+'-restored')
        if name=='SAME':
            with self.c.holding() as sid:self.save(sid,h,1)
            self.observe(name+'-new-session')
    def run(self):
        start=len(self.c.results);RegisteredOpsChecks(self.c,self.a.selection).run()
        self.c.check('OEP-CONFIG-STORAGE-CONTEXT','config §2/3/4',self.context)
        if self.c.results[-1]['status']!='passed':self.c.abort=True
        for name in self.cases:self.c.check('OEP-CONFIG-STORAGE-'+name,'config §2/3',lambda name=name:self.case(name))
        rows=self.c.results[start:]
        return dict(status='passed' if all(r['status']=='passed' for r in rows) else 'failed',checks=rows,full_conformance=False,
                    state_observations=self.observations,
                    unchecked=['flash wear and physical storage','save execution/exclusivity/power loss','all config items/slot-uart references',
                               'arbitrary catalogs and production Host integration'])
