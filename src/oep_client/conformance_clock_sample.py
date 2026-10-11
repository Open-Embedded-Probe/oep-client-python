"""Explicit logical clock/read/writer observations; independent of model imports."""
from .conformance_route_sample import SampleRouteAdapter


class SampleClockAdapter(SampleRouteAdapter):
    reset_scope='logical-model'
    reference='docs/clock-conformance.ja.md exact logical ns sample'
    def restart_fixture(self):return self.call('restart_fixture')
    def boot(self, value):return self.call('clock_boot',value)
    def state(self):return self.call('clock_state')
    def advance(self, value):return self.call('clock_advance',value)
    def delays(self, execution, delivery):return self.call('clock_delays',execution,delivery)
    def tick(self):return self.call('clock_tick')
