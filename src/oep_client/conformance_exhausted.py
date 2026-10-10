"""Independent exhausted pending-batch abandonment and fresh-route recovery checks."""
import struct
from .conformance import require, tlvs
from .conformance_retention import RetentionChecks
from .conformance_recovery_race_sample import SamplePendingHost
from .conformance_exhausted_sample import SampleExhaustedHost
from .conformance_route_sample import encoded


class ExhaustedChecks(RetentionChecks):
    make_host=staticmethod(SampleExhaustedHost)

    def batch(self):
        c,a=self.c,self.a;mode=a.batch_mode
        with c.holding(3000) as sid:
            boot=c.boot;self.peer.confirm();self.peer.identity()
            require(self.peer.boot==boot,'peer boot mismatch')
            limits={route:{key:checks.observed['confirm'][key] for key in ('max_frame','window','max_inflight')}
                    for route,checks in ((a.primary,c),(a.peer,self.peer))}
            ledger=SamplePendingHost(c.reg,limits)
            c.corr=65532
            requests=[self.create(sid),self.create(sid,kind=9,size=17),c.request(c.core['end'],session=sid)]
            require([struct.unpack_from('<H',req,1)[0] for req in requests]==[65533,65534,65535], 'exhausted batch corr')
            for req in requests:ledger.track(a.primary,req,boot)
            a.hold(a.primary,0);a.submit([(a.primary,req) for req in requests])
            require(a.pending(a.primary)==requests,'complete batch not admitted')
            before=self.state();raw=[]
            if mode!='queued':
                self.service();a.hold(a.primary,None);self.service();raw=a.receive(a.primary)
                require(len(raw)==3,'batch reply count')
                for frame,req,reason in zip(raw,requests,(None,'malformed',None)):self.result(frame,req,reason)
                require(len(raw[0])==7 and len(raw[2])==5,'batch response shape')
                if mode=='prefix':require(ledger.accept(a.primary,0,raw[0]),'known prefix not accepted')
            a.trace.append({'method':'exhausted_batch_delivery','mode':mode,'requests_hex':[r.hex() for r in requests],
                            'observed_responses_hex':[r.hex() for r in raw],'delivered_count':1 if mode=='prefix' else 0})
            a.wait_ms(1050)
            old_rows=ledger.snapshot();ledger.abandon(a.primary);c.session=None
            retired=ledger.snapshot()
            require([row['status'] for row in retired]==(['completed','unknown','unknown'] if mode=='prefix' else ['unknown']*3),
                    'known/unknown batch accounting')
            if mode=='prefix':require(retired[0]==old_rows[0],'known prefix rewritten')
            for frame in raw:require(not ledger.accept(a.primary,0,frame),'late old-reader result adopted')
            require(ledger.snapshot()==retired,'late result rewrote outcome')
            a.close(a.primary)
            closed=self.state()
            require(a.pending(a.primary)==[] and a.receive(a.primary)==[],'closed old route retained credit/output')
            require(closed['holder']==(sid if mode=='queued' else None) and
                    closed['next_resource_id']==before['next_resource_id']+(0 if mode=='queued' else 1) and not closed['resources'],
                    'batch execution or logical close state')
            # SID 0 is allowed on the new route. The exhausted S never moves there.
            def barrier():
                require(a.pending(a.primary)==[] and a.receive(a.primary)==[] and a.pending(a.peer)==[] and a.receive(a.peer)==[],
                        'logical route barrier not drained')
                a.trace.append({'method':'reader_barrier','scope':'logical-model distinct-route','completed':True})
                return True
            host=self.make_host(self.peer,a.functions,ledger,a.primary,a.peer,lambda:False)
            host.bindings[123]=(boot,sid,a.functions[0],1)
            start=len(self.peer.trace);adapter_start=len(a.trace)
            blocked=host.recover(sid,boot)
            require(blocked['status']=='reader-unavailable' and len(self.peer.trace)==start and not host.bindings,
                    'failed barrier used wire or retained old bindings')
            host.barrier=barrier
            decision=host.recover(sid,boot)
            if mode=='queued':
                require(decision['status']=='lock-held' and ledger.snapshot()==retired and self.state()==closed,
                        'held exhausted session was adopted or mutated')
                # Fixture waits for the known 3000ms lease; recovery host never expires/forces S.
                a.wait_ms(1050);a.wait_ms(1050)
                decision=host.recover(sid,boot)
            require(decision['status']=='recovered' and decision['original_outcomes']=='unchanged' and
                    type(decision['new_session']) is int and decision['new_session'] not in (0,sid), 'fresh-route recovery decision')
            require(ledger.snapshot()[:len(retired)]==retired and a.primary in ledger.quarantined and sid in ledger.retired_sessions,
                    'recovery rewrote unknown/known results or resumed old session')
            stable=ledger.snapshot()
            for frame in raw:require(not ledger.accept(a.primary,0,frame),'old reader result adopted after recovery')
            require(ledger.snapshot()==stable,'late old result rewrote new session ledger')
            require(not host.bindings and self.peer.corr==1 and self.peer.session==decision['new_session'],'old bindings/corr survived')
            rows=[struct.unpack_from('<BHHBI',bytes.fromhex(row['request_hex'])) for row in self.peer.trace[start:]]
            require(all((fn==0 and session==0 and op in (1,3,19)) or
                        (fn in a.functions and session==0 and op==19) or
                        (fn==0 and op==c.core['open'] and session==decision['new_session'] and corr==1)
                        for _,corr,fn,op,session in rows), 'recovery moved old S, repeated mutation or used force')
            sent=[bytes.fromhex(item[1]['hex']) for row in a.trace[adapter_start:] if row['method']=='submit'
                  for item in row['arguments'][0]]
            require(sent==[bytes.fromhex(row['request_hex']) for row in self.peer.trace[start:]], 'actual wire differs from host trace')
            opens=[bytes.fromhex(row['request_hex']) for row in self.peer.trace[start:] if struct.unpack_from('<BHHBI',bytes.fromhex(row['request_hex']))[2:4]==(0,c.core['open'])]
            require(len(opens)==1 and opens[0][14]==0,'recovery force or repeated open')
            state=self.state()
            require(state['holder']==decision['new_session'] and state['high']==1 and len(state['cache'])==1 and
                    not state['resources'] and state['next_resource_id']==closed['next_resource_id'], 'fresh session state')
            self.observation=encoded({'mode':mode,'decision':decision,'records':ledger.snapshot(),'before':before,'closed':closed,'recovered':state})
            tlvs(self.peer.success(self.peer.request(self.peer.core['end'],session=self.peer.session)))
            self.peer.session=None

    def run(self):
        self.observation=None
        def controls():
            require(getattr(self.a,'batch_mode',None) in ('none','prefix','queued') and getattr(self.a,'reset_scope',None)=='logical-model' and
                    all(callable(getattr(self.a,key,None)) for key in ('pending','receive','close','wait_ms')), 'explicit logical exhausted-batch controls required')
        self.c.check('CORE-EXHAUSTED-CONTROL','explicit logical sample',controls)
        if self.c.results[-1]['status']!='passed':self.c.abort=True
        report=super().run()
        if report['status']!='passed':self.c.abort=True
        first=len(self.a.trace)
        self.c.check('CORE-EXHAUSTED-'+str(getattr(self.a,'batch_mode',None)).upper(),'core §4.1/4.4/5.2/6; fresh-route sample host',self.batch)
        self.c.results[-1]['adapter_trace']=self.a.trace[first:]
        report.update(status='passed' if all(row['status']=='passed' for row in self.c.results) else 'failed',checks=self.c.results,
                      adapter_trace=self.a.trace,peer_exchanges=self.peer.trace,exhausted_observation=self.observation,
                      scope='exhausted pending batch retirement and fresh-route recovery',
                      unchecked=['physical reader cancellation/reconnect','production Host integration','concurrent writers',
                                 'arbitrary extension side effects','other batch sizes/retention limits'])
        return report
