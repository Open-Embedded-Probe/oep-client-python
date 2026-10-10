"""Explicit instrumentation adapter; no imports of the probe model."""
import struct
import time
from .conformance import require


def encoded(value):
    if isinstance(value, bytes):
        return {'hex': value.hex()}
    if isinstance(value, dict):
        return {str(key): encoded(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [encoded(item) for item in value]
    return value


class SampleRouteAdapter:
    primary, peer = 'primary', 'peer'
    functions = (1, 2)

    def __init__(self, source, *, service_budget_ms=100):
        self.source = source
        self.service_budget_ms = service_budget_ms
        self.trace = []
        self.positions = {fn: 0 for fn in self.functions}

    def call(self, method, *args):
        row = {'method': method, 'arguments': encoded(args), 'started_monotonic_ns': time.monotonic_ns()}
        self.trace.append(row)
        try:
            result = getattr(self.source, method)(*args)
            row['result'] = encoded(result)
            return result
        except Exception as exc:
            row['error'] = f'{type(exc).__name__}: {exc}'
            raise
        finally:
            row['elapsed_ns'] = time.monotonic_ns() - row['started_monotonic_ns']

    def exchange(self, route, request):
        self.submit([(route, request)])
        self.service()
        frames = self.receive(route)  # record all bytes before checking the exchange shape
        require(isinstance(frames, (list, tuple)) and len(frames) == 1 and
                isinstance(frames[0], bytes) and frames[0][:1] == b'\x02',
                'single-result exchange cannot filter unexpected notifications')
        return frames[0]
    def hold(self, route, budget): return self.call('hold', route, budget)
    def submit(self, requests): return self.call('submit', requests)
    def service(self): return self.call('service')
    def receive(self, route): return self.call('receive', route)
    def queue(self, route): return self.call('queue', route)
    def stimulate(self, fn, marker): return self.call('emit', fn, marker)
    def close(self, route): return self.call('close', route)
    def state(self): return self.call('state')

    def feed(self, fn, data):
        position = self.positions[fn]
        self.call('feed', fn, position, data)
        self.positions[fn] += len(data)
        return position

    def create_resource(self, checks, sid):
        payload = checks.success(checks.request(16, b'\x01', session=sid, fn=self.functions[0]))
        require(len(payload) == 2, 'sample resource response')
        return struct.unpack('<H', payload)[0]
