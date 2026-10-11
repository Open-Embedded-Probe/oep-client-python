"""Independent execution versus writer-wait clock oracle for link §3."""
import struct
from .conformance import require,tlvs
from .conformance_port_speed import PortSpeedChecks


class PortSpeedExecutionChecks(PortSpeedChecks):
    cases=tuple((op,duration,lease) for op in ('source','sink') for duration in (1500,3500) for lease in (1000,60000))
    def context(self):
        super().context()
        require(callable(getattr(self.a,'execution',None)),'explicit execution-time control required')
        values=[v for t,v in self.c.describe(0) if t==77]
        require(values==[struct.pack('<I',5000)],'reviewed max_op_ms5000 fixture required')

    def case(self,op,duration,lease):
        self.a.select(self.c,0)
        old=self.c.boot;fresh=self.a.restart_fixture();self.c.confirm();self.c.identity()
        require(fresh['boot_id']==self.c.boot!=old,'fresh logical boot required')
        with self.c.holding(lease) as sid:
            try:
                self.a.seed(self.c,sid);self.trial(sid,5000);self.commit(sid)
                self.a.advance(400)
                start=self.observe('before-execution');self.a.execution(duration)
                body=struct.pack('<I',16) if op=='source' else struct.pack('<H',8)+b'abcdefgh'
                req=self.c.request(16 if op=='source' else 17,body,fn=4,session=sid)
                original=self.c.send;self.c.send=self.a.begin
                try:
                    payload=self.c.success(req)
                    require(payload==b'\x10\0'+bytes(range(16)) if op=='source' else payload==b'','timed link response differs')
                finally:self.c.send=original
                held=self.observe('after-execution')
                require(held['time_ms']==start['time_ms']+duration,'execution did not consume declared logical time')
                require(held['execution_ms']==0 and held['executions'][-1]==dict(fn=4,op=16 if op=='source' else 17,duration_ms=duration),'execution control did not apply to selected operation')
                require(held['protected']==start['protected'],'execution changed protected state')
                require(held['ports'][0]['state']==2 and held['ports'][0]['baud']==230400,'execution expired port')
                require(held['ports'][0]['deadline']==held['time_ms']+3000,'execution consumed idle interval')
                require(held['lease_deadline']==start['lease_deadline']+duration,'execution consumed or renewed lease')
                wait=599 if lease==1000 else 2999
                self.a.advance(wait);waiting=self.observe('writer-wait')
                require(waiting['ports']==held['ports'] and waiting['lease_deadline']==held['lease_deadline'],'writer wait paused or renewed timer')
                require(waiting['protected']==held['protected'],'writer wait released protected state early')
                self.a.finish();sent=self.observe('writer-complete')
                require(sent['ports'][0]['state']==2 and sent['ports'][0]['deadline']==sent['time_ms']+3000,'response completion did not restart idle')
                require(sent['lease_deadline']==sent['time_ms']+lease,'response completion did not restart lease')
                require(self.a.events()[-1]['baud']==230400,'timed response sent after speed expiry')
                # Wait to the exact limiting timer with no request to renew it.
                boundary=min(lease,3000)
                self.a.advance(boundary-1);self.speed(2,230400)
                self.a.advance(1);self.speed(0,115200)
                end=self.observe('final-boundary')['protected']
                if lease==1000:
                    require(end['ownership']['holder'] is None and end['plans']=={},'lease boundary retained resources')
                    self.c.session=None
                else:require(end==start['protected'],'idle boundary changed session resources')
            finally:
                if self.a.speed()['pending']:self.a.finish()
                if self.a.state()['ownership']['holder'] is None:self.c.session=None

    def run(self):
        start=len(self.c.results)
        self.c.check('OEP-PORT-SPEED-EXECUTION-CONTEXT','link §3; core §6.1/7.5',self.context)
        if self.c.results[-1]['status']!='passed':self.c.abort=True
        for op,duration,lease in self.cases:
            self.c.check(f'OEP-PORT-SPEED-EXECUTION-{op.upper()}-{duration}-{lease}','link §3; core §6.1',lambda op=op,duration=duration,lease=lease:self.case(op,duration,lease))
        rows=self.c.results[start:]
        return dict(status='passed' if all(r['status']=='passed' for r in rows) else 'failed',checks=rows,full_conformance=False,
                    state_observations=self.observations,write_events=self.a.events(),
                    unchecked=['simultaneous execution on other routes','other operation execution paths','Host switch/wait and physical timing'])
