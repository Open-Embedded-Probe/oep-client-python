"""Explicit retention instrumentation; no import of the probe model."""
from .conformance_lease_sample import SampleLeaseAdapter


class SampleRetentionAdapter(SampleLeaseAdapter):
    def retention_state(self):
        return self.call('retention_state')
