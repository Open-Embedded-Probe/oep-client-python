"""Explicit logical UART input, error and queue controls."""
from .conformance_route_sample import SampleRouteAdapter


class SampleUartAdapter(SampleRouteAdapter):
    reset_scope='logical-model'
    selection={1:'oep.probe.plan',2:'oep.fixture.uart'}
    reference='docs/uart-conformance.ja.md logical fixture contract'
    def restart_fixture(self):return self.call('restart_fixture')
    def state(self):return self.call('uart_state')
    def feed_bytes(self,data):return self.call('uart_feed',data)
    def error(self,detail):return self.call('uart_error',detail)
    def fail_configure(self):return self.call('uart_fail_configure')
    def drain(self,count):return self.call('uart_drain',count)
    def advance(self,ms):return self.call('uart_advance',ms)
