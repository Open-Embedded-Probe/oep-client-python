"""Explicit logical step seeds and completion/fallback stimulus."""
from .conformance_block_sample import SampleBlockAdapter


class SampleStepAdapter(SampleBlockAdapter):
    reference='docs/step-conformance.ja.md logical optional step contract'
    def state(self):return self.call('step_state')
    def seed_step(self,parent,pc,dcsr):return self.call('step_seed',parent,pc,dcsr)
    def step_behavior(self,mode,after):return self.call('step_behavior',mode,after)
