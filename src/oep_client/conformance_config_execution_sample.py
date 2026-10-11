"""Explicit execution/writer/clock controls; never reads equipment settings."""
from .conformance_config_sample import SampleConfigAdapter


class SampleConfigExecutionAdapter(SampleConfigAdapter):
    def prepare(self):
        self.model.erase_config();self.model.events=[];self.model.writes=0
        return self.restart_fixture()
    def state(self):return self.model.execution_state()
    def arm(self,duration,probes):return self.model.arm(duration,probes)
    def advance(self,delta):return self.model.advance(delta)
    def begin(self,request):return self.model.begin(request)
    def finish(self):return self.model.finish()
