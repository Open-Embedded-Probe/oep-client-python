"""Explicit logical reply loss and a wire-only sample recovery host."""
import struct
import time
from .conformance import require, tlvs
from .conformance_retention_lifecycle_sample import SampleRetentionLifecycleAdapter


class SampleRecoveryAdapter(SampleRetentionLifecycleAdapter):
    def discard_reply(self, request):
        started = time.monotonic_ns()
        reply = self.exchange(self.primary, request)
        self.trace.append({'method': 'discard_reply', 'request_hex': request.hex(),
                           'discarded_response_hex': reply.hex(), 'delivered_to_host': False,
                           'scope': 'logical complete reply loss', 'started_monotonic_ns': started,
                           'elapsed_ns': time.monotonic_ns() - started})
        # Intentionally no return value: the recovery host cannot use the original result.


class SampleRecoveryHost:
    """Only sample wire reads; no model state, allocator, cache or control access.

    Observed postconditions are not reconstructed command outcomes. Attribution
    of an added resource needs the explicitly controlled single-writer fixture.
    """
    def __init__(self, checks, functions):
        self.c, self.functions = checks, tuple(functions)

    def inventory(self):
        result, ids = {}, set()
        for fn in self.functions:
            payload = self.c.success(self.c.request(19, fn=fn))
            require(payload and len(payload) == 1 + 3 * payload[0], 'sample inventory shape')
            for at in range(1, len(payload), 3):
                rid, kind = struct.unpack_from('<HB', payload, at)
                require(rid and rid not in ids and kind in (1, 2), 'sample inventory duplicate/invalid resource')
                ids.add(rid); result[rid] = (fn, kind)
        return result

    def live_session(self, sid, boot):
        frame = self.c.exchange(self.c.request(self.c.core['clock'], session=sid))
        if frame[3] == self.c.reg['resolutions']['rejected']:
            require(frame[4] in (self.c.reasons['no_session'], self.c.reasons['locked']),
                    'unexpected recovery clock refusal')
            return 'session-unavailable'
        require(frame[3:5] == b'\x01\0' and len(frame) >= 17, 'recovery clock success shape')
        tlvs(frame[17:])
        return 'live' if struct.unpack_from('<I', frame, 5)[0] == boot else 'boot-changed'

    def recover(self, sid, boot, before, *, action, slot, rid=None, single_writer=False):
        require(action in ('create', 'close') and slot[0] in self.functions and slot[1] in (1, 2),
                'explicit sample action/slot required')
        self.c.confirm(); self.c.identity()
        if self.c.boot != boot:
            return {'status': 'boot-changed', 'original_outcome': 'unknown', 'inventory': None}
        status = self.live_session(sid, boot)
        if status != 'live':
            return {'status': status, 'original_outcome': 'unknown', 'inventory': None}
        after = self.inventory()
        # These reads are not an atomic snapshot. The sample controls all writers;
        # check boot/session again before returning any resource bindings.
        self.c.confirm(); self.c.identity()
        if self.c.boot != boot:
            return {'status': 'boot-changed', 'original_outcome': 'unknown', 'inventory': None}
        status = self.live_session(sid, boot)
        if status != 'live':
            return {'status': status, 'original_outcome': 'unknown', 'inventory': None}
        candidate = None
        if action == 'create':
            added = set(after) - set(before)
            unchanged = all(after.get(key) == value for key, value in before.items())
            if after == before:
                status = 'unchanged-state'
            elif single_writer and unchanged and len(added) == 1 and after[next(iter(added))] == slot:
                status, candidate = 'observed-added', next(iter(added))
            else:
                status = 'ambiguous-state'
        else:
            require(rid in before and before[rid] == slot, 'close baseline resource/slot required')
            if rid not in after and after == {key: value for key, value in before.items() if key != rid}:
                status = 'observed-absent'
            elif after == before:
                status = 'still-present'
            else:
                status = 'ambiguous-state'
        return {'status': status, 'original_outcome': 'unknown', 'inventory': after,
                'candidate_resource_id': candidate}
