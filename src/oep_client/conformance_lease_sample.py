"""Explicit clock/wait controls for logical writer lease checks."""
import time
from .conformance import require
from .conformance_pipeline_sample import SamplePipelineAdapter


class SampleLeaseAdapter(SamplePipelineAdapter):
    def __init__(self, source, *, clock_ms=None, wait_ms=None, service_budget_ms=100):
        super().__init__(source, service_budget_ms=service_budget_ms)
        self.clock_ms = clock_ms or (lambda: time.monotonic_ns() // 1_000_000)
        self._wait = wait_ms or (lambda ms: time.sleep(ms / 1000))

    def timing_state(self):
        return self.call('timing_state')

    def wait_ms(self, ms):
        require(type(ms) is int and 1 <= ms <= 2500, 'bounded explicit wait required')
        before = self.clock_ms()
        row = {'method': 'wait_ms', 'arguments': [ms], 'clock_before_ms': before,
               'started_monotonic_ns': time.monotonic_ns()}
        self.trace.append(row)
        try:
            self._wait(ms)
            after = self.clock_ms()
            row['clock_after_ms'] = after
            require(type(before) is int and type(after) is int and after - before >= ms,
                    'fixture wait/clock did not advance requested interval')
        except Exception as exc:
            row['error'] = f'{type(exc).__name__}: {exc}'
            raise
        finally:
            row['elapsed_ns'] = time.monotonic_ns() - row['started_monotonic_ns']
