"""Explicit backend snapshots; no flash access, timing or equipment discovery."""
import copy
from .conformance_config_sample import SampleConfigAdapter


def encoded(blob):
    if blob is None:return None
    return dict(items={f'{tag}:{key.hex()}':v.hex() for (tag,key),v in blob['items'].items()},
                identities={str(fn):list(row) for fn,row in blob['identities'].items()})


class SampleConfigStorageAdapter(SampleConfigAdapter):
    def prepare(self):
        self.model.erase_config();self.model.storage.reset()
        self.model.catalog={1:('oep.probe.plan',0,1),2:('oep.fixture.gpio',0,1),3:('oep.fixture.gpio',1,1),4:('oep.probe.config',0,1)}
        return self.restart_fixture()
    def storage_snapshot(self):return copy.deepcopy(self.model.storage.blob)
    def state(self):
        s=self.model.storage
        return dict(super().state(),backend=dict(write_count=len(s.writes),erase_count=s.erases,
                    blob=encoded(s.blob),writes=[encoded(blob) for blob in s.writes]))
    def change_catalog(self,case):
        if case=='REMAP':self.model.catalog[2],self.model.catalog[3]=self.model.catalog[3],self.model.catalog[2]
        elif case=='IDENTITY':self.model.catalog[2]=('oep.fixture.gpio',0,2)
        else:raise ValueError('explicit catalog scenario required')
