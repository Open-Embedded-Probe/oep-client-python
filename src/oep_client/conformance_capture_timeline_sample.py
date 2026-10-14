"""Explicit per-track nanosecond scheduler adapter, without implementation imports."""
from .conformance_route_sample import SampleRouteAdapter


class SampleCaptureTimelineAdapter(SampleRouteAdapter):
    reset_scope='logical-model'
    selection={1:'oep.probe.plan',2:'oep.fixture.analog',3:'oep.fixture.logic',4:'oep.fixture.capture-group'}
    reference='docs/capture-timeline-conformance.ja.md explicit waveform, clock and notification drain'
    def restart_fixture(self):return self.call('restart_fixture')
    def state(self):return self.call('timeline_state')
    def advance_to(self,ns):return self.call('timeline_advance_to',ns)
    def drain(self):return self.call('timeline_drain')
    def fail(self,fn,error):return self.call('timeline_fail',fn,error)
    def fail_next_start(self,fn,error):return self.call('timeline_fail_next_start',fn,error)
    def seed(self,generation,adc,logic):return self.call('timeline_seed',generation,adc,logic)
