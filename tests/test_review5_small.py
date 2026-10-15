"""Review 5 (fixture, plan, restart, link, probe-config against oep-spec a09fa45): code divergences found and fixed,
each test failing on the code before the fix."""

import struct

import pytest

from oep_client import message as m
from oep_client.virtual_core import Reject
from test_plan import APPLY, GPIO, assignment, bench, raw


# ---- plan §2.4 / core §4.3: "unsupported（受け取ったままの tag）" - the refused assignment's own tag -------------------

def test_plan_unsupported_names_the_tag_of_the_refused_assignment():
    """Two assignments of one fn, the first sent without bit 7 and accepted, the second with bit 7 and a role the gpio
    does not have: the refusal names 0x90, the tag of the TLV refused. The bench named the fn's first tag (0x10)."""
    ep, hst = bench()
    r = raw(hst, APPLY, assignment(GPIO, 1, 4, critical=False) + assignment(GPIO, 2, 5, critical=True))
    assert r.detail == m.UNSUPPORTED and r.payload[0] == 0x90 and ep.plan == set()
    r = raw(hst, APPLY, assignment(GPIO, 1, 4, critical=True) + assignment(GPIO, 2, 5, critical=False))
    assert r.detail == m.UNSUPPORTED and r.payload[0] == 0x10 and ep.plan == set()


# ---- fixture §2 / core §2.3: "後続の同じtagも上の検証対象である。" (a later format copy) ------------------------------------

def _uart():
    from test_conformance_uart import setup
    return setup()


def _call(model, c, fn, op, payload, session):
    return model.handle(c.request(op, payload, fn=fn, session=session))


def test_the_uart_model_checks_a_later_copy_of_the_format():
    """configure with format 4 and a later copy 0xff (bit 7): the later copy is checked too - an undefined format is
    unsupported, the tag as received (0x81), nothing changes. The model skipped every later copy; a later valid copy
    leaves the first in use."""
    from oep_client.conformance_uart import UartChecks
    from oep_client.conformance_uart_sample import SampleUartAdapter
    model, c = _uart()
    checker = UartChecks(c, SampleUartAdapter(model))
    checker.cases = ('FIRST-WINS',)
    r = checker.run()
    assert next(row for row in r['checks'] if row['id'] == 'OEP-UART-FIRST-WINS')['status'] == 'passed'
    e = model.extension
    e.plans[2] = [0, 1]
    from oep_client.virtual_stream_model import Stream
    e.stream = Stream(model.ep, 0, 0)
    tags = e.request_tags(2, 16, m.tlv(1, b'\x04') + m.tlv(1, b'\xff', critical=True))
    with pytest.raises(Reject) as ex:
        e.dispatch_request(2, 16, struct.pack('<I', 115200), tags)
    assert (ex.value.reason, ex.value.payload) == (11, b'\x81') and e.baud == 0


def test_the_uart_checker_requires_a_later_bad_format_copy_refused():
    """The checker's FIRST-WINS case required a configure with a later 0xff format copy to succeed (the old rule: later
    copies ignored); it now requires unsupported 0x81 and catches a model that skips later copies."""
    from oep_client.conformance_uart import UartChecks
    from oep_client.conformance_uart_sample import SampleUartAdapter
    model, c = _uart()
    install = model.install

    def skipping(model, install):
        install()
        e = model.extension
        original = e.request_tags

        def first_only(fn, op, data):
            tags = original(fn, op, data)
            if isinstance(tags, dict):
                tags.pop('later', None)
            return tags
        e.request_tags = first_only
    model.install = skipping
    checker = UartChecks(c, SampleUartAdapter(model))
    checker.cases = ('FIRST-WINS',)
    r = checker.run()
    assert next(row for row in r['checks'] if row['id'] == 'OEP-UART-FIRST-WINS')['status'] == 'failed'


# ---- plan §2.1: "同じ(fn, role, channel)のまま残る割り当ては解放も再取得もせず" (the uart model) ---------------------------

