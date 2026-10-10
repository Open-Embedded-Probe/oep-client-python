"""Interface-independent resource lifetime checks (SPEC core §6/9).

An explicit adapter supplies wire encoding and externally visible enumeration.
No interface names, equipment paths or resource operation numbers are inferred.
"""
import secrets
import struct
import time

from .conformance import require, tlvs


RESOURCE_CASES = (
    ('RESOURCE-SESSION', 'session_required'),
    ('RESOURCE-OWNER', 'owner'),
    ('RESOURCE-ID', 'ids'),
    ('RESOURCE-MISSING', 'missing'),
    ('RESOURCE-WRONG-FN-KIND', 'wrong_owner'),
    ('RESOURCE-CREATE-REPLAY', 'create_replay'),
    ('RESOURCE-CLOSE-REPLAY', 'close_replay'),
    ('RESOURCE-OPEN-RETAIN', 'open_retain'),
    ('RESOURCE-OPEN-REPLAY', 'open_replay'),
    ('RESOURCE-END', 'end'),
    ('RESOURCE-EXPIRY', 'expiry'),
    ('RESOURCE-FORCE', 'force'),
    ('RESOURCE-CAPACITY', 'capacity'),
)


class ResourceChecks:
    def __init__(self, checks, adapter, *, wait=time.sleep):
        self.c, self.a, self.wait = checks, adapter, wait

    def inventory(self):
        inventory = self.a.snapshot()
        require(set(inventory) == set(self.a.slots), 'snapshot omitted configured fn/kind')
        ids = [rid for values in inventory.values() for rid in values]
        require(all(type(rid) is int and 1 <= rid <= 65535 for rid in ids), 'invalid resource ID')
        require(len(ids) == len(set(ids)), 'resource ID space is not global')
        return inventory

    def create(self, sid, slot):
        request = self.a.request('create', sid, slot)
        rid = self.a.decode_created(self.c.success(request))
        require(type(rid) is int and 1 <= rid <= 65535, 'invalid allocated resource ID')
        return rid, request

    def use(self, sid, slot, rid):
        self.a.decode_empty(self.c.success(self.a.request('use', sid, slot, rid)))

    def close(self, sid, slot, rid):
        request = self.a.request('close', sid, slot, rid)
        self.a.decode_empty(self.c.success(request))
        return request

    def empty(self):
        require(not any(self.inventory().values()), 'session left live resources')

    def cause(self, request, cause):
        values = dict(reversed(tlvs(self.c.rejected(request, 'unavailable'))))
        require(values.get(1) == bytes((cause,)), 'unavailable cause differs')

    def session_required(self):
        before = self.inventory()
        slot = self.a.slots[0]
        for action in ('create', 'close', 'use'):
            self.c.rejected(self.a.request(action, 0, slot, 0), 'session_required')
        require(self.inventory() == before, 'unlocked request changed resources')

    def ids(self):
        with self.c.holding() as sid:
            self.empty()
            previous = 0
            for slot in self.a.slots * 2:
                rid, _ = self.create(sid, slot)
                require(rid > previous, 'resource IDs reused or not increasing across fn/kind')
                previous = rid
                require(self.inventory()[slot] == {rid}, 'creation not visible exactly once')
                self.close(sid, slot, rid)
                self.empty()
                self.c.rejected(self.a.request('use', sid, slot, rid), 'no_resource')

    def owner(self):
        with self.c.holding() as sid:
            slot = self.a.slots[0]
            rid, _ = self.create(sid, slot)
            before = self.inventory()
            foreign = sid % 0xffffffff + 1
            for action in ('create', 'use', 'close'):
                self.c.rejected(self.a.request(action, foreign, slot, rid), 'locked')
            require(self.inventory() == before, 'foreign session changed resources')
            self.use(sid, slot, rid)

    def missing(self):
        with self.c.holding() as sid:
            self.empty()
            for slot in self.a.slots:
                for rid in (0, 65535):
                    self.c.rejected(self.a.request('use', sid, slot, rid), 'no_resource')
                    self.c.rejected(self.a.request('close', sid, slot, rid), 'no_resource')
            self.empty()

    def wrong_owner(self):
        with self.c.holding() as sid:
            owner = self.a.slots[0]
            rid, _ = self.create(sid, owner)
            for slot in self.a.slots[1:]:
                for action in ('use', 'close'):
                    self.cause(self.a.request(action, sid, slot, rid), 6)
                self.use(sid, owner, rid)
            require(self.inventory()[owner] == {rid}, 'wrong fn/kind mutated valid resource')

    def create_replay(self):
        with self.c.holding() as sid:
            slot = self.a.slots[0]
            rid, request = self.create(sid, slot)
            before = self.inventory()
            require(self.a.decode_created(self.c.success(request)) == rid, 'replayed create ID changed')
            require(self.inventory() == before, 'replayed create allocated twice')
            self.use(sid, slot, rid)

    def close_replay(self):
        with self.c.holding() as sid:
            slot = self.a.slots[0]
            rid, _ = self.create(sid, slot)
            request = self.close(sid, slot, rid)
            self.empty()
            self.a.decode_empty(self.c.success(request))
            self.c.rejected(self.a.request('close', sid, slot, rid), 'no_resource')
            next_id, _ = self.create(sid, slot)
            require(next_id > rid, 'closed ID reused')
            self.a.decode_empty(self.c.success(request))
            self.use(sid, slot, next_id)
            require(self.inventory()[slot] == {next_id}, 'replayed close affected another resource')

    def open_retain(self):
        with self.c.holding() as sid:
            allocations = [(slot, self.create(sid, slot)[0]) for slot in self.a.slots]
            before = self.inventory()
            opened = self.c.success(self.c.request(self.c.core['open'], struct.pack('<IB', 3000, 0), session=sid))
            require(len(opened) >= 8 and struct.unpack_from('<II', opened) == (3000, self.c.boot),
                    'same-session open response')
            tlvs(opened[8:])
            require(self.inventory() == before, 'same-session open released resources')
            for slot, rid in allocations:
                self.use(sid, slot, rid)

    def open_replay(self):
        with self.c.holding() as sid:
            # The first open must remain in cache; enumeration uses SID 0.
            opened = self.c.request(self.c.core['open'], struct.pack('<IB', 3000, 0), session=sid)
            original = self.c.success(opened)
            slot = self.a.slots[0]
            rid, _ = self.create(sid, slot)
            before = self.inventory()
            require(self.c.success(opened) == original, 'open replay response changed')
            require(self.inventory() == before, 'open replay released resources')
            self.use(sid, slot, rid)

    def ended(self, kind):
        with self.c.holding(1000 if kind == 'expiry' else 3000) as sid:
            allocations = [(slot, *self.create(sid, slot)) for slot in self.a.slots]
            if kind == 'end':
                self.c.success(self.c.request(self.c.core['end'], session=sid))
                self.c.session = None
            elif kind == 'expiry':
                self.wait(1.1)
                state = self.c.success(self.c.request(self.c.core['lock_state']))
                require(len(state) >= 5 and struct.unpack_from('<BI', state) == (0, 0), 'lease did not expire')
                tlvs(state[5:])
                self.c.session = None
            else:
                replacement = secrets.randbelow(0xffffffff) + 1
                while replacement == sid:
                    replacement = secrets.randbelow(0xffffffff) + 1
                self.c.corr = 0
                value = self.c.success(self.c.request(self.c.core['open'], struct.pack('<IB', 3000, 1),
                                                    session=replacement))
                self.c.session = replacement  # clean up the new session even if its reply is malformed
                require(len(value) >= 8 and struct.unpack_from('<II', value) == (3000, self.c.boot),
                        'force open response')
                tlvs(value[8:])
            self.empty()
            if kind != 'force':
                # Replay gives historical IDs, without resurrecting resources.
                slot, rid, request = allocations[-1]
                require(self.a.decode_created(self.c.success(request)) == rid, 'ended-session replay lost')
                self.empty()
                self.c.rejected(self.a.request('use', sid, slot, rid), 'no_session')
            else:
                for slot, rid, _ in allocations:
                    self.c.rejected(self.a.request('use', self.c.session, slot, rid), 'no_resource')
                fresh, _ = self.create(self.c.session, self.a.slots[0])
                require(fresh > max(rid for _, rid, _ in allocations), 'takeover reused resource ID')

    def end(self):
        self.ended('end')

    def expiry(self):
        self.ended('expiry')

    def force(self):
        self.ended('force')

    def capacity(self):
        # This declared fixture limit is not the 65535-ID exhaustion test.
        require(type(self.a.capacity) is int and 1 <= self.a.capacity <= 32,
                'adapter must supply a bounded, reproducible capacity (1..32)')
        with self.c.holding(60000) as sid:
            self.empty()
            allocations = []
            for i in range(self.a.capacity):
                slot = self.a.slots[i % len(self.a.slots)]
                allocations.append((slot, self.create(sid, slot)[0]))
            before = self.inventory()
            self.cause(self.a.request('create', sid, self.a.slots[0]), 2)
            require(self.inventory() == before, 'failed allocation left a partial resource')
            for slot, rid in allocations:
                self.use(sid, slot, rid)
            slot, rid = allocations[0]
            self.close(sid, slot, rid)
            fresh, _ = self.create(sid, slot)
            require(fresh > max(rid for _, rid in allocations), 'allocation after capacity failure reused ID')

    def run(self):
        start = len(self.c.results)
        def identify():
            self.c.confirm()
            self.c.identity()
            self.c.declarations()
            first_interface = len(self.c.results)
            self.c.interfaces()
            require(all(row['status'] == 'passed' for row in self.c.results[first_interface:]),
                    'interface declaration checks failed')
            require(len(self.a.slots) >= 3 and len(set(self.a.slots)) == len(self.a.slots), 'adapter slots')
            first = self.a.slots[0]
            require(all(type(slot[0]) is int and 1 <= slot[0] <= 65535 for slot in self.a.slots),
                    'adapter fn must be a nonzero u16')
            require(any(slot[0] != first[0] for slot in self.a.slots) and
                    any(slot[0] == first[0] and slot[1] != first[1] for slot in self.a.slots),
                    'adapter must exercise different fn and same-fn different kind')
            require(type(self.a.capacity) is int and len(self.a.slots) <= self.a.capacity <= 32,
                    'adapter capacity must hold every slot and be bounded at 32')
            require(all(callable(getattr(self.a, method, None)) for method in
                        ('request', 'snapshot', 'decode_created', 'decode_empty')), 'adapter methods missing')
            fns = {x['fn'] for x in self.c.observed['interfaces']}
            require(all(slot[0] in fns for slot in self.a.slots), 'adapter fn absent from list')
        self.c.check('RESOURCE-IDENTITY', 'core §2/7/9; adapter contract', identify)
        if self.c.results[-1]['status'] != 'passed':
            self.c.abort = True
        for name, method in RESOURCE_CASES:
            self.c.check(name, 'core §4.2/5.2/6/9', getattr(self, method))
        rows = self.c.results[start:]
        return {'status': 'passed' if all(x['status'] == 'passed' for x in rows) else 'failed',
                'full_conformance': False, 'scope': 'resource lifetime on explicit adapter slots',
                'observed': self.c.observed, 'checks': rows,
                'unchecked': ['subscriptions and notifications', 'route closure', 'resource dependency order',
                              'electrical idle', 'reboot', '65535-ID exhaustion', 'unconfigured fn/kind']}
