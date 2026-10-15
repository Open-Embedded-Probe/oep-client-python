"""Review 6 (fixture, link, probe-config): checkers must accept what a09fa45 lets a probe add, and the save model must
not lose requests that arrive while it writes.

core §4.3 (a09fa45): 表の `—` は固定部分のフィールドが無いことを表す。応答の固定部分の後ろへ TLV を足す §2.3 の規則は、固定部分が空の成功応答と
rejected にも掛かる。知らない応答 TLV は読み飛ばす。 / unavailable の payload（任意の TLV の並び。... probe は分かる範囲で付ける）
"""
import struct

import pytest

from oep_client import virtual_core
from oep_client.virtual_core import tlv

EXTRA = tlv(0x7e, b'x')          # an answer TLV the checker does not know (core §2.3: skipped)


def extended(monkeypatch, success=(), refusals=True):
    """Every virtual_core answer: a success of (fn, op) in `success` gets EXTRA after its fixed part, and (refusals) an
    extension's rejected unavailable / unsupported gets the fn TLV 0x05 (core §4.3) after what it had."""
    handle = virtual_core.Core.handle

    def wrapped(self, data, transport, **kwargs):
        reply = handle(self, data, transport, **kwargs)
        if reply is None or len(data) < 10:
            return reply
        fn, op = struct.unpack_from('<HB', data, 3)
        if reply[3] == 1 and reply[4] == 0 and (fn, op) in success and len(reply) + len(EXTRA) <= 64:
            return reply + EXTRA                               # where it fits every route's max_frame (64 or more)
        if refusals and fn and reply[3] == 0 and reply[4] in (4, 11):
            return reply + tlv(5, struct.pack('<H', fn))
        return reply
    monkeypatch.setattr(virtual_core.Core, 'handle', wrapped)


def passed(r):
    assert r['status'] == 'passed', [(x['id'], x.get('error')) for x in r['checks'] if x['status'] != 'passed']


# ---- link §1/§2: source len(u16) data [TLV], sink — [TLV] ----------------------------------------------------------
def test_link_routes_accept_a_source_answer_tail(monkeypatch):
    from oep_client.conformance_link_routes import LinkRoutesChecks
    from oep_client.conformance_link_routes_sample import SampleLinkRoutesAdapter
    from test_conformance_link_routes import setup
    extended(monkeypatch, success={(4, 16), (4, 17)})
    model, c = setup()
    passed(LinkRoutesChecks(c, SampleLinkRoutesAdapter(model)).run())


def test_port_speed_execution_accepts_source_and_sink_answer_tails(monkeypatch):
    from oep_client.conformance_port_speed_execution import PortSpeedExecutionChecks
    from oep_client.conformance_port_speed_execution_sample import SamplePortSpeedExecutionAdapter
    from test_conformance_port_speed_execution import setup
    extended(monkeypatch, success={(4, 16), (4, 17)})
    model, c = setup()
    passed(PortSpeedExecutionChecks(c, SamplePortSpeedExecutionAdapter(model)).run())


# ---- link §3: 「拒否」はunavailable cause 6 (a TLV list; the probe may add the fn TLV) ---------------------------------
def test_port_speed_accepts_more_tlvs_in_its_refusals_and_a_source_tail(monkeypatch):
    from oep_client.conformance_port_speed import PortSpeedChecks
    from oep_client.conformance_port_speed_sample import SamplePortSpeedAdapter
    from test_conformance_port_speed import setup
    extended(monkeypatch, success={(4, 16)})
    model, c = setup()
    passed(PortSpeedChecks(c, SamplePortSpeedAdapter(model)).run())


def test_port_speed_lifecycle_accepts_more_tlvs_in_its_refusals(monkeypatch):
    from oep_client.conformance_port_speed_lifecycle import PortSpeedLifecycleChecks
    from oep_client.conformance_port_speed_sample import SamplePortSpeedAdapter
    from test_conformance_port_speed import setup
    extended(monkeypatch)
    model, c = setup()
    passed(PortSpeedLifecycleChecks(c, SamplePortSpeedAdapter(model)).run())


def test_port_speed_still_requires_cause_6(monkeypatch):
    from oep_client.conformance_port_speed import PortSpeedChecks
    from oep_client.conformance_port_speed_sample import SamplePortSpeedAdapter
    from test_conformance_port_speed import setup
    handle = virtual_core.Core.handle

    def cause5(self, data, transport, **kwargs):
        reply = handle(self, data, transport, **kwargs)
        if reply is not None and reply[3] == 0 and reply[4] == 4 and data[3:6] == b'\4\0\x12':
            return reply[:5] + tlv(1, b'\5')
        return reply
    monkeypatch.setattr(virtual_core.Core, 'handle', cause5)
    model, c = setup()
    checks = PortSpeedChecks(c, SamplePortSpeedAdapter(model));checks.cases = ('STATES',)
    assert checks.run()['status'] == 'failed'


