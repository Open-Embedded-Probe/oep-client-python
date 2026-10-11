"""Explicit independent per-track waveform and nanosecond scheduler adapter."""
from .conformance_capture_group_sample import SampleCaptureGroupAdapter


class SampleCaptureTimelineAdapter(SampleCaptureGroupAdapter):
    reference='docs/capture-timeline-conformance.ja.md explicit waveform indices and clock'
    def program(self,fn,rise):return self.call('timeline_program',fn,rise)
    def advance_to(self,ns):return self.call('timeline_advance_to',ns)
