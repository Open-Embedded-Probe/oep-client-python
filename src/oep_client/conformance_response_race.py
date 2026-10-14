"""Stale response completions across competing force and lease expiry."""
from .conformance import require
from .conformance_host_deadline import reviewed_identity


class ResponseRaceChecks:
    kinds=('FORCE','REJECTED','DOUBLE-FORCE','EXPIRE-NEW','NO-FORCE','RETURN-ID')
    def __init__(self,adapter,*,spec):self.a=adapter;self.identity=reviewed_identity(spec);self.checks=[];self.observations=[]
    def check(self,name,fn):
        row=dict(id='RESPONSE-RACE-'+name,level='core',clause='core §4.3/5.2/6.1/6.4/9')
        try:fn();row['status']='passed'
        except Exception as exc:row.update(status='failed',error=f'{type(exc).__name__}: {exc}')
        self.checks.append(row)
    def context(self):
        for name in ('run_case','source_sha256'):require(callable(getattr(self.a,name,None)),'explicit competing writers and completion controls required')
    def case(self,case):
        r=self.a.run_case(case);events=r['events'];kind=case['kind'];lease=case['lease_ms']
        require(r['case']==case and not r['pending_routes'],'writer fixture case/pending results differ')
        old_prepared=[e for e in events if e['action']=='prepare' and e['route']==0]
        require(len(old_prepared)==3,'old writer request sequence differs')
        old=old_prepared[-1]
        expected_request='01030001001007000000'+('03' if kind=='REJECTED' else '01')
        expected_reply='0203000003' if kind=='REJECTED' else '02030001000200'
        require(old['request_hex']==expected_request and old['reply_hex']==expected_reply and old['token']==[7,1],'old request/reply/token differs')
        # core §6.1 (a09fa45): the old holder's answer restarts its lease when it is generated (rejected too), not at
        # the writer's completion
        require(old['before']['deadline']==lease and old['after']['deadline']==100+lease and old['after']['holder']==7 and old['after']['now_ms']==100,'old answer did not restart the lease at its generation, or lost it')
        finish=next(e for e in events if e['action']=='finish' and e['request_hex']==expected_request)
        require(finish['reply_hex']==expected_reply and finish['token']==old['token'],'queued old result changed before completion')
        competing=[e for e in events if e['action']=='prepare' and e['route'] in (1,2) and bytes.fromhex(e['request_hex'])[3:6]==b'\0\0\x10']
        require(len(competing)==(2 if kind in ('DOUBLE-FORCE','RETURN-ID') else 1),'force attempt count differs')
        for i,e in enumerate(competing):
            holder=7 if kind=='RETURN-ID' and i==1 else 8+i;payload=lease.to_bytes(4,'little')+bytes((kind!='NO-FORCE',))
            require(bytes.fromhex(e['request_hex'])==b'\1\1\0\0\0\x10'+holder.to_bytes(4,'little')+payload,'competing open bytes differ')
            expected_open=(b'\2\1\0\0\10'+(lease-300).to_bytes(4,'little') if kind=='NO-FORCE'
                           else b'\2\1\0\1\0'+lease.to_bytes(4,'little')+(17).to_bytes(4,'little'))
            require(bytes.fromhex(e['reply_hex'])==expected_open,'competing open result lease/boot/refusal differs')
            sent=next(s for s in events if s['action']=='finish' and s['route']==e['route'] and s['request_hex']==e['request_hex'])
            require(sent['reply_hex']==e['reply_hex'] and sent['token']==e['token'],'queued competing response changed')
        state=r['state']
        if kind=='NO-FORCE':
            require(competing[0]['reply_hex'].startswith('0201000008') and competing[0]['token'] is None and competing[0]['before']==competing[0]['after'],'non-force open changed old ownership')
            refused=next(e for e in events if e['action']=='finish' and e['route']==1)
            require(refused['before']==refused['after'],'non-owner refused completion renewed old lease')
            require(finish['before']['deadline']==finish['after']['deadline']==100+lease,'old completion renewed the lease at send completion')
            replay=next(e for e in events if e['action']=='replay');require(replay['before']==replay['after'] and replay['reply_hex']==expected_reply,'replay changed old owner after valid completion')
            require(state['holder']==7 and state['live']=={'1':[1,1],'2':[1,1]} and state['next_id']==3,'non-force attempt lost resources')
        else:
            holder=7 if kind=='RETURN-ID' else 9 if kind=='DOUBLE-FORCE' else 8;rid=2 if kind=='REJECTED' else 3
            require(finish['before']==finish['after'],'stale old completion changed current ownership/lease/resources/cache')
            for i,e in enumerate(competing):require(e['after']['holder']==(7 if kind=='RETURN-ID' and i==1 else 8+i) and not e['after']['live'],'force did not retire old resources before taking ownership')
            if kind in ('DOUBLE-FORCE','RETURN-ID'):
                middle=next(e for e in events if e['action']=='finish' and e['route']==1)
                require(middle['token']==[8,2] and middle['before']==middle['after'],'middle stale completion changed third session')
            if kind=='EXPIRE-NEW':
                expiry=next(e for e in events if e['action']=='expire')
                require(expiry['before']['now_ms']==expiry['before']['deadline']==500+lease and expiry['after']['holder'] is None and not expiry['after']['live'],'new session failed to expire at boundary')
                require(state['holder'] is None and not state['live'],'old completion revived expired new owner')
            else:require(state['holder']==holder and state['live']=={str(rid):[1,1]} and state['deadline']==500+lease,'force successor state/lease differs')
            require(state['last']==holder and state['lease_ms']==lease and state['next_id']==rid+1,'force reset identifiers or last session')
            if kind=='RETURN-ID':
                require(not any(e['action']=='old-request' for e in events) and state['generation']==3,'raw ID reuse test sent an unsafe old wire request or missed ownership epoch')
            else:
                old_request=next(e for e in events if e['action']=='old-request')
                require(old_request['before']==old_request['after'] and old_request['reply_hex'].startswith('0203000007' if kind=='EXPIRE-NEW' else '0203000008'),'old request replay restored retired session or bypassed current owner')
            require(set(state['cache'])=={'1','2'},'old completion polluted successor replay cache')
        self.observations.append(r)
    def run(self):
        self.check('CONTEXT',self.context)
        if self.checks[-1]['status']=='passed':
            for kind in self.kinds:
                for lease in (1000,60000):self.check(f'{kind}-{lease}',lambda kind=kind,lease=lease:self.case(dict(kind=kind,lease_ms=lease)))
        return dict(status='passed' if all(c['status']=='passed' for c in self.checks) else 'failed',checks=self.checks,observations=self.observations,
                    spec=self.identity,full_conformance=False,implementation_sources_sha256=self.a.source_sha256() if callable(getattr(self.a,'source_sha256',None)) else {},
                    unchecked=['physical transport completion and simultaneous CPU execution','all extension resource/dependency types','production Host integration'])
