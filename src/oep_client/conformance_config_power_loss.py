"""Whole old/new config recovery at every explicit storage write boundary."""
from .conformance import require,tlvs
from .conformance_config import ConfigChecks,item,page
from .conformance_registered_ops import RegisteredOpsChecks


OLDER=[(2,b'\4\0seed'),(3,b'\1\0\1'),(7,b'\6\0')]
OLD=[(2,b'\4\0old'),(2,b'\5\0aux'),(3,b'\1\0\1'),(7,b'\6\0')]
NEW=[(2,b'\4\0new-value'),(3,b'\1\0\2'),(7,b'\7\0')]


class ConfigPowerLossChecks(ConfigChecks):
    cases=('FIRST','REPLACE','REUSE','ACK')
    def __init__(self,c,a):super().__init__(c,a);self.cuts=[]
    def context(self):
        super().context()
        for name in ('prepare','write_steps','cut_save','power_cycle'):require(callable(getattr(self.a,name,None)),'explicit storage power-loss controls required')
        require('core' in self.a.state(),'explicit volatile Core observation required')
    def set_all(self,items,sid):return self.set(sid,*(item(t|128,v) for t,v in items))
    def seed(self,name):
        fresh=self.a.prepare();self.c.confirm();self.c.identity();require(self.c.boot==fresh['boot_id'],'fresh power-loss fixture')
        with self.c.holding(60000) as sid:
            if name=='REUSE':
                g=self.set_all(OLDER,sid);require(self.changed(18,b'',sid)==g,'older save generation')
            if name!='FIRST':
                g=self.set_all(OLD,sid);require(self.changed(18,b'',sid)==g,'old save generation');self.storage(1,g)
    def candidate(self,sid):
        # Whole replacement removes the old keys (disable 6, label 5) as well as replacing per-key items.
        self.unset(sid,(7,6),(2,5))
        return self.set_all(NEW,sid)
    def fresh_core(self):
        require(self.a.state()['core']==dict(holder=None,last=None,cache_keys=[]),'power loss retained old session/history/cache')
    def case(self,name):
        if name=='ACK':
            self.seed(name)
            with self.c.holding() as sid:
                g=self.candidate(sid);req=self.op(18,sid=sid)
                reply=self.c.success(req);require(len(reply)>=4 and int.from_bytes(reply[:4],'little')==g,'acknowledged save generation differs');tlvs(reply[4:]);self.storage(1,g)
                boot=self.c.boot;event=self.a.power_cycle();self.c.session=None;self.fresh_core()
                self.c.confirm();self.c.identity();require(self.c.boot!=boot and self.c.boot==event['boot_id'],'acknowledged save requires new boot')
                require(self.get(NEW)==1,'successful save lost durable config');self.storage(1,1)
                self.c.rejected(req,'no_session')
                s=self.a.state();require(s['journal_image']==event['image']==event['recovered_image'],'acknowledged recovery rewrote durable bytes')
                require(s['gpio']['plans']=={} and s['gpio']['pins'][1]==2 and s['gpio']['pins'][7] is None,'acknowledged saved idle/disable differs')
                self.cuts.append(dict(case=name,cut=None,total=None,outcome='new',generation=g,event=event,state=s))
            self.observe(name+'-final');return
        self.seed(name)
        with self.c.holding() as sid:self.candidate(sid);total=self.a.write_steps()
        require(type(total)is int and 1<=total<=1024,'explicit bounded journal write steps required')
        for step in range(total+1):
            self.seed(name)
            with self.c.holding(60000) as sid:
                self.a.seed(self.c,sid);self.candidate(sid);self.get(NEW)
                require(self.a.write_steps()==total,'power-loss fixture boundary count changed')
                before=self.a.state()['gpio'];boot=self.c.boot
                req=self.op(18,sid=sid);event=self.a.cut_save(req,step);self.c.session=None;self.fresh_core()
                require(event['cut']==step and event['total']==total and event['reply_hex'] is None,'save cut control did not interrupt response')
                self.c.confirm();self.c.identity();require(self.c.boot!=boot and self.c.boot==event['boot_id'],'power loss did not discard old boot')
                more,g,rows=page(self.c.success(self.op(16,b'\0\0')));require(more==0,'power-loss config page shape')
                previous=[] if name=='FIRST' else OLD
                require(rows==previous or rows==NEW,'power loss restored a partial/mixed/lost config')
                require(step!=total or rows==NEW,'completed durable record did not survive power loss')
                require(g==1,'recovered boot generation is not 1')
                if rows==NEW:self.storage(1,1);mode=2;off=7;outcome='new'
                elif name=='FIRST':self.storage(0,0);mode=0;off=None;outcome='absent'
                else:self.storage(1,1);mode=1;off=6;outcome='old'
                self.c.rejected(req,'no_session')
                state=self.a.state()
                require(state['gpio']['plans']=={} and state['gpio']['pins'][1]==mode,'recovered idle or old live resources differ')
                require(all((state['gpio']['pins'][ch] is None)==(ch==off) for ch in range(8)),'recovered disable differs')
                require(before['plans']=={2:[1]} and before['pins'][1]==4,'power-loss live fixture not seeded')
                require(state['journal_image']==event['image']==event['recovered_image'],'boot rewrote durable bytes')
                # Recovery remains stable on another boot; no unfinished write is resumed.
                fresh=self.a.restart_fixture();self.c.confirm();self.c.identity()
                require(self.c.boot==fresh['boot_id'] and self.get(rows)==1,'second boot resumed partial save or lost committed config')
                self.storage(0 if outcome=='absent' else 1,0 if outcome=='absent' else 1)
                require(self.a.state()['journal_image']==state['journal_image'],'second boot rewrote durable bytes')
                self.cuts.append(dict(case=name,cut=step,total=total,outcome=outcome,generation=g,event=event,state=state))
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
                               'arbitrary corruption/counter wrap/storage serialization','erase power loss','production implementation'])
