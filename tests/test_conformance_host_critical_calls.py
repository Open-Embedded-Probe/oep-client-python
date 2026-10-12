"""Core §2.3 critical policy at actual GPIO/UART/debug Host call sites.

The peer checks wire bytes against literal expectations and returns unsupported
for a selected critical field. It does not model electrical effects or certify
all optional arguments of every interface.

Public interface clients still carry legacy operation numbers. This core-policy
test deliberately does not certify those numbers against the current SPEC;
that implementation follow-up remains separate from critical-bit validation.
"""
import struct

import pytest

from oep_client import arm, catalog, core, fixture, host, message as m, riscv


CASES = [
    ('gpio', tag) for tag in (0x81,)
] + [('uart', 0x81)] + [
    (kind, tag) for kind, tags in (
        ('rvswd', (0x81, 0x83, 0x84, 0x85)),
        ('swio', (0x81, 0x83, 0x85)),
        ('swd', (0x81, 0x83, 0x85, 0x82)),
    ) for tag in tags
]


def exercise_call(kind, refused_tag):
    names = {'gpio': 'oep.fixture.gpio', 'uart': 'oep.fixture.uart',
             'rvswd': 'oep.wire.rvswd', 'swio': 'oep.wire.swio', 'swd': 'oep.wire.swd'}
    # Literal protocol oracle: independent of catalog/Host TLV encoders.
    expected = {
        'gpio': bytes.fromhex('02 0100 04 0200 03 81 0200 0000 81 0200 0101'),
        'uart': bytes.fromhex('00c20100 81 0100 05'),
        'rvswd': bytes.fromhex('00 81 0400 40420f00 83 0400 01000200 84 0100 01 85 0400 03000a00'),
        'swio': bytes.fromhex('00 81 0400 40420f00 83 0400 0100ffff 85 0400 03000a00'),
        'swd': bytes.fromhex('00 81 0400 40420f00 83 0400 01000200 85 0400 03000a00 82 0400 78563412'),
    }[kind]
    sent = []

    def send(raw):
        sent.append(raw)
        role, corr, fn, operation, sid = struct.unpack('<BHHBI', raw[:10])
        assert (role, corr, fn, sid) == (1, 1, 9, 7), 'request header differs'
        assert raw[10:] == expected, 'request payload lost required critical policy'
        return b'\2\1\0\0\x0b' + bytes((refused_tag,))

    h = host.Host(send)
    h.revision = 1
    h.session = 7
    h._fns[names[kind]] = 9
    h._revisions[9] = 1
    h._describes[9] = [(catalog.OPS, catalog.pack_ops({1, 2}))]
    h._describes[0] = [(core.CORE_MAX_OP_MS, struct.pack('<I', 1000))]
    with pytest.raises(host.Unsupported) as error:
        if kind == 'gpio':
            fixture.Gpio(h).set([(1, 4, 0), (2, 3, 1)])
        elif kind == 'uart':
            fixture.FixtureUart(h).configure(115200, fmt=5)
        elif kind == 'swd':
            arm.SwdWire(h).attach(max_speed=1_000_000, pins=(1, 2), reset=(3, 10), targetsel=0x12345678)
        else:
            riscv.Wire(h, names[kind]).attach(halt=False, max_speed=1_000_000,
                pins=(1, 0xffff if kind == 'swio' else 2), reset=(3, 10),
                idle_clock='low' if kind == 'rvswd' else None)
    assert error.value.tag == refused_tag, 'unsupported did not preserve the received critical tag'
    assert len(sent) == 1, 'unsupported request retried or fell back without the requested field'
    assert h.session == 7 and h.epoch == 0
    return sent[0]


@pytest.mark.parametrize('kind,tag', CASES)
def test_public_calls_send_critical_fields_and_surface_refusal(kind, tag):
    exercise_call(kind, tag)


@pytest.mark.parametrize('kind,tag', CASES)
def test_critical_field_assertion_detects_dropped_bit(monkeypatch, kind, tag):
    original = m.tlv
    def drop_bit(number, value, *, critical=False):
        return original(number, value, critical=False if number == tag & 127 else critical)
    monkeypatch.setattr(m, 'tlv', drop_bit)
    with pytest.raises(AssertionError, match='request payload lost required critical policy'):
        exercise_call(kind, tag)
