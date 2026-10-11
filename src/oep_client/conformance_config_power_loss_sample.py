"""Explicit logical loss of volatile state while durable journal bytes remain."""
from .conformance_config_sample import SampleConfigAdapter


class SampleConfigPowerLossAdapter(SampleConfigAdapter):
    def prepare(self):
        self.model.erase_config();self.model.events=[]
        return self.restart_fixture()
    def write_steps(self):return self.model.write_steps()
    def cut_save(self,request,step):return self.model.cut_save(request,step)
    def power_cycle(self):return self.model.power_cycle()
    def state(self):
        c=self.model.ep.current_core
        return dict(super().state(),journal_image=self.model.journal.image(),
                    core=dict(holder=c.holder,last=c.last,cache_keys=list(c.cache)))
