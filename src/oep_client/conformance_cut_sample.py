"""Explicit deferred writer and console wire-loss controls."""
from .conformance_retry_sample import SampleRetryAdapter


class SampleCutAdapter(SampleRetryAdapter):
    reference='docs/cut-conformance.ja.md logical response-before-cut contract'
    def state(self):return self.call('cut_state')
    def accept(self,request):return self.call('accept_request',request)
    def poll_cut(self,rid):return self.call('cut_console_poll',rid)
    def tick(self):return self.call('cut_tick')
