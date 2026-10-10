"""Checker-side sample event decoding; no probe implementation imports."""
import struct
import time

from .conformance import require, tlvs


class SampleSubscriptionAdapter:
    def __init__(self, source, *, functions, observation_ms, wait=time.sleep):
        self.source = source
        self.functions = tuple(functions)
        self.observation_ms = observation_ms
        self.wait = wait

    def stimulate(self, fn, marker):
        self.source.emit(fn, marker)

    def receive(self, timeout_ms):
        self.wait(timeout_ms / 1000)
        return self.source.drain()

    def decode_event(self, frame):
        require(len(frame) >= 10 and frame[5] == 1, 'sample event kind/fixed payload')
        tlvs(frame[10:])
        return struct.unpack_from('<I', frame, 6)[0]
