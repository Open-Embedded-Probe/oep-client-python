"""Processing order from dependent resource effects on an explicit pipeline fixture."""
import struct
from .conformance import require, tlvs


class ExecutionOrderChecks:
    cases = tuple((phase, budget) for phase in ('CLOSE-USE-CREATE', 'CREATE-CLOSE-USE')
                  for budget in (0, 1, None))

    def __init__(self, checks, adapter):
        self.c, self.a = checks, adapter
        self.observations = []

    def request(self, sid, fn, op, kind, rid=None):
        body = bytes((kind,)) + (b'' if rid is None else struct.pack('<H', rid))
        return self.c.request(op, body, session=sid, fn=fn)

    def state(self):
        value = self.a.execution_state()
        require(all(k in value for k in ('resources', 'next_resource_id', 'cache', 'high', 'holder')),
                'explicit processing/resource observation required')
        return value

    def context(self):
        require(self.a.reset_scope == 'logical-model' and self.a.admission_policy == 'reject-overflow',
                'explicit logical admission fixture required')
        require(tuple(self.a.functions) == (1, 2), 'two sample resource functions required')
        for key in ('restart_fixture', 'execution_state', 'hold', 'submit', 'service', 'receive', 'pending'):
            require(callable(getattr(self.a, key, None)), 'missing processing control: ' + key)

    def case(self, phase, budget):
        reset = self.a.restart_fixture()
        require(reset['scope'] == 'logical-model', 'logical restart required')
        self.c.confirm(); self.c.identity(); self.c.declarations(); self.c.interfaces()
        require(self.c.boot == reset['boot_id'], 'processing fixture boot differs')
        limits = self.c.observed['confirm']
        require((limits['max_frame'], limits['window'], limits['max_inflight']) == (64, 80, 3),
                'explicit processing fixture geometry differs')
        with self.c.holding(60000) as sid:
            seed = self.c.success(self.request(sid, 1, 16, 1))
            require(len(seed) == 2 and int.from_bytes(seed, 'little') != 0, 'processing seed resource shape')
            old_id = int.from_bytes(seed, 'little'); before = self.state(); new_id = before['next_resource_id']
            require(1 <= new_id <= 65535 and new_id != old_id, 'explicit future allocator witness required')
            if phase == 'CLOSE-USE-CREATE':
                requests = [self.request(sid, 1, 17, 1, old_id), self.request(sid, 1, 18, 1, old_id),
                            self.request(sid, 2, 16, 2)]
                expected = [(None, b''), ('no_resource', b''), (None, struct.pack('<H', new_id))]
                resources = {new_id: (2, 2)}
            else:
                requests = [self.request(sid, 2, 16, 2), self.request(sid, 2, 17, 2, new_id),
                            self.request(sid, 2, 18, 2, new_id)]
                expected = [(None, struct.pack('<H', new_id)), (None, b''), ('no_resource', b'')]
                resources = {old_id: (1, 1)}
            require(sum(map(len, requests)) <= limits['window'], 'processing batch exceeded byte credit')
            try:
                self.a.hold(self.a.primary, budget)
                self.a.submit([(self.a.primary, request) for request in requests])
                require(self.a.pending(self.a.primary) == requests, 'processing batch pending bytes differ')
                self.a.service()
                processed = self.state()
                require(processed['resources'] == resources, 'processing order side effect resources differ')
                require(processed['next_resource_id'] == new_id + 1, 'processing order allocator differs')
                frames = self.a.receive(self.a.primary)
                if budget is not None:
                    require(frames == [] and self.a.pending(self.a.primary) == requests,
                            'blocked/partial writer completed results before observation')
                    self.a.hold(self.a.primary, None); self.a.service()
                    frames = self.a.receive(self.a.primary)
                require(len(frames) == len(requests), 'missing processing results')
                for request, frame, (reason, payload) in zip(requests, frames, expected):
                    header = b'\x02' + request[1:3] + (b'\x01\0' if reason is None else bytes((0, self.c.reasons[reason])))
                    require(frame == header + payload, 'processing order result/corr/outcome differs')
                    if reason is not None or not payload: tlvs(frame[5:])
                require(self.a.pending(self.a.primary) == [] and self.a.receive(self.a.peer) == [],
                        'processing result credit/route differs')
                after = self.state()
                for request, saved in zip(requests, frames):
                    require(self.c.exchange(request) == saved, 'processing result replay differs')
                    require(self.state() == after, 'processing replay reapplied dependent effects')
                self.observations.append(dict(phase=phase, writer_budget=budget,
                    request_hex=[request.hex() for request in requests], reply_hex=[frame.hex() for frame in frames],
                    resource_ids=sorted(resources), next_resource_id=processed['next_resource_id'],
                    effects_observed_before_result_completion=budget is not None))
            finally:
                self.a.hold(self.a.primary, None); self.a.service()
                self.a.receive(self.a.primary); self.a.receive(self.a.peer)

    def run(self):
        start = len(self.c.results)
        self.c.check('CORE-EXECUTION-ORDER-CONTEXT', 'core §4.4/9; explicit fixture', self.context)
        if self.c.results[-1]['status'] != 'passed': self.c.abort = True
        for phase, budget in self.cases:
            self.c.check('CORE-EXECUTION-ORDER-' + phase + '-' + str(budget), 'core §4.4/9',
                         lambda phase=phase, budget=budget: self.case(phase, budget))
        rows = self.c.results[start:]
        return dict(status='passed' if all(row['status'] == 'passed' for row in rows) else 'failed',
                    checks=rows, observations=self.observations, adapter_trace=self.a.trace,
                    full_conformance=False, scope='dependent sample resource effects under bounded pipeline/writer states',
                    unchecked=['all operations/concurrent arrivals/long execution', 'physical backend effect timing',
                               'Host behavior that predicts allocated IDs', 'full normative conformance'])
