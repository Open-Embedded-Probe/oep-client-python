"""Explicit paired-sample group adapter, without implementation imports."""
from .conformance_route_sample import SampleRouteAdapter


class SampleCaptureGroupAdapter(SampleRouteAdapter):
    reset_scope='logical-model'
    selection={1:'oep.probe.plan',2:'oep.fixture.analog',3:'oep.fixture.logic',4:'oep.fixture.capture-group'}
    reference='docs/capture-group-conformance.ja.md explicit paired samples'
    def restart_fixture(self):return self.call('restart_fixture')
    def state(self):return self.call('group_state')
    def feed(self,rows):return self.call('group_feed',rows)
    def advance(self,ms):return self.call('group_advance',ms)
    def fail(self,fn,error):return self.call('group_fail',fn,error)
    def seed(self,generation,adc,logic):return self.call('group_seed',generation,adc,logic)
    def invalidate(self,fn):return self.call('group_invalidate',fn)
