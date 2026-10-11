"""Explicit bounded execution-time control for the logical port fixture."""
from .conformance_port_speed_sample import SamplePortSpeedAdapter


class SamplePortSpeedExecutionAdapter(SamplePortSpeedAdapter):
    def execution(self,duration):self.model.execution(duration)
