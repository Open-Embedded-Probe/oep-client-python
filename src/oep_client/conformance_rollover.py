"""Independent sample host rollover checks on explicit retention equipment."""
import struct
from .conformance import require, Violation
from .conformance_corr import CorrChecks
from .conformance_recovery_race_sample import SamplePendingHost
from .conformance_rollover_sample import SampleRolloverHost
from .conformance_route_sample import encoded


class RolloverChecks(CorrChecks):
    def host(self, barrier=None):
        limits={self.a.primary:{key:self.c.observed['confirm'][key] for key in ('max_frame','window','max_inflight')}}
        def logical_barrier():
            require(self.a.pending(self.a.primary)==[] and self.a.receive(self.a.primary)==[], 'logical reader has undrained replies/credit')
            self.a.trace.append({'method':'reader_barrier','scope':'logical-model','completed':True})
            return True
        return self.make_host(self.c,SamplePendingHost(self.c.reg,limits),self.a.primary,barrier or logical_barrier)

    make_host = staticmethod(SampleRolloverHost)

    def assert_blocked(self, host, operation):
        before,trace,corr = self.state(),len(self.c.trace),self.c.corr
        try: operation()
        except Violation: pass
        else: require(False,'unsafe rollover admission accepted')
        require(self.state()==before and len(self.c.trace)==trace and self.c.corr==corr, 'blocked rollover changed wire/state/counter')

    def switched(self, host, old_sid, old_rid, status='switched'):
        first=len(self.c.trace); old_records=host.pending.snapshot()
        decision=host.switch_session()
        require(decision['status']==status, 'rollover decision')
        self.observations.append(encoded({'decision':decision,'records':host.pending.snapshot(),'host_state':host.state}))
        rows=[struct.unpack_from('<BHHBI',bytes.fromhex(row['request_hex'])) for row in self.c.trace[first:]]
        require(rows[0][1:]==(65535,0,self.c.core['end'],old_sid), 'did not reserve 65535 for end')
        middle = rows[1:-1] if status=='switched' else rows[1:]
        require(all(row[2]==0 and row[3] in (self.c.core['confirm'],self.c.core['describe']) and row[4]==0 for row in middle),
                'rollover automatically repeated a mutation or used an unsafe session read')
        require(not host.bindings, 'old resource bindings survived end')
        require(host.pending.snapshot()[:len(old_records)]==old_records, 'rollover rewrote old known/unknown results')
        if status!='switched':
            require(self.c.session is None and decision['new_session'] is None and
                    not any(row[3]==self.c.core['open'] and row[2]==0 for row in rows), 'unsafe open after incomplete barrier/boot change')
            return
        replacement=decision['new_session']
        require(replacement!=old_sid and replacement!=0 and self.c.session==replacement and self.c.corr==1, 'session ID/corr reused')
        require(rows[-1][1:]==(1,0,self.c.core['open'],replacement), 'new session did not start with open 1')
        state=self.state()
        require(state['holder']==replacement and state['last']==replacement and state['high']==1 and
                len(state['cache'])==1 and not state['resources'], 'new session retained old state/history')
        self.assert_blocked(host,lambda:host.binding(old_rid))
        request=host.request(16,b'\x01\x07',fn=self.a.functions[0]); frame=host.exchange(request); self.result(frame,request)
        require(len(frame)==7 and struct.unpack_from('<H',frame,5)[0]==state['next_resource_id'] and
                struct.unpack_from('<H',frame,5)[0]!=old_rid, 'same-boot resource ID reused')
        host.bind(struct.unpack_from('<H',frame,5)[0],self.a.functions[0],1)
        self.c.rejected(self.c.request(18,b'\x01'+struct.pack('<H',old_rid),session=replacement,fn=self.a.functions[0]),'no_resource')

    def transition(self, unknown=False):
        with self.c.holding(60000) as sid:
            host=self.host(); self.c.corr=65533
            request=host.request(16,b'\x01\x07'+(b'\x40\x02\0\0\0' if unknown else b''),fn=self.a.functions[0])
            if unknown:
                require(len(request)==17,'lost request retention geometry')
                require(self.a.discard_reply(request) is None,'reply delivered despite loss')
                self.a.wait_ms(1050)
                frame=self.c.exchange(request); self.result(frame,request,'result_lost')
                require(host.pending.accept(self.a.primary,0,frame),'lost response not accounted')
            else:
                frame=host.exchange(request); self.result(frame,request)
            state=self.state(); require(len(state['resources'])==1,'initial resource missing')
            rid=next(iter(state['resources']))  # oracle only; never disclose the lost RID to the sample host
            if not unknown:
                require(len(frame)==7 and struct.unpack_from('<H',frame,5)[0]==rid, 'known create RID')
                host.bind(struct.unpack_from('<H',frame,5)[0],self.a.functions[0],1)
            else: require(not host.bindings, 'lost create supplied an unobserved resource binding')
            require(self.c.corr==65534,'last normal corr')
            self.assert_blocked(host,lambda:host.request(16,b'\x01\x07',fn=self.a.functions[0]))
            self.switched(host,sid,rid)

    def pending_blocked(self):
        with self.c.holding(60000) as sid:
            host=self.host(); self.c.corr=65532
            requests=[host.request(16,b'\x01\x07',fn=self.a.functions[0]) for _ in range(2)]
            self.a.hold(self.a.primary,0); self.a.submit([(self.a.primary,request) for request in requests]); self.service()
            try: self.assert_blocked(host,host.switch_session)
            finally:
                self.a.hold(self.a.primary,None); self.service()
            raw=self.a.receive(self.a.primary)
            require(len(raw)==2,'pending results missing/extra')
            for frame,request in zip(raw,requests):
                self.result(frame,request); require(host.pending.accept(self.a.primary,0,frame),'pending result not accepted')
            ids=self.created(raw)
            require(set(ids)==set(self.state()['resources']), 'pending create RIDs differ from independent oracle')
            rid=ids[0];host.bind(rid,self.a.functions[0],1)
            self.switched(host,sid,rid)

    def boot_change(self):
        with self.c.holding(60000) as sid:
            host=self.host(); self.c.corr=65533
            request=host.request(16,b'\x01\x07',fn=self.a.functions[0]);frame=host.exchange(request)
            rid=struct.unpack_from('<H',frame,5)[0];host.bind(rid,self.a.functions[0],1)
            send=self.c.send
            def restart_after_end(request):
                result=send(request)
                if struct.unpack_from('<BHHBI',request)[3:]==(self.c.core['end'],sid):self.a.restart(self.c.boot^1)
                return result
            self.c.send=restart_after_end
            try:self.switched(host,sid,rid,'boot-changed')
            finally:self.c.send=send

    def barrier_failure(self):
        with self.c.holding(60000) as sid:
            host=self.host(lambda:False);self.c.corr=65533
            request=host.request(16,b'\x01\x07',fn=self.a.functions[0]);frame=host.exchange(request)
            rid=struct.unpack_from('<H',frame,5)[0];host.bind(rid,self.a.functions[0],1)
            self.switched(host,sid,rid,'reader-unavailable')

    def run(self):
        self.observations=[]
        def controls():
            require(getattr(self.a,'reset_scope',None)=='logical-model' and all(callable(getattr(self.a,name,None)) for name in
                    ('discard_reply','restart','pending','receive')), 'explicit logical rollover/loss/reader controls required')
        self.c.check('CORE-ROLLOVER-CONTROL','explicit host sample',controls)
        if self.c.results[-1]['status']!='passed':self.c.abort=True
        report=super().run()
        if report['status']!='passed':self.c.abort=True
        for name,method in [('KNOWN',self.transition),('UNKNOWN',lambda:self.transition(True)),('PENDING',self.pending_blocked),
                            ('BOOT',self.boot_change),('BARRIER',self.barrier_failure)]:
            first=len(self.a.trace)
            self.c.check('CORE-ROLLOVER-'+name,'core §4.1/5.2/6/9; sample host',method)
            self.c.results[-1]['adapter_trace']=self.a.trace[first:]
        report.update(status='passed' if all(row['status']=='passed' for row in self.c.results) else 'failed',checks=self.c.results,
                      adapter_trace=self.a.trace,rollover_observations=self.observations,scope='sample host reserved-end and session/resource retirement')
        report['unchecked']=['production Host integration','physical reader cancellation/reconnect','end/open timeout recovery',
                             'pending abandonment during exhausted session','arbitrary extension side effects','concurrent scheduling']
        return report
