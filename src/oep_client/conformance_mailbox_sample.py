"""Explicit logical SDI/DMDATA target mailbox controls."""
from .conformance_attach_sample import SampleAttachAdapter


class SampleMailboxAdapter(SampleAttachAdapter):
    reference='docs/mailbox-conformance.ja.md logical SDI/DMDATA contract'
    def state(self):return self.call('mailbox_state')
    def frame(self,rid,data0,data1):return self.call('mailbox_frame',rid,data0,data1)
    def poll(self,rid):return self.call('mailbox_poll',rid)
    def halted(self,rid,value):return self.call('mailbox_halted',rid,value)
    def debug_busy(self,rid,value):return self.call('mailbox_debug_busy',rid,value)
