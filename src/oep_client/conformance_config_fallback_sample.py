"""Explicit RAM storage/catalog perturbations; never alters physical equipment."""
import copy
from .conformance_config_sample import SampleConfigAdapter


CATALOG={1:('oep.probe.plan',0,1),2:('oep.fixture.gpio',0,1),
         3:('oep.fixture.gpio',1,1),4:('oep.probe.config',0,1)}


class SampleConfigFallbackAdapter(SampleConfigAdapter):
    def prepare(self):
        self.model.saved=None;self.model.saved_hash=0;self.model.saved_identities={}
        self.model.catalog=dict(CATALOG)
        return self.restart_fixture()
    def perturb(self,case):
        m=self.model
        if case=='FORMAT':m.saved[(2,b'\4\0')]=b'\4\0'
        elif case=='MISSING':m.catalog.pop(2)
        elif case=='NAME':m.catalog[2]=('io.example.other-gpio',0,1)
        elif case=='INSTANCE':m.catalog[2]=('oep.fixture.gpio',7,1)
        elif case=='REVISION':m.catalog[2]=('oep.fixture.gpio',0,2)
        elif case=='APPLY':m.saved[(7,b'\1\0')]=b'\1\0'
        elif case=='REMAP':m.catalog[2],m.catalog[3]=m.catalog[3],m.catalog[2]
        elif case=='UNRELATED':m.catalog[3]=('oep.fixture.gpio',1,2)
        else:raise ValueError('unknown explicit fallback scenario')
    def storage_snapshot(self):
        return copy.deepcopy(dict(items=self.model.saved,identities=self.model.saved_identities))
    def state(self):
        return dict(super().state(),catalog={str(fn):list(row) for fn,row in self.model.catalog.items()},
                    saved_identities={str(fn):list(row) for fn,row in self.model.saved_identities.items()})
