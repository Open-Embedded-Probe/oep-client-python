"""Raw Probe stimuli for bool readers; ordinary Hosts emit force 0 or 1."""
import copy
import struct

import pytest

from oep_client.virtual_resource_model import ResourceModel


def request(corr, fn, op, sid, payload=b''):
    return struct.pack('<BHHBI', 1, corr, fn, op, sid) + payload


def exercise_force(value, *, defect=None):
    now = [0]
    model = ResourceModel(lambda: now[0], boot_id=17)
    core = model.ep.current_core
    if defect is not None:
        handle = core.handle
        def broken(data, transport, **kwargs):
            fn, op, sid = struct.unpack_from('<HBI', data, 3)
            selected = fn == 0 and op == 16 and sid == 8
            if selected and defect == 'reject_noncanonical' and data[14] > 1:
                return b'\x02' + data[1:3] + b'\0\x03'
            if selected and defect == 'one_only':
                data = data[:14] + bytes((int(data[14] == 1),)) + data[15:]
            if selected and defect == 'zero_true':
                data = data[:14] + b'\x01' + data[15:]
            return handle(data, transport, **kwargs)
        core.handle = broken
    opened = request(1, 0, 16, 7, struct.pack('<IB', 3000, 0) + b'\x01\x03\0old')
    assert model.handle(opened) == b'\x02\x01\0\x01\0' + struct.pack('<II', 3000, 17)
    created = request(2, 1, 16, 7, b'\x01')
    assert model.handle(created) == b'\x02\x02\0\x01\0\x01\0'
    now[0] = 123
    before = copy.deepcopy((core.holder, core.last, core.owner, core.lease, core.deadline,
                            core.high, list(core.cache.items()), model.extension.live, model.extension.next_id))
    take = request(1, 0, 16, 8, struct.pack('<IB', 1000, value) + b'\x01\x04\0next')
    answer = model.handle(take)
    if value == 0:
        expected = b'\x02\x01\0\0\x08' + struct.pack('<I', 2877) + b'\x01\x03\0old'
        assert answer == expected, 'force zero must refuse the foreign owner'
        after = (core.holder, core.last, core.owner, core.lease, core.deadline,
                 core.high, list(core.cache.items()), model.extension.live, model.extension.next_id)
        assert after == before, 'force zero changed the existing session/resources/history'
        assert model.handle(request(3, 1, 18, 7, b'\x01\x01\0')) == b'\x02\x03\0\x01\0'
        ending = request(4, 0, 17, 7)
    else:
        assert answer == b'\x02\x01\0\x01\0' + struct.pack('<II', 1000, 17), 'nonzero force must take ownership'
        assert (core.holder, core.last, core.owner, core.lease, core.deadline, core.high) == (8, 8, b'next', 1000, 1123, 1)
        assert not model.extension.live and model.extension.next_id == 2, 'force retained resources or reused their allocator'
        assert list(core.cache.items()) == [(1, (take, answer))], 'force did not replace old replay history'
        assert model.handle(request(2, 1, 18, 8, b'\x01\x01\0')) == b'\x02\x02\0\0\x0a'
        assert model.handle(request(3, 1, 16, 8, b'\x01')) == b'\x02\x03\0\x01\0\x02\0'
        ending = request(4, 0, 17, 8)
    assert model.handle(ending) == b'\x02\x04\0\x01\0'
    assert core.holder is None and not model.extension.live
    return dict(force=value, request_hex=take.hex(), reply_hex=answer.hex(),
                branch='locked' if value == 0 else 'taken', cleanup_complete=True)


@pytest.mark.parametrize('value', range(256))
def test_force_u8_reads_all_nonzero_values_as_true(value):
    exercise_force(value)


@pytest.mark.parametrize('defect,value', [('reject_noncanonical', 2), ('one_only', 255), ('zero_true', 0)])
def test_force_boolean_faults_are_detected(defect, value):
    expected = 'force zero' if value == 0 else 'nonzero force'
    with pytest.raises(AssertionError, match=expected):
        exercise_force(value, defect=defect)
