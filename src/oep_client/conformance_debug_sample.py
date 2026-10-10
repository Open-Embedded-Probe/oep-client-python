"""Explicit logical standard riscv-dm and console arbitration controls."""
from .conformance_dmseq_sample import SampleDmseqAdapter


class SampleDebugAdapter(SampleDmseqAdapter):
    reference='docs/debug-conformance.ja.md logical standard dmi/halt/resume contract'
    debug_fn=5
    def state(self):return self.call('debug_state')
    def failure(self,index,status=2):return self.call('debug_failure',index,status)
    def registers(self,parent,values):return self.call('debug_registers',parent,values)
    def poll(self,rid):return self.call('debug_console_poll',rid)
    def hart_failure(self,value):return self.call('debug_hart_failure',value)
