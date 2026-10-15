"""Restart response completion, quiet routes and volatile boot-state checks."""
import struct
from .conformance import require,tlvs,ops
from .conformance_registered_ops import RegisteredOpsChecks


class RestartChecks:
    cases=('BARRIER','LOCK','TLV')
    def __init__(self,checks,adapter):self.c,self.a=checks,adapter;self.observations=[]
    def observe(self,action):
        s=self.a.state();self.observations.append(dict(action=action,state=s));return s
    def request(self,sid,tail=b''):return self.c.request(16,tail,fn=4,session=sid)
    def context(self):
        require(self.a.reset_scope=='logical-model' and self.a.restart_fn==4 and self.a.routes==(0,1,2) and self.a.restart_delay_ms==50,'explicit logical restart routes required')
        for name in ('begin','finish','advance','raw','events','seed','restart_fixture'):require(callable(getattr(self.a,name,None)),'explicit restart/write controls required')
        self.c.confirm();self.c.identity()
        spec=next(r for r in self.c.reg['interface'] if r['name']=='oep.probe.restart')
        require(spec['revision']==1 and spec['op'][0]['name']=='restart' and spec['op'][0]['code']==16,'reviewed restart numbers differ')
        desc=dict(reversed(self.c.describe(4)));require(16 in ops(desc[7]) and desc[64]==struct.pack('<I',50),'restart ops/max_ms50 declaration differs')
    def refused(self,request,reason):
        before=self.a.state();payload=self.c.rejected(request,reason)
        after=self.a.state()
        # Core reply caching/lease can change; boot/phase/resources cannot.
        for k in ('boot_id','pending','restarting','return_at','holder','gpio'):require(before[k]==after[k],'refused restart changed '+k)
        return payload
    def restart(self,sid,tail=b''):
        before=self.observe('before-write');req=self.request(sid,tail)
        original=self.c.send;self.c.send=self.a.begin
        try:require(self.c.success(req)==b'','restart success payload differs')
        finally:self.c.send=original
        prepared=self.observe('prepared')
        require(prepared['boot_id']==before['boot_id'] and not prepared['restarting'] and prepared['holder']==sid and prepared['gpio']==before['gpio'],'restart ran before response completion')
        self.a.advance(9);held=self.observe('writer-wait')
        require(held['boot_id']==before['boot_id'] and held['gpio']==before['gpio'] and not held['restarting'],'write wait restarted Probe')
        self.a.finish();self.c.session=None;sent=self.observe('sent')
        require(sent['restarting'] and sent['boot_id']==before['boot_id'],'restart phase not after response')
        require(self.a.events()[-1]['action']=='sent' and self.a.events()[-1]['boot_id']==before['boot_id'],'restart reply sent after boot')
        self.a.advance(49)
        for route in self.a.routes:
            for probe in (self.c.request(self.c.core['confirm'],b'OEP?\1\1'),req):
                require(self.a.raw(probe,route) is None,'request answered while restarting')
        self.observe('quiet-before-boundary');self.a.advance(1);boot=self.observe('boot-complete')
        require(boot['boot_id']!=before['boot_id'] and not boot['restarting'] and boot['holder'] is None and not boot['cache_keys'],'restart retained boot/session/cache')
        require(boot['gpio']['plans']=={} and boot['gpio']['pins']==boot['gpio']['idle'],'restart retained plan/pin drive')
        self.c.confirm();self.c.identity();require(self.c.boot==boot['boot_id'],'new confirm boot differs')
        clock=self.c.success(self.c.request(self.c.core['clock']))
        # the new boot's clock counts from that boot (core §2.7), which began after the answer went out (restart §2): any
        # uptime up to the time since then, not one exact value (the probe may count its own start-up)
        uptime=int.from_bytes(clock[4:12],'little') if len(clock)>=12 else None
        require(len(clock)>=12 and int.from_bytes(clock[:4],'little')==boot['boot_id'] and uptime<=(boot['time_ms']-sent['time_ms'])*1000000,'new boot clock identity/uptime differs');tlvs(clock[12:])
        for route in self.a.routes:
            answer=self.a.raw(self.c.request(self.c.core['confirm'],b'OEP?\1\1'),route)
            require(answer is not None and answer[3:5]==b'\1\0' and int.from_bytes(answer[17:21],'little')==boot['boot_id'],'route did not return by declared bound')
        self.c.rejected(req,'no_session')
    def case(self,name):
        old=self.c.boot;fresh=self.a.restart_fixture();self.c.confirm();self.c.identity()
        require(fresh['boot_id']==self.c.boot!=old,'fresh logical boot required')
        self.refused(self.request(0),'session_required')
        with self.c.holding(60000) as sid:
            try:
                self.a.seed(self.c,sid)
                if name=='LOCK':
                    other=1 if sid!=1 else 2;self.refused(self.request(other),'locked')
                elif name=='TLV':
                    for tail in (b'\0\0\0',b'\x80\0\0',b'\x7f',b'\x7f\2\0x'):self.refused(self.request(sid,tail),'malformed')
                    payload=self.refused(self.request(sid,b'\xff\0\0'),'unsupported');require(payload[:1]==b'\xff','restart critical tag not echoed');tlvs(payload[1:])   # tag(u8), [TLV] (core §4.3)
                    self.restart(sid,b'\x7f\0\0')
                else:self.restart(sid)
            finally:
                if self.a.state()['pending']:self.a.finish()
                if self.a.state()['restarting']:self.c.session=None;self.a.advance(50)
                if self.a.state()['holder'] is None:self.c.session=None
    def run(self):
        start=len(self.c.results);RegisteredOpsChecks(self.c,self.a.selection).run()
        self.c.check('OEP-RESTART-CONTEXT','restart §1/2; core §6',self.context)
        if self.c.results[-1]['status']!='passed':self.c.abort=True
        for name in self.cases:self.c.check('OEP-RESTART-'+name,'restart §2; core §9',lambda name=name:self.case(name))
        rows=self.c.results[start:]
        return dict(status='passed' if all(r['status']=='passed' for r in rows) else 'failed',checks=rows,full_conformance=False,
                    state_observations=self.observations,write_events=self.a.events() if callable(getattr(self.a,'events',None)) else [],
                    unchecked=['target halt/reset state','saved/unsaved settings and fallback','port_speed boot baud','notifications and true concurrent routes','physical transport/re-enumeration timing'])
