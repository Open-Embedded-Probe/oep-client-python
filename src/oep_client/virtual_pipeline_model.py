"""Conservative admission sample: retain credit until whole result is written.

Overflow rejection is an explicit sample policy, not mandatory probe behavior.
Only the sample uses different route limits; no public firmware or socket changes.
"""
import struct
from .virtual_route_model import RouteModel


class PipelineModel(RouteModel):
    def __init__(self, now_ms=None, boot_id=None):
        super().__init__(now_ms, boot_id)
        self.limits = {'primary': (80, 3), 'peer': (96, 2)}
        self.unresolved = {route: [] for route in self.pipes}

    def pending(self, route):
        return list(self.unresolved[route])

    def submit(self, requests):
        for route, request in requests:
            if not self.pipes[route]['open']:
                raise OSError('closed logical route')
            window, count = self.limits[route]
            reason = None
            if len(request) > self.ep.probe.max_frame:
                reason = 3
            elif len(self.unresolved[route]) >= count or sum(map(len, self.unresolved[route])) + len(request) > window:
                reason = 6
            if reason is None:
                self.unresolved[route].append(request)
            self.requests.append((route, request, reason))

    def service(self):
        self.ep.current_core.tick()
        while self.requests:
            route, request, reason = self.requests.popleft()
            self.active_route = route
            previous = self.ep.window, self.ep.max_inflight
            self.ep.window, self.ep.max_inflight = self.limits[route]
            try:
                response = self.ep.current_core.handle(request, 0, admission_reason=reason)
            finally:
                self.ep.window, self.ep.max_inflight = previous
            if response is not None and self.pipes[route]['open']:
                self.pipes[route]['result'].append(response)
        self.extension.pump()
        for route in self.pipes:
            self.write(route)

    def write(self, route):
        first = len(self.pipes[route]['out'])
        super().write(route)
        for frame in self.pipes[route]['out'][first:]:
            if frame[0] != 2:
                continue
            corr = struct.unpack_from('<H', frame, 1)[0]
            for index, request in enumerate(self.unresolved[route]):
                if struct.unpack_from('<H', request, 1)[0] == corr:
                    self.unresolved[route].pop(index)
                    break

    def close(self, route):
        super().close(route)
        self.unresolved[route].clear()
        self.requests = type(self.requests)(row for row in self.requests if row[0] != route)

    def state(self):
        value = super().state()
        core = self.ep.current_core
        value.update(last=core.last, high=core.high, deadline=core.deadline)
        return value
