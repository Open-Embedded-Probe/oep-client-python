"""Explicit attach option and transient reset-pin fixture controls."""
from .conformance_attach_sample import SampleAttachAdapter


class SampleWireOptionsAdapter(SampleAttachAdapter):
    reference='docs/wire-options-conformance.ja.md logical attach options contract'
    pairs={**SampleAttachAdapter.pairs,3:[(4,5),(2,3)]}
    def state(self):return self.call('wire_options_state')
    def behavior(self,value):return self.call('wire_behavior',value)
    def reset_support(self,value):return self.call('wire_reset_support',value)
    def pin(self,channel,mode):return self.call('wire_pin',channel,mode)
