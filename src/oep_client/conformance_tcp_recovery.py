"""TCP pauses, broken framing, reconnect and retained replay/session state."""
from .conformance import require
from .conformance_host_deadline import reviewed_identity


class TcpRecoveryChecks:
    cases=('PAUSE-HEADER','PAUSE-BODY','LENGTH','SHAPE','EOF','BLOCKED')
    def __init__(self, adapter, *, spec):
        self.a=adapter; self.identity=reviewed_identity(spec); self.checks=[]; self.observations=[]
    def check(self,name,fn):
        row=dict(id='TCP-RECOVERY-'+name,level='core',clause='transports §2/3/5; core §5.2')
        try:fn();row['status']='passed'
        except Exception as exc:row.update(status='failed',error=f'{type(exc).__name__}: {exc}')
        self.checks.append(row)
    def context(self):
        for name in ('run_case','source_sha256'):
            require(callable(getattr(self.a,name,None)),'explicit TCP reconnect/clock controls required')
    def case(self,name):
        r=self.a.run_case(name); events=r['events']
        requests=[e for e in events if e['action']=='request']
        alloc=[e for e in requests if bytes.fromhex(e['request_hex'])[3:6]==b'\1\0\x10']
        require(alloc and all(e['request_hex']=='0102000100100700000001' for e in alloc),'allocation request bytes changed')
        require(alloc[0]['after']['next_id']==2 and r['state']['next_id']==2 and r['state']['live']=={'1':[1,1]} and r['state']['holder']==7,'session/resources lost or allocation repeated')
        results=[e for e in events if e['action']=='result']
        if name.startswith('PAUSE'):
            waits=[e for e in events if e['action']=='wait']
            require(r['connections']==1 and len(alloc)==1 and len(waits)==1 and waits[0]['reply_hex'] is None and waits[0]['buffered'],'TCP pause discarded partial frame/reconnected')
            require(waits[0]['time_ns']==100_000_000 and len(results)==1 and results[0]['time_ns']==400_000_000,'TCP fragment wait differs')
        else:
            require(r['connections']==2,'broken TCP did not reconnect')
            broken=next(e for e in events if e['action']=='broken')
            require(broken['error']=={'LENGTH':'FramingLost','SHAPE':'ShortPayload','EOF':'ConnectionError','BLOCKED':'FramingLost'}[name],'broken framing not detected')
            close=next(e for e in events if e['action']=='close'); connect=[e for e in events if e['action']=='connect'][1]
            require(close['closed'] and events.index(close)<events.index(connect) and close['state']==connect['state'],'reconnect before close or Core reset')
            confirm=next(e for e in events if e['action']=='confirm')
            after=[e for e in requests if e['epoch']==1]
            require(after and after[0]['request_hex']=='010a00000001000000004f45503f0101','reopened TCP did not confirm first')
            preserved=next(e for e in events if e['action']=='preserved')
            require(preserved['before']==preserved['after'],'reconnect/replay changed retained session/cache/deadline')
            if name=='BLOCKED':
                require(not confirm['valid'] and len(alloc)==1 and len(after)==1 and not results and next(e for e in events if e['action']=='blocked')['closed'],'failed confirm allowed replay')
            else:
                require(confirm['valid'] and len(alloc)==2 and alloc[1]['epoch']==1 and events.index(confirm)<events.index(alloc[1]),'replay before valid confirm')
                raw=bytes.fromhex(confirm['reply_hex'])
                require(len(raw)>=22 and raw[:10]==b'\2\x0a\0\1\0OEP!\1' and int.from_bytes(raw[18:22],'little')==17,'recovery confirm corr/header/magic/boot differs')
                require(alloc[1]['before']==alloc[1]['after'] and alloc[1]['reply_hex']==alloc[0]['reply_hex'],'replay reexecuted or changed state/result')
        if name!='BLOCKED':require(len(results)==1 and results[0]['reply_hex']=='02020001000100','wrong recovered allocation result')
        self.observations.append(r)
    def run(self):
        self.check('CONTEXT',self.context)
        if self.checks[-1]['status']=='passed':
            for name in self.cases:self.check(name,lambda name=name:self.case(name))
        else:
            for name in self.cases:self.checks.append(dict(id='TCP-RECOVERY-'+name,status='blocked'))
        return dict(status='passed' if all(r['status']=='passed' for r in self.checks) else 'failed',checks=self.checks,
                    observations=self.observations,spec=self.identity,full_conformance=False,
                    implementation_sources_sha256=self.a.source_sha256() if callable(getattr(self.a,'source_sha256',None)) else {},
                    unchecked=['production SerialLink/full Host reconnect policy','OS TCP sockets/network timing','reboot or lease expiration during reconnect','notifications and multiple concurrent TCP clients'])
