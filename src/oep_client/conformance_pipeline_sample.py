"""Explicit admission instrumentation, independent of model implementation."""
from .conformance_route_sample import SampleRouteAdapter


class SamplePipelineAdapter(SampleRouteAdapter):
    admission_policy = 'reject-overflow'

    def pending(self, route):
        return self.call('pending', route)
