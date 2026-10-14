"""Core §6.5: observe boot changes before using cached function bindings.

The scripted peer supplies protocol replies, not a physical entropy source.
Resource invalidation is witnessed by the Host epoch; resource wrapper behavior
and every stream consumer require their own adoption tests.
"""
import struct

import pytest

from oep_client import catalog, core, host, message as m


NAME = 'example.test.resource'


def exercise_boot_change(observer, before, after, host_type=host.Host):
    state = {'boot': before, 'fn': 7}
    requests = []

    def send(raw):
        req = m.Request.unpack(raw)
        requests.append(req)
        if req.fn == 0 and req.op == m.OP_CONFIRM:
            payload = struct.pack('<4sBHIBI', b'OEP!', 1, 1024, 4096, 4, state['boot'])
            payload += m.tlv(1, b'\0')
        elif req.fn == 0 and req.op == m.OP_CLOCK:
            payload = struct.pack('<IQ', state['boot'], 1234)
        elif req.fn == 0 and req.op == m.OP_OPEN:
            payload = struct.pack('<II', 3000, state['boot'])
        elif req.fn == 0 and req.op == m.OP_LIST:
            first = struct.unpack('<H', req.payload)[0]
            payload = catalog.pack_list_result(1, [catalog.ListEntry(state['fn'], 0, 1, NAME)] if first == 0 else [])
        elif req.fn == 0 and req.op == m.OP_DESCRIBE:
            fn, first = struct.unpack('<HH', req.payload)
            assert fn in (0, state['fn'])                       # fn 0: max_op_ms before open (core §4.4)
            body = catalog.u32(core.CORE_MAX_OP_MS, 1000) if fn == 0 else catalog.ops_tlv({16})
            payload = b'\0' + (body if first == 0 else b'')
        else:
            assert (req.fn, req.op) == (state['fn'], 16), 'Host used an obsolete function binding'
            payload = b''
        return b'\2' + struct.pack('<H', req.corr) + b'\1\0' + payload

    h = host_type(send)
    assert h.confirm()['boot_id'] == before
    assert core.find(h, NAME) == 7
    core.describe(h, 7)
    assert h._fns and h._revisions and h._describes
    h.subscriptions.add(7)
    epoch = h.epoch
    state.update(boot=after, fn=513)
    start = len(requests)
    getattr(h, observer)()
    observed = len(requests)
    assert h._boot_id == after
    assert h.epoch == epoch + 1, 'changed boot did not invalidate resource epoch'
    assert not h._fns and not h._revisions and not h._describes, 'old discovery caches survived boot change'
    assert not h.subscriptions, 'old subscription survived boot change'
    assert core.find(h, NAME) == 513
    h.call(513, 16, locked=False)
    assert all(r.fn == 0 for r in requests[start:observed])          # the observer (open: fn 0's describe first)
    assert [(r.fn, r.op) for r in requests[observed:]] == [(0, m.OP_LIST), (513, 16)], 'Host must rediscover before interface use'
    assert h.epoch == epoch + 1


@pytest.mark.parametrize('observer', ['confirm', 'clock', 'open'])
@pytest.mark.parametrize('before,after', [(17, 0), (0, 17), (0, 0xffffffff)])
def test_boot_observation_invalidates_and_rediscovers(observer, before, after):
    exercise_boot_change(observer, before, after)


@pytest.mark.parametrize('observer', ['confirm', 'clock'])
def test_repeated_zero_boot_preserves_discovery(observer):
    def send(raw):
        req = m.Request.unpack(raw)
        payload = (struct.pack('<4sBHIBI', b'OEP!', 1, 1024, 4096, 4, 0) + m.tlv(1, b'\0')
                   if req.op == m.OP_CONFIRM else struct.pack('<IQ', 0, 1234))
        return b'\2' + struct.pack('<H', req.corr) + b'\1\0' + payload
    h = host.Host(send)
    h.confirm()
    h._fns[NAME] = 7
    h._revisions[7] = 1
    h._describes[7] = [(7, b'cached')]
    h.subscriptions.add(7)
    getattr(h, observer)()
    assert h.epoch == 0 and h._fns == {NAME: 7} and h._revisions == {7: 1}
    assert h._describes == {7: [(7, b'cached')]} and 7 in h.subscriptions


@pytest.mark.parametrize('fault', ['ignore-zero', 'keep-functions', 'keep-revisions', 'keep-describes', 'keep-subscriptions', 'keep-epoch'])
def test_boot_change_assertions_detect_retained_state(fault):
    class BrokenHost(host.Host):
        def boot_id_seen(self, boot):
            if self._boot_id is None:
                return super().boot_id_seen(boot)
            saved = {name: getattr(self, name).copy() for name in ('_fns', '_revisions', '_describes', 'subscriptions')}
            epoch = self.epoch
            if fault == 'ignore-zero' and boot == 0:
                self._boot_id = boot
                return
            super().boot_id_seen(boot)
            field = {'keep-functions': '_fns', 'keep-revisions': '_revisions', 'keep-describes': '_describes',
                     'keep-subscriptions': 'subscriptions'}.get(fault)
            if field:
                setattr(self, field, saved[field])
            if fault == 'keep-epoch':
                self.epoch = epoch
    with pytest.raises(AssertionError, match='invalidate resource epoch|discovery caches survived|subscription survived'):
        exercise_boot_change('clock', 17, 0, BrokenHost)
