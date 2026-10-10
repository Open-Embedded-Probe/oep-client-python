"""Explicit block memory and scratch-register fixture controls."""
from .conformance_debug_sample import SampleDebugAdapter


class SampleBlockAdapter(SampleDebugAdapter):
    reference='docs/block-conformance.ja.md logical optional block contract'
    max_length=8
    def state(self):return self.call('block_state')
    def memory(self,parent,values):return self.call('block_memory',parent,values)
    def scratch(self,parent,values):return self.call('block_scratch',parent,values)
    def block_failure(self,index,status=2):return self.call('block_failure',index,status)
