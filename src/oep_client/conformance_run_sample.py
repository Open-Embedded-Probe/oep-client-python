"""Explicit logical loader outcome and program-effect stimulus."""
from .conformance_reset_sample import SampleResetAdapter


class SampleRunAdapter(SampleResetAdapter):
    reference='docs/run-conformance.ja.md logical optional run contract'
    def state(self):return self.call('run_state')
    def run_behavior(self,mode,pc):return self.call('run_behavior',mode,pc)
    def program(self,parent,memory,gprs):return self.call('run_program',parent,memory,gprs)
