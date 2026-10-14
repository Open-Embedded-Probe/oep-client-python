"""Explicit RAM storage perturbations; never alters physical equipment."""
import copy
from .conformance_config_sample import SampleConfigAdapter


class SampleConfigFallbackAdapter(SampleConfigAdapter):
    def prepare(self):
        self.model.erase_config();self.model.hold_max=None
        return self.restart_fixture()
    def perturb(self,case):
        # Each case changes the saved items as an older firmware or a torn write might have left them (fixture form only).
        m=self.model;saved=m.saved
        if case=='FORMAT':saved.append(b'torn')
        elif case=='UNKNOWN':saved.insert(0,(1,b'\2\0\1\3\0'))
        elif case=='CHANNEL':saved.append((2,b'\x09\0far'))
        elif case=='MODE':saved.append((3,b'\2\0\7'))
        elif case=='LABEL':saved.append((2,b'\5\0'))
        elif case=='CONFLICT':saved.append((7,b'\1\0'))
        elif case=='DUPLICATE':saved.append((2,b'\4\0other'))
        elif case=='CAPACITY':saved.append((2,b'\0\0pwr'));m.hold_max=3
        else:raise ValueError('unknown explicit fallback scenario')
    def storage_snapshot(self):
        return copy.deepcopy(self.model.saved)
    def state(self):
        return dict(super().state(),hold_max=self.model.hold_max)
