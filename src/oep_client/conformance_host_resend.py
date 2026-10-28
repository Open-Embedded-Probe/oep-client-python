"""Same-byte resends, Probe deduplication and gated post-failure recovery."""
from .conformance import require
from .conformance_host_deadline import reviewed_identity
from .conformance_registered_ops import fixed_then_tlvs


class HostResendChecks:
    cases=('LOST-COBS','LOST-LENGTH','PIPELINE-COBS','PIPELINE-LENGTH','BAD-CRC','FAILURE-RECOVER','FAILURE-BLOCKED')
    def __init__(self,a,*,spec):self.a=a;self.identity=reviewed_identity(spec);self.results=[];self.observations=[]
    def check(self,name,fn):
        row=dict(id='HOST-RESEND-'+name,level='core',clause='core §4.1/4.4/5.1; transports §5/6')
        try:fn();row['status']='passed'
        except Exception as exc:row.update(status='failed',error=f'{type(exc).__name__}: {exc}')
        self.results.append(row)
    def context(self):
        for name in ('run_case','source_sha256'):require(callable(getattr(self.a,name,None)),'explicit retry stream/clock controls required')
    def case(self,name):
        r=self.a.run_case(name);events=r['events'];peers=[e for e in events if e['action']=='peer'];alloc=[e for e in peers if bytes.fromhex(e['request_hex'])[3:6]==b'\1\0\x10']
        pipeline=name.startswith('PIPELINE');failure=name.startswith('FAILURE');count=2 if pipeline else 1
        reqs=[(b'\1'+(2+i).to_bytes(2,'little')+b'\1\0\x10\7\0\0\0\1').hex() for i in range(count)]
        require(all(e['request_hex'] in reqs for e in alloc),'Host resent with changed corr/header/payload')
        expected=[reqs[0],reqs[1],reqs[1]] if pipeline else [reqs[0],reqs[0]]
        require([e['request_hex'] for e in alloc[:len(expected)]]==expected,'resend did not preserve unresolved request order/bytes')
        require(alloc[0]['next_after']==alloc[0]['next_before']+1,'first request did not execute')
        repeat=alloc[2] if pipeline else alloc[1];first=alloc[1] if pipeline else alloc[0]
        require(repeat['next_after']==repeat['next_before'] and repeat['reply_hex']==first['reply_hex'] and repeat['deadline_after']==repeat['deadline_before'],'Probe replay reexecuted/changed result/renewed lease')
        writes=[e for e in events if e['action']=='write'];apps=[e for e in writes if any(bytes.fromhex(p)[3:6]==b'\1\0\x10' for p in e['requests_hex'])]
        if name!='BAD-CRC':
            # core §4.4: max_op_ms (no shorter whole-request bound for op16) + 1000 ms + transfer time.
            maximum=r.get('max_op_ms');require(isinstance(maximum,int) and 1<=maximum<=600000,'fixture max_op_ms required')
            wire=apps[0]['wire_length'];transfer=(wire+64*3)*10/115200 if r['kind']=='cobs' else 0
            floor_ns=round((maximum/1000+1+transfer)*1e9)
            origin=apps[0]['time_ns']+(10_000_000 if pipeline else 0)
            require(apps[1]['started_ns']>=origin+floor_ns,'Host resent before missing response wait elapsed')
        total=3 if pipeline else 2
        require(len(alloc)==total,'extra state-changing requests sent')
        require(r['live']=={str(i):[1,1] for i in range(1,count+1)},'retry or recovery lost/changed live resources')
        if failure:
            require(r['error']=='TransportFailed' and r['failed_before_next'],'unanswered resend did not fail transport')
            confirms=[e for e in peers if bytes.fromhex(e['request_hex'])[3:6]==b'\0\0\1']
            require(confirms,'next request bypassed recovery confirm')
            if name=='FAILURE-BLOCKED':require(r['next_error']=='ConnectionError' and r['failed_after_next'] and r['next_id']==2,'failed recovery allowed next state change')
            else:
                require(r['next_error'] is None and not r['failed_after_next'] and r['next_id']==2 and r['next_reply_hex'] is not None and fixed_then_tlvs(bytes.fromhex(r['next_reply_hex']),bytes.fromhex('020300010001010001')),'recovered state query failed or repeated unknown state change')
                state=[e for e in peers if bytes.fromhex(e['request_hex'])[3:6]==b'\1\0\x13']
                require(len(state)==1 and events.index(confirms[0])<events.index(state[0]),'state query preceded recovery')
        else:
            expected_replies=['02020001000100','02030001000200'] if pipeline else ['02020001000100']
            require(r['error'] is None and len(r['replies_hex'])==len(expected_replies) and all(fixed_then_tlvs(bytes.fromhex(h),bytes.fromhex(x)) for h,x in zip(r['replies_hex'],expected_replies)) and r['next_id']==count+1,'Host retry returned wrong result or allocated twice')
        self.observations.append(r)
    def run(self):
        self.check('CONTEXT',self.context)
        if self.results[-1]['status']=='passed':
            for name in self.cases:self.check(name,lambda name=name:self.case(name))
        else:
            for name in self.cases:self.results.append(dict(id='HOST-RESEND-'+name,status='blocked'))
        return dict(status='passed' if all(row['status']=='passed' for row in self.results) else 'failed',checks=self.results,full_conformance=False,
                    spec=self.identity,observations=self.observations,implementation_sources_sha256=self.a.source_sha256() if callable(getattr(self.a,'source_sha256',None)) else {},
                    unchecked=['TCP close/reopen and actual wall-clock/transport','state read before deciding another state change','own-link confirm timing',
                               'resend exhaustion after corruption,other pipeline sizes/cache capacity','corr wrap and half-range limits (core §4.1/5.1)',
                               'production Host selection of the processing bound and integration'])
