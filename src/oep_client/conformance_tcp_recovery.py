"""TCP pauses, broken framing, reconnect and retained replay/session state."""
from .conformance import require
from .conformance_host_deadline import reviewed_identity
from .conformance_registered_ops import fixed_then_tlvs


def answers(reply_hex,fixed):
    """The reply starts with these bytes (header and fixed part); TLVs may follow (core §2.3, §4.3)."""
    return reply_hex is not None and fixed_then_tlvs(bytes.fromhex(reply_hex),bytes.fromhex(fixed) if isinstance(fixed,str) else fixed)


class TcpRecoveryChecks:
    cases=('PAUSE-HEADER','PAUSE-BODY','LENGTH','SHAPE','EOF','BLOCKED','REBOOT','REBOOT-SAME','EXPIRED')
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
        if name in ('REBOOT','REBOOT-SAME','EXPIRED'):
            self.lifecycle(name,r)
            self.observations.append(r)
            return
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
                require(len(raw)>=21 and raw[:10]==b'\2\x0a\0\1\0OEP!\1' and int.from_bytes(raw[17:21],'little')==17,'recovery confirm corr/header/magic/boot differs')
                require(alloc[1]['before']==alloc[1]['after'] and alloc[1]['reply_hex']==alloc[0]['reply_hex'],'replay reexecuted or changed state/result')
        if name!='BLOCKED':require(len(results)==1 and answers(results[0]['reply_hex'],'02020001000100'),'wrong recovered allocation result')
        self.observations.append(r)
    def lifecycle(self,name,r):
        events=r['events'];require(r['connections']==2,'lifecycle did not reopen TCP')
        close=next(e for e in events if e['action']=='close');transition=next(e for e in events if e['action']=='transition')
        connect=[e for e in events if e['action']=='connect'][1]
        require(close['closed'] and events.index(close)<events.index(transition)<events.index(connect),'lifecycle transition/close/reopen order differs')
        require(close['state']==transition['before'] and transition['after']==connect['state'],'transition snapshots differ')
        before=transition['before'];state=r['state'];boot=18 if name=='REBOOT' else 17
        require(before['holder']==7 and before['live']=={'1':[1,1]} and before['next_id']==2,'lifecycle live seed missing')
        require(state['holder'] is None and not state['live'] and state['boot_id']==boot,'expired/reboot resources/session survived')
        requests=[e for e in events if e['action']=='request'];alloc=[e for e in requests if bytes.fromhex(e['request_hex'])[3:6]==b'\1\0\x10']
        require(all(e['request_hex']=='0102000100100700000001' for e in alloc) and len(alloc)==(2 if name=='EXPIRED' else 1),'unknown allocation reexecuted after lifecycle transition')
        after=[e for e in requests if e['epoch']==1]
        require(after[0]['request_hex']=='010a00000001000000004f45503f0101','lifecycle skipped confirm')
        expected=['010a00000001000000004f45503f0101']
        if name=='EXPIRED':expected+=['0102000100100700000001']
        expected+=['010b0000001300000000','010c0000001207000000']
        if name.startswith('REBOOT'):expected+=['010d00000002000000000000','010e00000002000000000100']
        expected+=['010f0001001300000000']
        require([e['request_hex'] for e in after]==expected,'recovery state/rediscovery request order or bytes differ')
        confirm=next(e for e in events if e['action']=='confirm');raw=bytes.fromhex(confirm['reply_hex'])
        require(confirm['valid'] and raw[:10]==b'\2\x0a\0\1\0OEP!\1' and int.from_bytes(raw[17:21],'little')==boot,'lifecycle confirm boot/corr/header differs')
        require(answers(next(e for e in events if e['action']=='core-state')['reply_hex'],'020b0001000000000000'),'lifecycle core state not unlocked')
        require(answers(next(e for e in events if e['action']=='old-session')['reply_hex'],'020c000007'),'new old-session request revived session')
        require(answers(next(e for e in events if e['action']=='resource-state')['reply_hex'],'020f00010000'),'freed resource remained visible')
        invalid=next(e for e in events if e['action']=='invalidate')
        require(invalid['boot_id']==boot and invalid['session'] is None and not invalid['resource_ids'],'Host kept stale session/resource identifiers')
        if name=='EXPIRED':
            require(transition['time_ns']==60_000_000_000 and transition['after']['cache']==before['cache'],'lease expiry discarded replay cache or occurred off boundary')
            require(alloc[1]['before']==alloc[1]['after'] and alloc[1]['reply_hex']==alloc[0]['reply_hex'] and answers(alloc[0]['reply_hex'],'02020001000100'),'expired historical response reexecuted/changed state')
            require(state['next_id']==2 and state['last']==7 and state['deadline']==60000,'expiry reset identifiers/deadline/last session')
            require(not invalid['old_fn_cache_cleared'] and not invalid['discovered'],'unchanged boot unexpectedly required rediscovery in sample')
        else:
            require(transition['after']['cache']=={} and transition['after']['last'] is None and state['next_id']==1,'reboot retained old replay/session/resource IDs')
            pages=[e for e in events if e['action']=='list-page']
            require(invalid['old_fn_cache_cleared'] and len(pages)==2 and [e['first'] for e in pages]==[0,1],'reboot skipped fn rediscovery')
            require([e['fn'] for e in invalid['discovered']]==[1,2] and all(e['name']=='io.github.open-embedded-probe.resource' for e in invalid['discovered']),'rediscovery returned wrong interfaces')
            for i,page in enumerate(pages):
                n=b'io.github.open-embedded-probe.resource'
                value=b'\2\0\1'+(i+1).to_bytes(2,'little')+i.to_bytes(2,'little')+bytes((1,len(n)))+n
                require(answers(page['reply_hex'],b'\2'+(13+i).to_bytes(2,'little')+b'\1\0'+value),'rediscovery page wire differs')
        require(not any(e['action']=='result' for e in events),'lifecycle returned stale live allocation')
    def run(self):
        self.check('CONTEXT',self.context)
        if self.checks[-1]['status']=='passed':
            for name in self.cases:self.check(name,lambda name=name:self.case(name))
        else:
            for name in self.cases:self.checks.append(dict(id='TCP-RECOVERY-'+name,status='blocked'))
        return dict(status='passed' if all(r['status']=='passed' for r in self.checks) else 'failed',checks=self.checks,
                    observations=self.observations,spec=self.identity,full_conformance=False,
                    implementation_sources_sha256=self.a.source_sha256() if callable(getattr(self.a,'source_sha256',None)) else {},
                    unchecked=['production SerialLink/full Host reconnect policy','OS TCP sockets/network timing','arbitrary reboot catalogs/configuration and lease race interleavings','notifications and multiple concurrent TCP clients'])