# ---- probe-config §3: erase 応答の固定部分は無く、末尾に[TLV]; save の unsupported は tag(u8)、[TLV] -----------------------
def test_config_checkers_accept_an_erase_answer_tail(monkeypatch):
    from oep_client.conformance_config import ConfigChecks
    from oep_client.conformance_config_fallback import ConfigFallbackChecks
    from oep_client.conformance_config_fallback_sample import SampleConfigFallbackAdapter
    from oep_client.conformance_config_sample import SampleConfigAdapter
    from oep_client.conformance_config_storage import ConfigStorageChecks
    from oep_client.conformance_config_storage_sample import SampleConfigStorageAdapter
    import test_conformance_config
    import test_conformance_config_storage
    # erase, and the plan / gpio answers the config checkers make on the way (plan §2, fixture §1: — [TLV])
    extended(monkeypatch, success={(4, 19), (1, 16), (1, 17), (2, 16)})
    model, c = test_conformance_config.setup()
    passed(ConfigChecks(c, SampleConfigAdapter(model)).run())
    model, c = test_conformance_config.setup()
    passed(ConfigFallbackChecks(c, SampleConfigFallbackAdapter(model)).run())
    model, c = test_conformance_config_storage.setup()
    passed(ConfigStorageChecks(c, SampleConfigStorageAdapter(model)).run())


def test_config_execution_accepts_more_tlvs_after_the_unsupported_tag(monkeypatch):
    from oep_client.conformance_config_execution import ConfigExecutionChecks
    from oep_client.conformance_config_execution_sample import SampleConfigExecutionAdapter
    from test_conformance_config_execution import setup
    extended(monkeypatch)
    model, c = setup()
    checks = ConfigExecutionChecks(c, SampleConfigExecutionAdapter(model));checks.cases = ('TLV', 'CAPACITY')
    passed(checks.run())


# ---- core §4.4 with probe-config §3: requests that come while save writes are answered after it, not lost ----------
def test_save_execution_answers_the_requests_it_held_after_its_own_answer():
    from oep_client.virtual_config_execution_model import ConfigExecutionModel
    model = ConfigExecutionModel()
    confirm = struct.pack('<BHHBI', 1, 7, 0, 1, 0) + b'OEP?\1\1'
    opened = model.handle(struct.pack('<BHHBI', 1, 1, 0, 16, 5) + struct.pack('<IB', 60000, 0))
    assert opened[3:5] == b'\1\0'
    model.arm(1000, [(1, confirm)])
    save = struct.pack('<BHHBI', 1, 2, 4, 0x12, 5)
    reply = model.handle(save)
    assert reply[3:5] == b'\1\0'
    late = [e for e in model.events if e['action'] == 'held-result']
    assert [(e['route'], bytes.fromhex(e['request_hex'])) for e in late] == [(1, confirm)]
    assert late[0]['reply_hex'] is not None and bytes.fromhex(late[0]['reply_hex'])[3:5] == b'\1\0'
    sent = next(i for i, e in enumerate(model.events) if e['action'] == 'sent')
    assert model.events.index(late[0]) > sent                     # in order: the save's answer first


def test_save_execution_checker_requires_held_requests_answered():
    from oep_client.conformance_config_execution import ConfigExecutionChecks
    from oep_client.conformance_config_execution_sample import SampleConfigExecutionAdapter
    from test_conformance_config_execution import setup
    model, c = setup()
    model.answer_held = lambda: None                            # the old model: what came during the write is lost
    checks = ConfigExecutionChecks(c, SampleConfigExecutionAdapter(model));checks.cases = ('RUN-1500-60000',)
    assert checks.run()['status'] == 'failed'


# ---- fixture §1-§4: rejected payloads are TLV lists the probe may extend --------------------------------------------
@pytest.mark.parametrize('name', ['gpio', 'uart', 'i2c', 'spi'])
def test_fixture_checkers_accept_more_tlvs_in_refusals(monkeypatch, name):
    import importlib
    test = importlib.import_module(f'test_conformance_{name}')
    checker = {'gpio': ('conformance_gpio', 'GpioChecks', 'conformance_gpio_sample', 'SampleGpioAdapter'),
               'uart': ('conformance_uart', 'UartChecks', 'conformance_uart_sample', 'SampleUartAdapter'),
               'i2c': ('conformance_i2c', 'I2cChecks', 'conformance_i2c_sample', 'SampleI2cAdapter'),
               'spi': ('conformance_spi', 'SpiChecks', 'conformance_spi_sample', 'SampleSpiAdapter')}[name]
    checks = getattr(importlib.import_module('oep_client.' + checker[0]), checker[1])
    adapter = getattr(importlib.import_module('oep_client.' + checker[2]), checker[3])
    extended(monkeypatch)
    model, c = test.setup()
    passed(checks(c, adapter(model)).run())


@pytest.mark.parametrize('payload,expected,ok', [
    (b'\1\1\0\6\5\2\0\2\0', b'\1\1\0\6', True), (b'\5\2\0\2\0\1\1\0\6', b'\1\1\0\6', True),
    (b'\1\1\0\5', b'\1\1\0\6', False), (b'', b'\1\1\0\6', False)],
    ids=['more-after', 'more-before', 'other-cause', 'none'])
def test_refusal_matcher(payload, expected, ok):
    from oep_client.conformance_registered_ops import refusal_carries
    assert refusal_carries('unavailable', payload, expected) is ok
    assert refusal_carries('unsupported', b'\xfe' + payload, b'\xfe' + expected) is ok
    assert refusal_carries('unsupported', b'\x00' + payload, b'\xfe' + expected) is False