def test_the_uart_model_keeps_a_kept_tx_through_an_unconfigured_replacement():
    """Plan RX + TX (not configured), then TX alone: TX (fn 2, role 2, channel 1) stays - not released to its idle and
    taken again (no 'idle' / 'uart-tx-idle' events for it, its pin high throughout); RX leaves to its idle; the
    buffer goes, the position stays. The model released every channel and took TX again."""
    model, c = _uart()
    e = model.extension
    e.plan(16, b'', [(16, struct.pack('<HBH', 2, 1, 0)), (16, struct.pack('<HBH', 2, 2, 1))])
    e.stream.feed(b'abc')
    before = list(e.events)
    e.plan(16, b'', [(16, struct.pack('<HBH', 2, 2, 1))])
    new = e.events[len(before):]
    assert ('idle', 1, e.idle[1]) not in new and ('uart-tx-idle', 1) not in new, new
    assert ('idle', 0, e.idle[0]) in new and e.pins[1] == 4
    assert e.plans[2] == [1] and e.stream.position == 3 and not e.stream.data


# ---- fixture §3 / §4 configure [TLV] and core §2.3 "後続の同じtagも上の検証対象である。" (the i2c / spi models) -----------

@pytest.mark.parametrize('kind', ['i2c', 'spi'])
def test_the_target_models_refuse_a_later_critical_copy_of_an_unknown_tag(kind):
    """configure with an unknown tag 0x7e, then the same tag with bit 7 (0xfe): the later copy is checked too - an
    unknown critical TLV is unsupported with its tag as received. The i2c / spi models skipped every later copy of a
    tag they had seen and took the configure."""
    if kind == 'i2c':
        from test_conformance_i2c import setup
    else:
        from test_conformance_spi import setup
    model, c = setup()
    e = model.extension
    with pytest.raises(Reject) as ex:
        e.request_tags(2, 16, m.tlv(0x7e, b'x') + m.tlv(0x7e, b'x', critical=True))
    assert (ex.value.reason, ex.value.payload) == (11, b'\xfe')
    assert e.request_tags(2, 16, m.tlv(0x7e, b'x') + m.tlv(0x7e, b'y')) == []


@pytest.mark.parametrize('kind', ['i2c', 'spi'])
def test_the_target_checkers_require_a_later_critical_copy_refused(kind):
    """The checkers' TLV case now sends a non-critical 0x7e then 0xfe and requires unsupported 0xfe; a model that skips
    later copies fails it."""
    if kind == 'i2c':
        from oep_client.conformance_i2c import I2cChecks as Checker
        from oep_client.conformance_i2c_sample import SampleI2cAdapter as Adapter
        from test_conformance_i2c import setup
        prefix = 'OEP-I2C-'
    else:
        from oep_client.conformance_spi import SpiChecks as Checker
        from oep_client.conformance_spi_sample import SampleSpiAdapter as Adapter
        from test_conformance_spi import setup
        prefix = 'OEP-SPI-'
    def skipping(model, install):
        install()
        e = model.extension

        def first_only(fn, op, data):
            if fn == 1:
                return type(e).request_tags(e, fn, op, data)
            seen = set()
            while data:
                raw, size = struct.unpack_from('<BH', data)
                data = data[3 + size:]
                if raw & 127 in seen:
                    continue
                seen.add(raw & 127)
                if raw & 128:
                    raise Reject(11, bytes((raw,)))
            return []
        e.request_tags = first_only
    model, c = setup()
    checker = Checker(c, Adapter(model))
    checker.cases = ('TLV',)
    assert next(r for r in checker.run()['checks'] if r['id'] == prefix + 'TLV')['status'] == 'passed'
    model, c = setup()
    model.install = lambda install=model.install, model=model: skipping(model, install)
    checker = Checker(c, Adapter(model))
    checker.cases = ('TLV',)
    assert next(r for r in checker.run()['checks'] if r['id'] == prefix + 'TLV')['status'] == 'failed'
