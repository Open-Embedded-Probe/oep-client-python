"""Explicit logical packet outcome and probe-owned read controls."""
from .conformance_debug_sample import SampleDebugAdapter


class SampleRetryAdapter(SampleDebugAdapter):
    reference='docs/retry-conformance.ja.md logical packet retry contract'
    def state(self):return self.call('retry_state')
    def plan(self,parent,statuses):return self.call('retry_plan',parent,statuses)
    def idle(self,parent,value):return self.call('retry_idle',parent,value)
    def probe_read(self,parent):return self.call('retry_probe_read',parent)
