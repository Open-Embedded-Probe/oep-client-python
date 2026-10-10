"""Explicit logical dmseq mailbox and detected target-reset controls."""
from .conformance_mailbox_sample import SampleMailboxAdapter


class SampleDmseqAdapter(SampleMailboxAdapter):
    reference='docs/dmseq-conformance.ja.md logical probe-side framing contract'
    mechanisms=[0,1,2]
    def state(self):return self.call('dmseq_state')
    def reset_target(self,parent):return self.call('reset_target',parent)
