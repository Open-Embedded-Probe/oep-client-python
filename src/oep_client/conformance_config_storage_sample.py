"""Explicit backend snapshots; no flash access, timing or equipment discovery."""
import copy
from .conformance_config_sample import SampleConfigAdapter
from .virtual_config_model import encoded


class SampleConfigStorageAdapter(SampleConfigAdapter):
    def prepare(self):
        self.model.erase_config();self.model.storage.reset()
        return self.restart_fixture()
    def storage_snapshot(self):return copy.deepcopy(self.model.storage.blob)
    def state(self):
        s=self.model.storage
        return dict(super().state(),backend=dict(write_count=len(s.writes),erase_count=s.erases,
                    blob=encoded(s.blob),writes=[encoded(blob) for blob in s.writes]))
