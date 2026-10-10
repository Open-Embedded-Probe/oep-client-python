"""Instrumented logical message writer; not an OS/USB/TCP framing model."""
from collections import deque
import copy

from .virtual_data_model import Data, DataModel


class Sink:
    def __init__(self, model):
        self.model = model

    def append(self, frame):
        fn = int.from_bytes(frame[1:3], 'little')
        self.model.offer(self.model.extension.routes.get(fn), frame)

    def clear(self):
        for pipe in self.model.pipes.values():
            pipe['push'].clear()
            # Finish any message already started; never interleave frame bytes.


class RoutedData(Data):
    name = b'io.github.open-embedded-probe.routes'

    def __init__(self, model):
        super().__init__(model.ep.now, model.ep.probe.max_frame)
        self.model = model
        self.routes = {}
        self.pending = Sink(model)

    def dispatch(self, fn, op, payload):
        value = super().dispatch(fn, op, payload)
        if op == 1:
            self.routes[fn] = self.model.active_route
        elif op == 2:
            self.routes.pop(fn, None)
        return value

    def release(self):
        super().release()
        self.routes.clear()


class RouteModel(DataModel):
    def __init__(self, now_ms=None, boot_id=None):
        from .endpoint import Endpoint
        from .virtual_bench import core_v1, with_unit_id
        from .virtual_core import Core
        super().__init__(now_ms, boot_id)
        self.ep = Endpoint(with_unit_id(core_v1(), 'virtual-routes-1'), self.ep.now, boot_id=self.ep.boot_id)
        from .virtual_core import tlv
        self.ep.static[0] = tuple(tlv(73, b'\0\x06\xff') if row[0] == 73 else row
                                  for row in self.ep.static[0])
        self.pipes = {name: {'open': True, 'budget': None, 'active': None,
                            'result': deque(), 'push': deque(), 'out': []}
                      for name in ('primary', 'peer')}
        self.requests = deque()
        self.active_route = None
        self.extension = RoutedData(self)
        self.ep.current_core = Core(self.ep, self.extension)

    def offer(self, route, frame):
        if route not in self.pipes or not self.pipes[route]['open']:
            return
        if sum(len(part) for part in self.queue(route)) + len(frame) <= self.ep.probe.max_frame * 2:
            self.pipes[route]['push'].append(frame)

    def queue(self, route):
        pipe = self.pipes[route]
        remaining = list(pipe['push'])
        if pipe['active'] is not None:
            frame, offset = pipe['active']
            if frame[0] in (3, 4):
                remaining.insert(0, frame[offset:])
        return remaining

    def submit(self, requests):
        for route, request in requests:
            if not self.pipes[route]['open']:
                raise OSError('closed logical route')
            self.requests.append((route, request))

    def service(self):
        self.ep.current_core.tick()
        while self.requests:
            route, request = self.requests.popleft()
            self.active_route = route
            response = self.ep.handle(request)
            if response is not None and self.pipes[route]['open']:
                self.pipes[route]['result'].append(response)
        self.extension.pump()
        for route in self.pipes:
            self.write(route)

    def write(self, route):
        pipe = self.pipes[route]
        if not pipe['open']:
            return
        budget = pipe['budget']
        while budget is None or budget > 0:
            if pipe['active'] is None:
                queue = pipe['result'] if pipe['result'] else pipe['push']
                if not queue:
                    break
                pipe['active'] = (queue.popleft(), 0)
            frame, offset = pipe['active']
            size = len(frame) - offset if budget is None else min(len(frame) - offset, budget)
            offset += size
            if budget is not None:
                budget -= size
            if offset == len(frame):
                pipe['out'].append(frame)
                pipe['active'] = None
            else:
                pipe['active'] = (frame, offset)
        pipe['budget'] = budget

    def hold(self, route, budget):
        self.pipes[route]['budget'] = budget

    def receive(self, route):
        frames, self.pipes[route]['out'] = self.pipes[route]['out'], []
        return frames

    def exchange(self, route, request):
        self.submit([(route, request)])
        self.service()
        frames = self.receive(route)
        if len(frames) != 1 or frames[0][0] != 2:
            raise ValueError('single-result exchange cannot filter unexpected notifications')
        return frames[0]

    def handle(self, request):
        return self.exchange('primary', request)

    def close(self, route):
        pipe = self.pipes[route]
        pipe['open'] = False
        pipe['active'] = None
        for field in ('result', 'push', 'out'):
            pipe[field].clear()

    def state(self):
        core = self.ep.current_core
        return copy.deepcopy({'holder': core.holder, 'cache': list(core.cache.items()),
                              'subscriptions': self.extension.subscriptions,
                              'routes': self.extension.routes, 'resources': self.extension.live})

    def drain(self):
        raise ValueError('route-specific receive required')

    def reboot(self, boot_id):
        raise ValueError('reboot requires a new route-model instance')
