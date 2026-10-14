"""Whole old/new config recovery at every explicit storage write boundary."""
from .conformance import require,tlvs
from .conformance_config import ConfigChecks,item
from .conformance_registered_ops import RegisteredOpsChecks


OLDER=[(1,b'\2\0\1\2\0'),(2,b'\4\0seed'),(3,b'\1\0\1\0'),(7,b'\6\0')]
OLD=[(1,b'\2\0\1\3\0'),(2,b'\4\0old'),(3,b'\1\0\1\0'),(7,b'\6\0')]
NEW=[(1,b'\2\0\1\4\0'),(2,b'\4\0new-value'),(3,b'\1\0\2\0'),(7,b'\7\0')]


class ConfigPowerLossChecks(ConfigChecks):
    cases=('FIRST','REPLACE','REUSE','ACK')
    def __init__(self,c,a):super().__init__(c,a);self.cuts=[]
    def context(self):
        super().context()
        for name in ('prepare','write_steps','cut_save','power_cycle'):require(callable(getattr(self.a,name,None)),'explicit storage power-loss controls required')
        require('core' in self.a.state(),'explicit volatile Core observation required')
    def set_all(self,items,sid):return self.hashed(17,b''.join(item(t|128,v) for t,v in items),sid)
    def seed(self,name):
        fresh=self.a.prepare();self.c.confirm();self.c.identity();require(self.c.boot==fresh['boot_id'],'fresh power-loss fixture')
        old_hash=None
        with self.c.holding(60000) as sid:
            if name=='REUSE':
                h=self.set_all(OLDER,sid);require(self.hashed(18,b'',sid)==h,'older save hash')
            if name!='FIRST':
                old_hash=self.set_all(OLD,sid);require(self.hashed(18,b'',sid)==old_hash,'old save hash');self.storage(1,old_hash)
        return old_hash
    def candidate(self,sid):
        # Whole replacement removes the old disable key as well as replacing per-key items.
        self.hashed(20,b'\1\2\7\6\0',sid)
        return self.set_all(NEW,sid)
    def fresh_core(self):
        require(self.a.state()['core']==dict(holder=None,last=None,cache_keys=[]),'power loss retained old session/history/cache')
    def case(self,name):
        if name=='ACK':
            self.seed(name)
            with self.c.holding() as sid:
                h=self.candidate(sid);req=self.op(18,sid=sid)
                reply=self.c.success(req);require(len(reply)>=4 and int.from_bytes(reply[:4],'little')==h,'acknowledged save hash differs');tlvs(reply[4:]);self.storage(1,h)
                boot=self.c.boot;event=self.a.power_cycle();self.c.session=None;self.fresh_core()
                self.c.confirm();self.c.identity();require(self.c.boot!=boot and self.c.boot==event['boot_id'],'acknowledged save requires new boot')
                require(self.get(NEW)==h,'successful save lost durable config');self.storage(1,h)
                self.c.rejected(req,'no_session')
                s=self.a.state();require(s['journal_image']==event['image']==event['recovered_image'],'acknowledged recovery rewrote durable bytes')
                require(s['gpio']['plans']=={} and s['gpio']['pins'][1]==2,'acknowledged saved idle/preset differs')
                self.cuts.append(dict(case=name,cut=None,total=None,outcome='new',hash=h,event=event,state=s))
            self.observe(name+'-final');return
        self.seed(name)
        with self.c.holding() as sid:self.candidate(sid);total=self.a.write_steps()
        require(type(total)is int and 1<=total<=1024,'explicit bounded journal write steps required')
        for step in range(total+1):
            old_hash=self.seed(name)
            with self.c.holding(60000) as sid:
                self.a.seed(self.c,sid);new_hash=self.candidate(sid);self.get(NEW)
                require(self.a.write_steps()==total,'power-loss fixture boundary count changed')
                before=self.a.state()['gpio'];boot=self.c.boot
                req=self.op(18,sid=sid);event=self.a.cut_save(req,step);self.c.session=None;self.fresh_core()
                require(event['cut']==step and event['total']==total and event['reply_hex'] is None,'save cut control did not interrupt response')
                self.c.confirm();self.c.identity();require(self.c.boot!=boot and self.c.boot==event['boot_id'],'power loss did not discard old boot')
                p=self.c.success(self.op(16,b'\0\0'));require(len(p)>=5 and p[0]==0,'power-loss config page shape')
                rows=tlvs(p[5:]);h=int.from_bytes(p[1:5],'little')
                previous=[] if name=='FIRST' else OLD
                require(rows==previous or rows==NEW,'power loss restored a partial/mixed/lost config')
                require(step!=total or rows==NEW,'completed durable record did not survive power loss')
                if rows==NEW:require(h==new_hash,'new recovered hash differs');self.storage(1,h);mode=2;outcome='new'
                elif name=='FIRST':self.storage(0,0);mode=0;outcome='absent'
                else:require(h==old_hash,'old recovered hash differs');self.storage(1,h);mode=1;outcome='old'
                self.c.rejected(req,'no_session')
                state=self.a.state()
                require(state['gpio']['plans']=={} and state['gpio']['pins'][1]==mode,'recovered idle/preset or old live resources differ')
                require(before['plans']=={2:[1]} and before['pins'][1]==4,'power-loss live fixture not seeded')
                require(state['journal_image']==event['image']==event['recovered_image'],'boot rewrote durable bytes')
                # Recovery remains stable on another boot; no unfinished write is resumed.
                fresh=self.a.restart_fixture();self.c.confirm();self.c.identity()
                require(self.c.boot==fresh['boot_id'] and self.get(rows)==h,'second boot resumed partial save or lost committed config')
                self.storage(0 if outcome=='absent' else 1,0 if outcome=='absent' else h)
                require(self.a.state()['journal_image']==state['journal_image'],'second boot rewrote durable bytes')
                self.cuts.append(dict(case=name,cut=step,total=total,outcome=outcome,hash=h,event=event,state=state))
        self.observe(name+'-final')
    def run(self):
        start=len(self.c.results);RegisteredOpsChecks(self.c,self.a.selection).run()
        self.c.check('OEP-CONFIG-POWER-LOSS-CONTEXT','config §2/3',self.context)
        if self.c.results[-1]['status']!='passed':self.c.abort=True
        for name in self.cases:self.c.check('OEP-CONFIG-POWER-LOSS-'+name,'config §2',lambda name=name:self.case(name))
        rows=self.c.results[start:]
        return dict(status='passed' if all(r['status']=='passed' for r in rows) else 'failed',checks=rows,full_conformance=False,
                    state_observations=self.observations,cut_observations=self.cuts,
                    unchecked=['save execution timing/exclusivity','physical flash erase/program tear,wear and persistence',
                               'arbitrary corruption/counter wrap/storage serialization','all config items and production implementation'])
