"""Save busy-input suppression, execution-only lease pause and writer timing."""
import struct
from .conformance import require,tlvs
from .conformance_config import ConfigChecks,item
from .conformance_registered_ops import RegisteredOpsChecks


class ConfigExecutionChecks(ConfigChecks):
    cases=tuple(f'RUN-{d}-{lease}' for d in (1500,5000) for lease in (1000,60000))+('EXPIRE-1500','EXPIRE-5000','SAME','TLV','CAPACITY')
    def context(self):
        super().context()
        for name in ('prepare','arm','advance','begin','finish'):require(callable(getattr(self.a,name,None)),'explicit save execution controls required')
        require(dict(self.c.describe(0))[77]==struct.pack('<I',5000),'explicit max_op_ms5000 profile required')
    def probes(self):
        requests=[self.c.request(self.c.core['confirm'],b'OEP?\1\1'),self.c.request(self.c.core['list'],b'\0\0'),
                  self.c.request(self.c.core['describe'],b'\0\0\0\0'),self.c.request(self.c.core['clock']),self.c.request(self.c.core['lock_state']),
                  self.op(16,b'\0\0'),self.op(21)]
        return [(route,req) for route in (0,1,2) for req in requests]
    def buffered(self,request,reason=None):
        send=self.c.send;self.c.send=self.a.begin
        try:return self.c.success(request) if reason is None else self.c.rejected(request,reason)
        finally:self.c.send=send
    def case(self,name):
        fresh=self.a.prepare();self.c.confirm();self.c.identity();require(self.c.boot==fresh['boot_id'],'fresh execution fixture')
        duration=1500;lease=1000
        if name.startswith('RUN-'):_,d,l=name.split('-');duration=int(d);lease=int(l)
        elif name.startswith('EXPIRE-'):duration=int(name.split('-')[1])
        with self.c.holding(lease) as sid:
            try:
                self.a.seed(self.c,sid);g=self.set(sid,item(2,b'\4\0nrst'),item(3,b'\1\0\1'))
                if name=='SAME':require(self.changed(18,b'',sid)==g,'initial save generation')
                if name=='CAPACITY':g=self.set(sid,item(2,b'\2\0'+b'a'*32),item(2,b'\3\0'+b'b'*32))
                self.a.advance(400);start=self.observe(name+'-start');probes=self.probes();self.a.arm(duration,probes)
                req=self.op(18,item(255,b'') if name=='TLV' else b'',sid)
                p=self.buffered(req,'unsupported' if name=='TLV' else 'unavailable' if name=='CAPACITY' else None)
                held=self.observe(name+'-prepared');writing=name.startswith(('RUN-','EXPIRE-'))
                elapsed=duration if writing else 0
                require(held['time_ms']==start['time_ms']+elapsed,'save execution elapsed time differs')
                require(held['core']['deadline']==start['core']['deadline']+elapsed,'save execution consumed or renewed lease')
                require(held['core']['holder']==sid and held['gpio']==start['gpio'],'save execution changed ownership/live resources')
                require(held['writes']==start['writes']+int(writing),'save execution backend calls differ')
                suppressed=[e for e in held['events'] if e['action']=='input-result']
                require([(e['route'],bytes.fromhex(e['request_hex'])) for e in suppressed]==(probes if writing else []),'save busy requests were answered or lost from observation')
                require(all(e['reply_hex'] is None for e in suppressed),'save answered other requests while writing')
                if name=='TLV':require(p==b'\xff','save unsupported raw tag differs')
                elif name=='CAPACITY':require(dict(tlvs(p)).get(1)==b'\3','save capacity cause differs')
                else:
                    require(len(p)>=4 and int.from_bytes(p[:4],'little')==g,'prepared save generation differs');tlvs(p[4:])
                if name.startswith('EXPIRE-'):
                    self.a.advance(599);require(self.a.state()['core']['holder']==sid,'writer lease expired early')
                    self.a.advance(1);expired=self.observe(name+'-expired')
                    require(expired['core']['holder'] is None and expired['gpio']['plans']=={} and expired['gpio']['pins'][1]==1,'writer wait did not expire lease/resources')
                    self.a.finish();self.c.session=None
                    require(self.a.state()['core']['holder'] is None,'stale writer token revived session')
                    count=self.a.state()['writes'];self.c.success(req);require(self.a.state()['writes']==count and self.a.state()['core']['holder'] is None,'save replay reexecuted or renewed expired session')
                else:
                    wait=lease-401;self.a.advance(wait);waiting=self.observe(name+'-waiting')
                    require(waiting['core']==held['core'] and waiting['gpio']==held['gpio'],'writer wait paused/renewed timer or resources')
                    self.a.finish();sent=self.observe(name+'-sent')
                    require(sent['core']['deadline']==sent['time_ms']+lease,'save response completion did not renew lease')
                    self.a.advance(lease-1);require(self.a.state()['core']['holder']==sid,'renewed save lease expired early')
                    self.a.advance(1);self.c.session=None
                    require(self.a.state()['core']['holder'] is None and self.a.state()['gpio']['plans']=={},'renewed save lease did not expire at boundary')
                self.observe(name+'-final')
            finally:
                if self.a.state()['pending']:self.a.finish()
                if self.a.state()['core']['holder'] is None:self.c.session=None
    def run(self):
        start=len(self.c.results);RegisteredOpsChecks(self.c,self.a.selection).run()
        self.c.check('OEP-CONFIG-EXECUTION-CONTEXT','config §2; core §6.1/7.5',self.context)
        if self.c.results[-1]['status']!='passed':self.c.abort=True
        for name in self.cases:self.c.check('OEP-CONFIG-EXECUTION-'+name,'config §2; core §6.1',lambda name=name:self.case(name))
        rows=self.c.results[start:]
        return dict(status='passed' if all(r['status']=='passed' for r in rows) else 'failed',checks=rows,full_conformance=False,state_observations=self.observations,
                    unchecked=['actual wall-clock Host timeout and physical flash','queued input behavior after save and actual concurrent transports',
                               'force/open races during pending response','erase execution timing','production implementation'])
