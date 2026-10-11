"""Explicit allocator boundary controls; no probe-model imports."""
from .conformance_resource_sample import SampleResourceAdapter
from .conformance_route_sample import SampleRouteAdapter


class SampleAllocatorAdapter(SampleResourceAdapter,SampleRouteAdapter):
    reset_scope='logical-model'
    reference='docs/allocator-conformance.ja.md'
    def __init__(self, checks, source):
        SampleResourceAdapter.__init__(self,checks,functions=(1,2),kinds=(1,2),capacity=8)
        SampleRouteAdapter.__init__(self,source)
    def restart_fixture(self):return self.call('restart_fixture')
    def seed(self, value):return self.call('seed_allocator',value)
    def state(self):return self.call('allocator_state')
    def advance(self, value):return self.call('advance_ms',value)
