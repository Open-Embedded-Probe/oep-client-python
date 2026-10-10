"""Explicit logical transition scheduling and host pending-result bookkeeping."""
import copy
import struct
import time
from .conformance import require
from .conformance_recovery_sample import SampleRecoveryAdapter


class SampleRecoveryRaceAdapter(SampleRecoveryAdapter):
    points = ('before-first-clock', 'after-first-inventory', 'before-second-clock')

    def __init__(self, source, *, batch_mode='none', **kwargs):
        super().__init__(source, **kwargs)
        require(batch_mode in ('none', 'prefix', 'queued'), 'explicit logical batch delivery mode required')
        self.batch_mode = batch_mode
        self.transition = None

    def arm_transition(self, point, sid, callback):
        require(self.reset_scope == 'logical-model' and point in self.points and
                type(sid) is int and 1 <= sid <= 0xffffffff and callable(callback) and self.transition is None,
                'explicit single logical transition required')
        self.transition = {'point': point, 'sid': sid, 'callback': callback, 'clocks': 0}
        self.trace.append({'method': 'arm_transition', 'point': point, 'session_id': sid})

    def disarm_transition(self):
        self.transition = None

    def trigger(self):
        event, self.transition = self.transition, None
        row = {'method': 'trigger_transition', 'point': event['point'],
               'session_id': event['sid'], 'started_monotonic_ns': time.monotonic_ns()}
        self.trace.append(row)
        try:
            event['callback']()
            row['completed'] = True
        except Exception as exc:
            row['error'] = f'{type(exc).__name__}: {exc}'
            raise
        finally:
            row['elapsed_ns'] = time.monotonic_ns() - row['started_monotonic_ns']

    def exchange(self, route, request):
        event = self.transition if route == self.primary else None
        if event:
            _, _, fn, op, sid = struct.unpack_from('<BHHBI', request)
            if fn == 0 and op == 4 and sid == event['sid']:
                event['clocks'] += 1
                wanted = 1 if event['point'] == 'before-first-clock' else 2
                if event['point'].startswith('before-') and event['clocks'] == wanted:
                    self.trigger()
        reply = super().exchange(route, request)
        if event and self.transition is event and event['point'] == 'after-first-inventory' and fn == self.functions[0] and op == 19:
            self.trigger()
        return reply


class SamplePendingHost:
    """Ledger for already decoded logical messages; no probe or transport access.

    Local epochs label reader lifetimes. They are not wire fields. Resuming an
    epoch requires the caller to complete transport recovery and confirm boot.
    """
    def __init__(self, registry, limits):
        self.reg = registry
        self.limits = copy.deepcopy(limits)
        require(limits and all(set(value) == {'max_frame', 'window', 'max_inflight'} and
                all(type(n) is int and n > 0 for n in value.values()) for value in limits.values()),
                'explicit per-route limits required')
        self.epochs = {route: 0 for route in limits}
        self.boots = {route: None for route in limits}
        self.quarantined = set()
        self.retired_sessions = set()
        self.records = []

    def snapshot(self):
        return copy.deepcopy(self.records)

    def track(self, route, request, boot):
        require(route in self.limits and route not in self.quarantined, 'route needs explicit recovery before admission')
        require(isinstance(request, bytes) and 10 <= len(request) <= self.limits[route]['max_frame'], 'request shape/frame limit')
        role, corr, _, _, sid = struct.unpack_from('<BHHBI', request)
        require(role == self.reg['roles']['request'] and corr and type(boot) is int and 0 <= boot <= 0xffffffff,
                'request role/corr/boot')
        require(self.boots[route] is None or self.boots[route] == boot, 'boot changed without explicit recovery')
        require(not sid or sid not in self.retired_sessions, 'abandoned session must not be reused')
        pending = [row for row in self.records if row['route'] == route and row['status'] == 'pending']
        require(all(row['corr'] != corr for row in pending), 'duplicate unresolved corr on route')
        require(len(pending) < self.limits[route]['max_inflight'] and
                sum(len(row['request']) for row in pending) + len(request) <= self.limits[route]['window'],
                'host admission exceeds route bounds')
        epoch = self.epochs[route]
        self.boots[route] = boot
        self.records.append({'route': route, 'epoch': epoch, 'boot_id': boot, 'session_id': sid,
                             'corr': corr, 'request': request, 'status': 'pending', 'response': None})
        return epoch

    def accept(self, route, epoch, frame):
        require(route in self.limits, 'unknown route')
        if route in self.quarantined or epoch != self.epochs[route]:
            return False
        require(isinstance(frame, bytes) and 5 <= len(frame) <= self.limits[route]['max_frame'], 'decoded result shape')
        role, corr, resolution, detail = struct.unpack_from('<BHBB', frame)
        require(role == self.reg['roles']['result'] and resolution in self.reg['resolutions'].values(), 'result role/resolution')
        allowed = self.reg['outcomes'].values() if resolution == self.reg['resolutions']['completed'] else self.reg['reject_reasons'].values()
        require(detail in allowed or (resolution == self.reg['resolutions']['rejected'] and 0x40 <= detail <= 0x7f), 'result detail')
        row = next((row for row in self.records if row['route'] == route and row['epoch'] == epoch and
                    row['corr'] == corr and row['status'] == 'pending'), None)
        if row is None: return False
        status = 'completed' if resolution == self.reg['resolutions']['completed'] else 'rejected'
        if resolution == self.reg['resolutions']['rejected'] and detail == self.reg['reject_reasons']['result_lost']:
            status = 'unknown'  # A known result_lost reply does not resolve the original operation.
        row.update(status=status, response=frame)
        return True

    def abandon(self, route):
        require(route in self.limits, 'unknown route')
        if route in self.quarantined: return []
        affected = []
        for row in self.records:
            if row['route'] == route and row['status'] == 'pending':
                row.update(status='unknown', response=None)
                if row['session_id']: self.retired_sessions.add(row['session_id'])
                affected.append(copy.deepcopy(row))
        self.quarantined.add(route)
        self.epochs[route] += 1
        return affected

    def resume(self, route, *, transport_recovered, confirmed_boot):
        require(route in self.quarantined and transport_recovered is True and
                type(confirmed_boot) is int and 0 <= confirmed_boot <= 0xffffffff,
                'explicit recovered transport and confirmed boot required')
        self.quarantined.remove(route)
        self.boots[route] = confirmed_boot
