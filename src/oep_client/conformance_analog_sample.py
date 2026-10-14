"""Explicit ADC sample adapter; does not import any probe implementation."""
from .conformance_route_sample import SampleRouteAdapter


class SampleAnalogAdapter(SampleRouteAdapter):
    reset_scope='logical-model'
    selection={1:'oep.probe.plan',2:'oep.fixture.analog'}
    reference='docs/analog-conformance.ja.md explicit ADC rows'
    def restart_fixture(self):return self.call('restart_fixture')
    def state(self):return self.call('analog_state')
    def feed(self,samples):return self.call('analog_feed',samples)
    def advance(self,ms):return self.call('analog_advance',ms)
    def fail(self,error):return self.call('analog_fail',error)
    def seed(self,generation):return self.call('analog_seed',generation)
    def factory(self,size):return self.call('analog_factory',size)
