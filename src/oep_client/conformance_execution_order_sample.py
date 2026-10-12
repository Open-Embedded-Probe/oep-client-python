"""Explicit controls for logical processing order; no model imports."""
from .conformance_pipeline_sample import SamplePipelineAdapter


class SampleExecutionOrderAdapter(SamplePipelineAdapter):
    reset_scope = 'logical-model'

    def restart_fixture(self): return self.call('restart_fixture')
    def execution_state(self): return self.call('execution_state')
