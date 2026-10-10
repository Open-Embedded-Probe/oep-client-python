"""Explicit finite candidate scan fixture controls; no wiring discovery."""
from .conformance_attach_sample import SampleAttachAdapter


class SampleScanAdapter(SampleAttachAdapter):
    reference='docs/scan-conformance.ja.md finite logical scan contract'
    def state(self):return self.call('scan_state')
    def scan_budget(self, limit):return self.call('scan_budget',limit)
    def scan_id(self, fn, pair, identity):return self.call('scan_id',fn,pair,identity)
    def pin_mode(self, pin, mode):return self.call('pin_mode',pin,mode)
