"""Explicit deterministic writer lease controls; no wall-clock sleep."""
from .conformance_cut_sample import SampleCutAdapter


class SampleWriterLeaseAdapter(SampleCutAdapter):
    reference='docs/writer-lease-conformance.ja.md logical standard-resource writer lease contract'
    def state(self):return self.call('writer_lease_state')
    def advance(self,ms):return self.call('writer_advance',ms)
    def execution(self,ms):return self.call('writer_execution',ms)
