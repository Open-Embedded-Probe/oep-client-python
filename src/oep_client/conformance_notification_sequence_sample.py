"""Explicit sequence/reset instrumentation; no probe-model imports."""
from .conformance_route_sample import SampleRouteAdapter


class SampleNotificationSequenceAdapter(SampleRouteAdapter):
    reset_scope = 'logical-model'
    reference = 'docs/notification-sequence-conformance.ja.md'

    def restart_fixture(self): return self.call('restart_fixture')
    def seed_sequence(self, fn, value): return self.call('seed_sequence', fn, value)
    def sequence_state(self): return self.call('sequence_state')
