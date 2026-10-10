"""Explicit logical ndmreset outcome and DM recovery stimulus."""
from .conformance_step_sample import SampleStepAdapter


class SampleResetAdapter(SampleStepAdapter):
    reference='docs/reset-conformance.ja.md logical optional reset contract'
    def state(self):return self.call('reset_state')
    def reset_behavior(self,result):return self.call('reset_behavior',result)
