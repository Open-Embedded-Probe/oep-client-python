"""Explicit observation adapter for the logical GPIO/plan fixture."""
from .conformance_route_sample import SampleRouteAdapter


class SampleGpioAdapter(SampleRouteAdapter):
    reset_scope='logical-model'
    selection={1:'oep.probe.plan',2:'oep.fixture.gpio',3:'oep.fixture.gpio'}
    plan_fn=1
    gpio_fns=(2,3)
    reference='docs/gpio-conformance.ja.md logical fixture contract'
    def restart_fixture(self):return self.call('restart_fixture')
    def state(self):return self.call('gpio_state')
