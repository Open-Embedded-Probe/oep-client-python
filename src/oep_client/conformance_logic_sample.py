"""Explicit logic sample, acquisition failure, clock and generation controls."""
from .conformance_route_sample import SampleRouteAdapter


class SampleLogicAdapter(SampleRouteAdapter):
    reset_scope='logical-model'
    selection={1:'oep.probe.plan',2:'oep.fixture.logic'}
    reference='docs/logic-conformance.ja.md explicit samples'
    def restart_fixture(self):return self.call('restart_fixture')
    def state(self):return self.call('logic_state')
    def feed(self,samples):return self.call('logic_feed',samples)
    def advance(self,ms):return self.call('logic_advance',ms)
    def fail(self,error):return self.call('logic_fail',error)
    def seed(self,generation):return self.call('logic_seed',generation)
