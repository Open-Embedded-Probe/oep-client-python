"""Core §2.3 critical policy at actual UART/debug Host call sites (gpio set has no request TLV, fixture §1).

The peer checks wire bytes against literal expectations and returns unsupported
for a selected critical field. It does not model electrical effects or certify
all optional arguments of every interface.

The wire forms are oep-spec a09fa45's (attach: method swdio swclk max_speed, then TLVs).
"""
import struct

import pytest
from spec_names import enum, tag

from oep_client import arm, catalog, core, fixture, host, message as m, riscv


# the tags by name from the selected SPEC (tests/spec_names.py), critical (bit 7)
CRIT = 0x80
UART_FORMAT = tag('oep.fixture.uart', 'configure', 'format') | CRIT
RV_IDLE, RV_RESET = (tag('oep.wire.rvswd', 'attach', k) | CRIT for k in ('idle_clock', 'reset'))
SWIO_RESET = tag('oep.wire.swio', 'attach', 'reset') | CRIT
SWD_RESET, SWD_TARGETSEL = (tag('oep.wire.swd', 'attach', k) | CRIT for k in ('reset', 'targetsel'))
LOW = enum('oep.wire.rvswd', 'idle_clock')['low']

CASES = [('uart', UART_FORMAT)] + [
    (kind, t) for kind, tags in (
        ('rvswd', (RV_IDLE, RV_RESET)),
        ('swio', (SWIO_RESET,)),
        ('swd', (SWD_RESET, SWD_TARGETSEL)),
    ) for t in tags
]


def item(t, value):
    return bytes((t,)) + struct.pack('<H', len(value)) + value


def exercise_call(kind, refused_tag):
    names = {'gpio': 'oep.fixture.gpio', 'uart': 'oep.fixture.uart',
             'rvswd': 'oep.wire.rvswd', 'swio': 'oep.wire.swio', 'swd': 'oep.wire.swd'}
    # The protocol oracle: built from the SPEC's names, independent of catalog/Host TLV encoders.
    attach = lambda swclk: struct.pack('<BHHI', enum('oep.wire.rvswd', 'attach_method')['run'], 1, swclk, 1_000_000)
    reset = lambda t: item(t, struct.pack('<HH', 3, 10))
    expected = {
        'uart': struct.pack('<I', 115200) + item(UART_FORMAT, b'\x05'),
        'rvswd': attach(2) + item(RV_IDLE, bytes((LOW,))) + reset(RV_RESET),
        'swio': attach(0xFFFF) + reset(SWIO_RESET),
        'swd': attach(2) + reset(SWD_RESET) + item(SWD_TARGETSEL, struct.pack('<I', 0x12345678)),
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
        if kind == 'uart':
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
