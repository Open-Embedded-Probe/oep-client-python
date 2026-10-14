"""Probe handshake/initial timer checks; Host speed switching is separate."""
import struct
from contextlib import contextmanager
from .conformance import require,tlvs,ops
from .conformance_registered_ops import RegisteredOpsChecks


class PortSpeedChecks:
    cases=('TRY-BARRIER','COMMIT','REVERT-TRIAL','REVERT-COMMITTED','STATES','VERIFY','TOLERANCE','MALFORMED','TLV','REPLAY','IDLE','IDLE-RENEW','IDLE-INVALID','END-BARRIER','NON-BRIDGE','ONE-PORT')
    def __init__(self,checks,adapter):self.c,self.a=checks,adapter;self.observations=[]
    def request(self,sid,baud=230400,step=0,verify=100,tail=b''):return self.c.request(self.op,struct.pack('<IBH',baud,step,verify)+tail,fn=4,session=sid)
    def observe(self,action):
        snapshot=self.a.speed();self.observations.append(dict(action=action,state=snapshot));return snapshot
    def speed(self,state,baud):
        p=self.observe('speed')['ports'][0];require(p['state']==state and p['baud']==baud,'port state/actual baud differs')
    def success(self,request,baud):
        protected=self.a.state();payload=self.c.success(request)
        require(len(payload)>=4 and int.from_bytes(payload[:4],'little')==baud,'port_speed actual response baud differs');tlvs(payload[4:])
        after=self.a.state();self.observations.append(dict(action='success',request_hex=request.hex(),response_hex=payload.hex(),before_protected=protected,after_protected=after))
        require(after==protected,'port_speed changed live plan/pin/ownership')
    def refused(self,request,reason,tag=None):
        before=self.a.speed();payload=self.c.rejected(request,reason)
        if reason=='unavailable':require(payload==b'\1\1\0\6','state refusal must report cause6')
        if reason=='unsupported':require(payload and payload[0]==(0 if tag is None else tag),'unsupported tag/mark differs');tlvs(payload[1:])
        after=self.a.speed();self.observations.append(dict(action='refused',request_hex=request.hex(),before=before,after=after))
        require(after==before,'refused port_speed changed state')
    def trial(self,sid,verify=100):self.success(self.request(sid,verify=verify),230400);self.speed(1,230400)
    def commit(self,sid,verify=0):self.success(self.request(sid,step=1,verify=verify),230400);self.speed(2,230400)
    def context(self):
        require(self.a.route==0 and self.a.port_speed is True,'explicit UART bridge route required')
        for name in ('raw','select','speed','begin','finish','advance','events','seed','restart_fixture'):require(callable(getattr(self.a,name,None)),'explicit write/clock/seed controls required')
        self.c.confirm();self.c.identity();require(self.c.confirm_transport==0,'selected UART route mismatch')
        require(b'\0\1\xff' in [v for t,v in self.c.describe(0) if t==73],'UART bridge transport declaration missing')
        spec=next(s for s in self.c.reg['interface'] if s['name']=='oep.probe.link');self.op=next(o['code'] for o in spec['op'] if o['name']=='port_speed')
        require(self.op==18 and self.c.reg['timing']['port_speed_idle_ms']==3000 and self.c.reg['limits']['port_speed_tolerance_pct']==2,'reviewed port_speed numbers/timing differ')
        require(18 in ops(dict(reversed(self.c.describe(4)))[7]),'port_speed declaration missing')
    def prepared(self,request,baud):
        original=self.c.send;self.c.send=self.a.begin
        try:self.success(request,baud)
        finally:self.c.send=original
    @contextmanager
    def holding(self):
        with self.c.holding(60000) as sid:
            try:yield sid
            finally:
                # Complete an inspected logical write before cleanup end, even
                # when an assertion fails. Preserve the original wire/state
                # violation instead of a second "prior write pending" error.
                if self.a.speed()['pending']:self.a.finish()
    def case(self,name):
        self.a.select(self.c,1 if name=='NON-BRIDGE' else 0)
        old=self.c.boot;fresh=self.a.restart_fixture();self.c.confirm();self.c.identity()
        require(fresh['boot_id']==self.c.boot!=old,'fresh logical boot required')
        with self.holding() as sid:
            self.a.seed(self.c,sid);require(self.a.state()['plans']=={2:[1]} and self.a.state()['pins'][1]==4,'live seed required')
            if name=='TRY-BARRIER':
                self.prepared(self.request(sid,verify=100),230400);self.speed(0,115200)
                self.a.advance(9);self.speed(0,115200);self.a.finish();self.speed(1,230400)
                require(self.a.events()[-1]['baud']==115200,'try answer sent at new baud')
                require(self.a.speed()['ports'][0]['deadline']==self.a.speed()['time_ms']+100,'verify did not start after response write')
            elif name=='VERIFY':
                self.trial(sid,100);self.a.advance(99);self.speed(1,230400);self.a.advance(1);self.speed(0,115200)
            elif name in ('REVERT-TRIAL','REVERT-COMMITTED','END-BARRIER'):
                self.trial(sid);self.a.advance(20)
                if name!='REVERT-TRIAL':self.commit(sid)
                if name=='END-BARRIER':
                    req=self.c.request(self.c.core['end'],session=sid);original=self.c.send;self.c.send=self.a.begin
                    try:tlvs(self.c.success(req))
                    finally:self.c.send=original
                    self.c.session=None
                else:self.prepared(self.request(sid,step=2,verify=65535),115200)
                self.speed(1 if name=='REVERT-TRIAL' else 2,230400);self.a.finish();self.speed(0,115200)
                require(self.a.events()[-1]['baud']==230400,'revert/end answer not sent at old baud')
            elif name=='COMMIT':
                self.trial(sid);self.a.advance(20);self.commit(sid,65535)
            elif name=='STATES':
                for step in (1,2):self.refused(self.request(sid,step=step),'unavailable')
                self.trial(sid)
                self.refused(self.request(sid),'unavailable');self.refused(self.request(sid,baud=460800,step=1),'unavailable')
                self.a.advance(20);self.commit(sid)
                for step in (0,1):self.refused(self.request(sid,step=step),'unavailable')
            elif name=='TOLERANCE':
                for rate in (1,100000,500000,235103,225882):self.refused(self.request(sid,baud=rate),'unsupported')
                # Reviewed integer boundaries around the fixture's230400 rate:
                # 235102/225883 are within2%, adjacent235103/225882 are outside.
                for rate in (232000,235102,225883):
                    self.success(self.request(sid,baud=rate),230400);self.speed(1,230400)
                    self.a.advance(20);self.success(self.request(sid,step=2,verify=0),115200);self.speed(0,115200)
            elif name=='MALFORMED':
                for n in range(7):self.refused(self.c.request(self.op,b'\0'*n,fn=4,session=sid),'malformed')
                self.refused(self.request(sid,verify=0),'malformed')
                for step in (3,255):self.refused(self.request(sid,step=step),'unsupported')
            elif name=='TLV':
                self.refused(self.request(sid,tail=b'\xff\0\0'),'unsupported',255)
                for tail in (b'\0\0\0',b'\x80\0\0',b'\x7f',b'\x7f\2\0x'):
                    self.refused(self.request(sid,tail=tail),'malformed')
                self.success(self.request(sid,tail=b'\x7f\0\0'),230400)
            elif name=='REPLAY':
                req=self.request(sid);self.success(req,230400);self.a.advance(20);before=self.a.speed()
                self.success(req,230400);require(self.a.speed()==before,'replayed try restarted deadline or switched again')
            elif name=='IDLE':
                self.trial(sid);self.a.advance(20);self.commit(sid)
                self.a.advance(2999);self.speed(2,230400);self.a.advance(1);self.speed(0,115200)
            elif name in ('IDLE-RENEW','IDLE-INVALID'):
                self.trial(sid);self.a.advance(20);self.commit(sid);self.a.advance(2000)
                if name=='IDLE-RENEW':
                    self.c.confirm();self.a.advance(2999);self.speed(2,230400);self.a.advance(1);self.speed(0,115200)
                else:
                    before=self.a.speed();require(self.a.raw(b'\0') is None,'invalid frame got a response')
                    after=self.a.speed();self.observations.append(dict(action='invalid-frame',before=before,after=after))
                    require(after==before,'invalid frame renewed idle or changed protected state')
                    self.a.advance(1000);self.speed(0,115200)
            elif name=='NON-BRIDGE':self.refused(self.request(sid),'unavailable')
            elif name=='ONE-PORT':
                self.trial(sid);self.a.advance(20)
                # Adverse Probe input, not a Host example: same owner ID on
                # another bridge must not raise a second port concurrently.
                self.a.select(self.c,2)
                try:self.refused(self.request(sid),'unavailable')
                finally:self.a.select(self.c,0)
        self.speed(0,115200)
    def run(self):
        start=len(self.c.results);RegisteredOpsChecks(self.c,self.a.selection).run()
        self.c.check('OEP-PORT-SPEED-CONTEXT','link §3; explicit logical bridge',self.context)
        if self.c.results[-1]['status']!='passed':self.c.abort=True
        for name in self.cases:self.c.check('OEP-PORT-SPEED-'+name,'link §3; core §6',lambda name=name:self.case(name))
        if callable(getattr(self.a,'select',None)):self.a.select(self.c,0)
        checks=self.c.results[start:]
        return dict(status='passed' if all(r['status']=='passed' for r in checks) else 'failed',checks=checks,full_conformance=False,
            state_observations=self.observations,write_events=self.a.events() if callable(getattr(self.a,'events',None)) else [],
            unchecked=['Host switch/wait behavior','other bridge/route geometries','idle renewal and execution pause',
                'force/expiry and deferred-write races','physical UART and production implementation'])
