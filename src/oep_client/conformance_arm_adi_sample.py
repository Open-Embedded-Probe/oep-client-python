"""Explicit ARM logical controls; no model imports or implicit device discovery."""
from .conformance_attach_sample import SampleAttachAdapter


class SampleArmAdiAdapter(SampleAttachAdapter):
    selection = dict(SampleAttachAdapter.selection) | {5:'oep.target.arm-adi'}
    reference = 'docs/arm-adi-conformance.ja.md logical fixture contract'
    wait_attempts = 3
    def state(self): return self.call('arm_state')
    def seed(self, parent, dp, ap, pending): return self.call('arm_seed', parent, dp, ap, pending)
    def acks(self, values): return self.call('arm_acks', values)
    def memory(self, parent, values): return self.call('arm_memory', parent, values)
