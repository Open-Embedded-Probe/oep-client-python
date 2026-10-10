"""Force during readback and route-wide abandonment of unresolved results."""
import struct
from .conformance import require, tlvs
from .conformance_recovery import RecoveryChecks
from .conformance_recovery_sample import SampleRecoveryHost
from .conformance_recovery_race_sample import SamplePendingHost
from .conformance_route_sample import encoded


class RecoveryRaceChecks(RecoveryChecks):
    def takeover(self, sid, boot):
        self.peer.confirm(); self.peer.identity()
        require(self.peer.boot == boot and self.state()['holder'] == sid, 'own S/boot changed before fixture force')
        replacement = sid ^ 0xffffffff or 1
        self.peer.corr = 0
        value = self.peer.success(self.peer.request(self.peer.core['open'], struct.pack('<IB',60000,1), session=replacement))
        self.peer.session = replacement
        self.c.session = None
        require(len(value) >= 8 and struct.unpack_from('<II',value) == (60000,boot), 'force open payload')
        tlvs(value[8:])
        state = self.state()
        require(state['holder'] == replacement and not state['resources'] and not state['subscriptions'], 'force retained S state')
        return replacement

    def end_peer(self):
        if self.peer.session is not None:
            tlvs(self.peer.success(self.peer.request(self.peer.core['end'], session=self.peer.session)))
            self.peer.session = None

    def forced_readback(self, point):
        with self.c.holding(60000) as sid:
            before, boot = self.host.inventory(), self.c.boot
            request = self.create(sid,size=17)
            self.lose(request); self.replay_unchanged(request,reason='result_lost')
            old = self.state(); replacement_state = []
            def change():
                replacement = self.takeover(sid,boot)
                value = self.peer.success(self.peer.request(16,b'\x01\x07',fn=self.a.functions[1],session=replacement))
                rid = struct.unpack('<H',value)[0]
                require(rid == old['next_resource_id'] and rid not in old['resources'], 'force reused old resource number')
                replacement_state.append(self.state())
            self.a.arm_transition(point,sid,change)
            first = len(self.c.trace)
            try:
                decision = self.host.recover(sid,boot,before,action='create',slot=(self.a.functions[0],1),single_writer=True)
                self.decisions.append(encoded(decision))
                require(decision == {'status':'session-unavailable','original_outcome':'unknown','inventory':None},
                        'force readback adopted stale or replacement resources')
                require(len(replacement_state) == 1 and self.state() == replacement_state[0], 'old-S reads changed T state/lease/history')
                for row in self.c.trace[first:]:
                    _,_,fn,op,session = struct.unpack_from('<BHHBI',bytes.fromhex(row['request_hex']))
                    require((fn == 0 and op in (1,3,4)) or (fn in self.a.functions and op == 19 and session == 0),
                            'readback mutated S/T or moved S to peer')
            finally:
                self.a.disarm_transition()
                self.end_peer()

    def abandoned_batch(self):
        mode = self.a.batch_mode
        with self.c.holding(60000) as sid:
            self.peer.confirm(); self.peer.identity()
            require(self.peer.boot == self.c.boot, 'peer boot differs')
            boot, before = self.c.boot, self.state()
            limits = {route:{key:checks.observed['confirm'][key] for key in ('max_frame','window','max_inflight')}
                      for route,checks in ((self.a.primary,self.c),(self.a.peer,self.peer))}
            ledger = self.make_pending_host(self.c.reg,limits)
            requests = [self.create(sid), self.create(sid,kind=9,size=17), self.create(sid,size=17)]
            epoch = ledger.epochs[self.a.primary]
            for request in requests: ledger.track(self.a.primary,request,boot)
            require(len(requests) == limits[self.a.primary]['max_inflight'] and sum(map(len,requests)) <= limits[self.a.primary]['window'],
                    'batch exceeded sample admission')
            self.a.hold(self.a.primary,0)
            self.a.submit([(self.a.primary,request) for request in requests])
            require(self.a.pending(self.a.primary) == requests, 'source did not accept complete unresolved batch')
            raw = []
            if mode != 'queued':
                self.service(); self.a.hold(self.a.primary,None); self.service()
                raw = self.a.receive(self.a.primary)
                require(len(raw) == 3, 'missing/extra raw batch replies')
                for frame,request,reason in zip(raw,requests,(None,'malformed',None)):
                    self.result(frame,request,reason)
                require(len(raw[0]) == len(raw[2]) == 7 and
                        [struct.unpack_from('<H',raw[index],5)[0] for index in (0,2)] ==
                        [before['next_resource_id'],before['next_resource_id']+1], 'batch create result IDs/order')
                if mode == 'prefix': require(ledger.accept(self.a.primary,epoch,raw[0]), 'delivered prefix not recorded')
            self.a.trace.append({'method':'batch_delivery','mode':mode,'requests_hex':[r.hex() for r in requests],
                                 'observed_responses_hex':[r.hex() for r in raw], 'delivered_count':1 if mode=='prefix' else 0})
            waited = self.a.clock_ms(); self.a.wait_ms(1050)
            require(self.a.clock_ms()-waited >= 1050, 'abandonment preceded first unresolved wait bound')
            # Peer pending uses the same numeric corr on another route. It is not abandoned.
            peer_request = self.peer.request(self.peer.core['clock'],corr=struct.unpack_from('<H',requests[0],1)[0])
            peer_epoch = ledger.track(self.a.peer,peer_request,boot)
            self.a.hold(self.a.peer,0); self.a.submit([(self.a.peer,peer_request)])
            pre = ledger.snapshot()
            affected = ledger.abandon(self.a.primary)
            expected = requests[1:] if mode == 'prefix' else requests
            require([row['request'] for row in affected] == expected and all(row['status']=='unknown' and row['response'] is None for row in affected),
                    'not all unresolved requests became unknown')
            after = ledger.snapshot()
            require(after[-1] == pre[-1] and after[-1]['status']=='pending', 'abandonment affected peer request')
            if mode == 'prefix': require(after[0] == pre[0] and after[0]['status']=='completed', 'known prefix was erased')
            stable = ledger.snapshot()
            require(ledger.abandon(self.a.primary) == [] and ledger.snapshot() == stable, 'repeat abandonment changed results')
            for frame in raw:
                require(not ledger.accept(self.a.primary,epoch,frame), 'late old-reader reply was adopted')
            require(ledger.snapshot() == stable, 'late reply rewrote unknown/known results')
            self.a.close(self.a.primary); self.c.session = None
            require(self.a.pending(self.a.primary) == [] and self.a.receive(self.a.primary) == [], 'closed route leaked credit/output')
            require(self.a.pending(self.a.peer) == [peer_request], 'closed route discarded peer credit')
            self.service(); self.a.hold(self.a.peer,None); self.service()
            replies = self.a.receive(self.a.peer)
            require(len(replies)==1, 'peer pending result lost'); self.result(replies[0],peer_request)
            require(len(replies[0]) >= 17 and struct.unpack_from('<I',replies[0],5)[0] == boot, 'peer clock payload/boot')
            tlvs(replies[0][17:])
            require(ledger.accept(self.a.peer,peer_epoch,replies[0]), 'unaffected peer result rejected')
            state = self.state()
            executed = 0 if mode == 'queued' else 2
            require(state['next_resource_id']==before['next_resource_id']+executed and len(state['resources'])==executed,
                    'source execution differs from queued/executed fixture')
            require(state['holder']==sid, 'logical close released session')
            inventory = SampleRecoveryHost(self.peer,self.a.functions).inventory()  # SID 0 only; S never moves to peer
            require(inventory==state['resources'], 'peer readback differs from oracle')
            self.batch_observation = {'mode':mode,'records':encoded(ledger.snapshot()),'inventory':encoded(inventory),
                                      'allocator_advance':executed,'late_replies_ignored':len(raw),'full_conformance':False}
            self.a.trace.append({'method':'host_abandonment','observation':self.batch_observation})
            try:
                self.takeover(sid,boot)  # explicit fixture cleanup of our abandoned S, not recovery-host behavior
            finally:
                self.end_peer()

    make_pending_host = staticmethod(SamplePendingHost)

    def run(self):
        def controls():
            require(getattr(self.a,'batch_mode',None) in ('none','prefix','queued') and
                    all(callable(getattr(self.a,name,None)) for name in ('arm_transition','disarm_transition','pending','close')),
                    'explicit logical transition/batch controls required')
        self.c.check('CORE-RECOVERY-RACE-CONTROL','explicit logical fixture',controls)
        if self.c.results[-1]['status'] != 'passed': self.c.abort = True
        report = super().run()
        if report['status'] != 'passed': self.c.abort = True
        cases = [(point.upper(),lambda point=point:self.forced_readback(point)) for point in
                 ('before-first-clock','after-first-inventory','before-second-clock')]
        cases.append(('BATCH-'+self.a.batch_mode.upper(),self.abandoned_batch))
        for name,method in cases:
            first,peer_first = len(self.a.trace),len(self.peer.trace)
            self.c.check('CORE-RECOVERY-RACE-'+name,'core §4.1/4.4/5.2/6; explicit host sample',method)
            self.c.results[-1]['adapter_trace'] = self.a.trace[first:]
            self.c.results[-1]['peer_exchanges'] = self.peer.trace[peer_first:]
        report.update(status='passed' if all(row['status']=='passed' for row in self.c.results) else 'failed',
                      checks=self.c.results,adapter_trace=self.a.trace,peer_exchanges=self.peer.trace,
                      recovery_decisions=self.decisions,batch_observation=getattr(self,'batch_observation',None),
                      scope='wire readback during force and route-wide unknown-result accounting')
        report['unchecked'] = ['physical timeout/framing/reconnect and reader cancellation','production Host integration',
                               'arbitrary concurrent writers/atomic snapshots','other batch sizes and retention limits',
                               'arbitrary extension recovery semantics']
        return report
