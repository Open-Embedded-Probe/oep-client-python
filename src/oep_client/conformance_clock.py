"""Core clock sampling, SID-zero isolation, replay exceptions and exact u64 checks."""
import struct
from .conformance import require,tlvs


class ClockChecks:
    def __init__(self,checks,adapter):self.c,self.a=checks,adapter
    def fresh(self):
        boot=self.a.restart_fixture();require(boot['scope']=='logical-model','logical reset scope')
        self.c.confirm();self.c.identity();require(self.c.boot==boot['boot_id'],'clock fixture boot mismatch')
    def read(self, request=None):
        request=request or self.c.request(self.c.core['clock'])
        frame=self.c.exchange(request)
        require(len(frame)>=17 and frame[3:5]==b'\x01\0','clock completed fixed part')
        boot,uptime=struct.unpack_from('<IQ',frame,5);tlvs(frame[17:])
        require(boot==self.c.boot,'clock/confirm boot differs')
        span=self.a.state()['exchanges'][-1]
        require(span['request']==request,'clock exchange witness differs')
        require(span['received_ns']<=uptime<=span['sent_ns'] and uptime in span['reads'],'clock did not return an actual in-exchange source read')
        return request,frame,uptime
    def wide(self):
        self.fresh();previous=0
        for value in (0,1,1_234_567,2**32+123,2**53+31,2**63+47,2**64-2):
            self.a.advance(value-previous);_,_,got=self.read();require(got==value,'explicit u64 clock value lost/truncated/rounded');previous=value
    def fresh_zero(self):
        self.fresh();request=self.c.request(self.c.core['clock'],corr=65535)
        _,_,first=self.read(request);self.a.advance(12345);_,_,second=self.read(request)
        require(second==first+12345,'SID zero clock was cached')
    def point(self):
        self.fresh();self.a.advance(1000);self.a.delays(7,13)
        self.read();self.a.delays(0,0)
    def observer(self):
        self.fresh()
        with self.c.holding(1000) as sid:
            self.c.success(self.c.request(16,b'\x01',fn=1,session=sid));self.a.advance(10_000_000)
            before=self.a.state()['ownership'];self.read(self.c.request(self.c.core['clock'],corr=before['high']))
            require(self.a.state()['ownership']==before,'SID zero clock changed session/lease/replay/resource state')
    def replay(self):
        self.fresh()
        with self.c.holding(1000) as sid:
            self.a.advance(11_000_000);request,frame,_=self.read(self.c.request(self.c.core['clock'],session=sid))
            self.a.advance(100_000_000);before=self.a.state();got=self.c.exchange(request);after=self.a.state()
            require(got==frame,'owned clock replay recomputed the saved response')
            require(after['ownership']==before['ownership'],'clock replay changed history/lease/resources')
    def lease(self):
        self.fresh()
        with self.c.holding(1000):
            self.a.advance(900_000_000);deadline=self.a.state()['ownership']['deadline'];self.read()
            require(self.a.state()['ownership']['deadline']==deadline,'observer clock renewed lease')
            self.a.advance(100_000_000);self.a.tick();self.read()
            require(self.a.state()['ownership']['holder'] is None,'clock observer kept expired lock')
            self.c.session=None
    def boots(self):
        self.fresh()
        for boot in (0,0xffffffff):
            result=self.a.boot(boot);self.c.confirm();self.c.identity();require(result['boot_id']==self.c.boot==boot,'zero/max boot identity')
            _,_,value=self.read();require(value==0,'clock did not restart at boot origin')
            with self.c.holding(1000):self.read()
    def refusals(self):
        self.fresh()
        self.c.rejected(self.c.request(self.c.core['clock'],session=123),'no_session')
        with self.c.holding(1000) as sid:
            for payload,reason in ((b'\x7f\x05\0x','malformed'),(b'\xfe\x01\0x','unsupported')):
                before=self.a.state();self.c.rejected(self.c.request(self.c.core['clock'],payload),reason);after=self.a.state()
                require(after['ownership']==before['ownership'],'rejected observer clock mutated ownership')
            before=self.a.state();self.c.rejected(self.c.request(self.c.core['clock'],session=sid%0xffffffff+1),'locked')
            require(self.a.state()['ownership']==before['ownership'],'foreign clock changed owner')
    def run(self):
        start=len(self.c.results)
        def context():
            require(self.a.reset_scope=='logical-model' and self.a.reference,'explicit logical clock fixture required')
            require(self.c.core['clock']==4,'selected clock operation differs')
            self.c.confirm();self.c.identity();self.c.declarations();require(self.a.state()['scope']=='logical-ns','exact logical ns observations required')
        self.c.check('CORE-CLOCK-CONTEXT','core §2.6a/6.5/7.7',context)
        if self.c.results[-1]['status']!='passed':self.c.abort=True
        cases=[('U64',self.wide),('SIDZERO-FRESH',self.fresh_zero),('READ-POINT',self.point),('SIDZERO-STATE',self.observer),('REPLAY',self.replay),('LEASE',self.lease),('BOOT',self.boots),('REFUSALS',self.refusals)]
        for name,case in cases:
            first=len(self.a.trace);self.c.check('CORE-CLOCK-'+name,'core §2.6a/4.1/5.2/6/7.7',case);self.c.results[-1]['adapter_trace']=self.a.trace[first:]
        rows=self.c.results[start:]
        return dict(status='passed' if all(x['status']=='passed' for x in rows) else 'failed',checks=rows,adapter_trace=self.a.trace,observed=self.c.observed,full_conformance=False,
                    scope='logical exact clock source reads, SID-zero isolation and cached owned replay exception',
                    unchecked=['physical clock accuracy/resolution and actual transport writer bounds','all time-producing OEP functions and deferred/concurrent writers','complete core/common normative audit and production Host integration'])
