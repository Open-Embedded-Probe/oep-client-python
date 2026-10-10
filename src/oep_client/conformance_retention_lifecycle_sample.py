"""Logical restart control; no firmware or equipment discovery."""
from .conformance_retention_sample import SampleRetentionAdapter


class SampleRetentionLifecycleAdapter(SampleRetentionAdapter):
    reset_scope = 'logical-model'

    def restart(self, boot_id):
        result = self.call('restart', boot_id)
        self.positions = {fn: 0 for fn in self.functions}
        return result
