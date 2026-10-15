"""Save suppression by contents (not by generation), erase and storage capacity boundaries."""
from .conformance import require,tlvs
from .conformance_config import ConfigChecks,item
from .conformance_registered_ops import RegisteredOpsChecks


SEED=[(2,b'\4\0nrst'),(3,b'\1\0\1'),(7,b'\6\0')]


class ConfigStorageChecks(ConfigChecks):
    cases=('SAME','EDIT-RETURN','CHANGE','ERASE','LIMIT-63','LIMIT-64','LIMIT-65')
    def context(self):
        super().context()
        for name in ('prepare','storage_snapshot'):require(callable(getattr(self.a,name,None)),'explicit config storage controls required')
        require('backend' in self.a.state(),'actual config storage backend observation required')
    def writes(self,n):require(self.a.state()['backend']['write_count']==n,'config backend write count differs')
    def reboot(self):
        old=self.c.boot;blob=self.a.storage_snapshot();count=self.a.state()['backend']['write_count']
        fresh=self.a.restart_fixture();self.c.confirm();self.c.identity()
        require(self.c.boot==fresh['boot_id'] and self.c.boot!=old,'storage requires new boot')
        self.writes(count);require(self.a.storage_snapshot()==blob,'boot rewrote saved backend')
    def save(self,sid,g,n):
        live=self.a.state()['gpio']
        require(self.changed(18,b'',sid)==g,'save/current generation differs');self.storage(1,g);self.writes(n)
        require(self.a.state()['gpio']==live,'save changed live GPIO/plan')
    def case(self,name):
        fresh=self.a.prepare();self.c.confirm();self.c.identity();require(self.c.boot==fresh['boot_id'],'fresh storage fixture')
        expected=SEED
        with self.c.holding(60000) as sid:
            self.a.seed(self.c,sid);g=self.set(sid,*(item(t|128,v) for t,v in SEED))
            self.get(SEED);self.save(sid,g,1);blob=self.a.storage_snapshot();self.observe(name+'-saved')
            if name=='SAME':
                # Replay the actual save request, then distinct corr values of the same op.
                req=next(bytes.fromhex(row['request_hex']) for row in reversed(self.c.trace) if bytes.fromhex(row['request_hex'])[3:6]==b'\4\0\x12')
                require(int.from_bytes(self.c.success(req)[:4],'little')==g,'save replay response differs');self.writes(1)
                self.save(sid,g,1);self.save(sid,g,1)
            elif name=='EDIT-RETURN':
                # The same contents again under a new generation: nothing to write (§3, not by generation alone).
                require(self.set(sid,item(2,b'\4\0other'))==g+1,'edit did not count the generation');self.storage(1,g);self.writes(1)
                require(self.unset(sid,(3,1))==g+2,'unset did not count the generation');self.storage(1,g)
                back=self.set(sid,*(item(t,v) for t,v in reversed(SEED)))
                require(back==g+3 and self.get(SEED)==back,'return-to-saved contents/generation differ');self.storage(1,g)
                self.save(sid,back,1)
            elif name=='CHANGE':
                expected=[(2,b'\4\0changed'),*SEED[1:]]
                changed=self.set(sid,item(130,expected[0][1]));require(changed==g+1,'changed contents did not count the generation');g=changed
                self.save(sid,g,2);self.save(sid,g,2)
            elif name=='ERASE':
                before=self.a.state()['items'];tlvs(self.c.success(self.op(19,sid=sid)))   # erase: no fixed part, [TLV] (§3)
                self.storage(0,0);require(self.a.state()['items']==before and self.a.storage_snapshot() is None,'erase changed current config or retained backend blob')
                self.writes(1);self.save(sid,g,2)
            else:
                size=int(name[-2:]);short=size-53
                # label37 + label(5+short) + idle6 + disable5 = requested byte capacity.
                candidate=[(2,b'\4\0'+b'a'*32),(2,b'\5\0'+b'b'*short),*SEED[1:]]
                require(sum(3+len(v) for t,v in candidate)==size,'capacity fixture byte arithmetic')
                changed=self.set(sid,item(130,candidate[0][1]),item(130,candidate[1][1]));before=self.settled()
                if size>64:
                    self.unavailable(self.op(18,sid=sid),3)
                    require(self.settled()==before and self.a.storage_snapshot()==blob,'oversize save changed current/storage/resources/backend')
                    self.storage(1,g);self.writes(1)
                else:expected=candidate;g=changed;self.save(sid,g,2)
            if name in ('SAME','EDIT-RETURN'):require(self.a.storage_snapshot()==blob,'same contents rewrote saved representation')
            self.observe(name+'-checked')
        self.reboot();require(self.get(expected)==1,'storage restart returned wrong committed content');self.storage(1,1)
        self.observe(name+'-restored')
        if name=='SAME':
            with self.c.holding() as sid:self.save(sid,1,1)
            self.observe(name+'-new-session')
    def run(self):
        start=len(self.c.results);RegisteredOpsChecks(self.c,self.a.selection).run()
        self.c.check('OEP-CONFIG-STORAGE-CONTEXT','config §2/3/4',self.context)
        if self.c.results[-1]['status']!='passed':self.c.abort=True
        for name in self.cases:self.c.check('OEP-CONFIG-STORAGE-'+name,'config §2/3',lambda name=name:self.case(name))
        rows=self.c.results[start:]
        return dict(status='passed' if all(r['status']=='passed' for r in rows) else 'failed',checks=rows,full_conformance=False,
                    state_observations=self.observations,
                    unchecked=['flash wear and physical storage','save execution/exclusivity/power loss','saved item fallback at boot',
                               'production Host integration'])
