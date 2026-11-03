"""Fixture, plan, restart, link and settings rules in the host, the virtual bench and the checkers, each test pinned to
the spec sentence it checks."""
import struct
from oep_client import message as m, registry as reg
from test_plan import GPIO, PLAN_FN, RELEASE, bench, raw
import pytest
from oep_client import message as m
from oep_client.virtual_core import Reject
from test_plan import APPLY, GPIO, assignment, bench, raw
from oep_client import virtual_core
from oep_client.virtual_core import tlv


def test_plan_release_naming_fn_0_is_ignored_like_any_existing_fn_without_a_plan():
    """fn 0 exists (core §1: the core is fn 0) and never has a plan: plan_release ignores it - only an fn the probe
    lacks is unknown_function. The bench refused fn 0 as unknown_function."""
    from oep_client import core
    ep, hst = bench()
    core.plan_apply(hst, [(GPIO, 1, 4)])
    assert hst.call(PLAN_FN, RELEASE, struct.pack("<BHH", 2, 0, GPIO)).payload == b"" and ep.plan == set()
    core.plan_apply(hst, [(GPIO, 1, 4)])
    assert hst.call(PLAN_FN, RELEASE, struct.pack("<BH", 1, 0)).payload == b"" and ep.plan == {(GPIO, 1, 4)}
    assert raw(hst, RELEASE, struct.pack("<BHH", 2, 0, 99)).detail == m.UNKNOWN_FUNCTION and ep.plan == {(GPIO, 1, 4)}
    assert PLAN_FN in ep.names


# ---- link §3: "決めた状態で、元の所有sessionが残る間に正常な要求frameを受けた場合だけidle復帰の保留を取り消し" -------------

def _idle_pending(owner_stays=True):
    from oep_client import core, endpoint, host as h, virtual_bench
    from test_port_speed import COMMIT, PS, TRY

    class Clock:
        t = 0

        def __call__(self):
            return self.t
    clock = Clock()
    ep = endpoint.Endpoint(virtual_bench.esp32_v003(), clock)                   # transport 0: its UART bridge
    hst = h.Host(lambda b: ep.handle(b, 0))
    hst.open(60000)
    hst.call(ep.link_fn, PS, struct.pack("<IBH", 500000, TRY, 2000))
    ep.after_answer()
    hst.call(ep.link_fn, PS, struct.pack("<IBH", 500000, COMMIT, 0))
    ep.speed_frame_busy = True                                                  # a frame still on the raised line
    clock.t += 3000                                                             # port_speed_idle_ms: the return is due
    ep.tick()
    assert ep.speed_phase(0) == 3 and ep.speed_fallback == "idle"
    return ep, hst, clock, core


def test_no_request_frame_cancels_an_idle_return():
    """link §3: phase 3 is never cancelled, whatever its reason. A request frame on the raised port
    while the idle return is pending - with or without a session id - is answered at the raised rate (port_state says
    phase 3), and the port goes back at that answer's boundary."""
    for op in ("port_state", "source"):
        ep, hst, clock, core = _idle_pending()
        payload = struct.pack("<I", 4) if op == "source" else b""
        r = h_call(hst, ep.link_fn, reg.PROBE_LINK.op[op], payload)
        if op == "port_state":
            assert struct.unpack_from("<IB", r) == (500000, 3)
        assert ep.speed_phase(0) == 3 and ep.speed_fallback == "idle"
        ep.speed_frame_done()                                                   # the answer's boundary
        assert ep.speed_phase(0) == 0 and ep.port_baud(0) == 115200


def h_call(hst, fn, op, payload):
    """A lock-free request sent with session id 0 -> the answer's payload."""
    sid, hst.session = hst.session, None
    try:
        return hst.call(fn, op, payload).payload
    finally:
        hst.session = sid


# ---- probe-config §1: label = "text（1〜32byte、coreのtext）"; core §2.1: text is UTF-8, the host replaces C0 / 0x7F ----

def _models():
    from oep_client.conformance.analog_model import AnalogModel
    from oep_client.conformance.capture_group_model import CaptureGroupModel
    from oep_client.conformance.capture_timeline_model import CaptureTimelineModel
    from oep_client.conformance.gpio_model import GpioModel
    from oep_client.conformance.i2c_model import I2cModel
    from oep_client.conformance.logic_model import LogicModel
    from oep_client.conformance.spi_model import SpiModel
    from oep_client.conformance.uart_model import UartModel
    return {"gpio": lambda: GpioModel(lambda: 0, boot_id=17), "uart": lambda: UartModel(boot_id=17),
            "spi": lambda: SpiModel(boot_id=17), "i2c": lambda: I2cModel(boot_id=17),
            "logic": lambda: LogicModel(boot_id=17), "analog": lambda: AnalogModel(boot_id=17),
            "capture-group": lambda: CaptureGroupModel(boot_id=17),
            "capture-timeline": lambda: CaptureTimelineModel(boot_id=17)}


def _plan(model, op, payload, tags=()):
    from oep_client.virtual_core import Reject
    e = model.extension
    try:
        (e.plan if hasattr(e, "plan") else lambda o, p, t: e.dispatch_request(1, o, p, t))(op, payload, list(tags))
        return None
    except Reject as r:
        return r.reason


import pytest  # noqa: E402


@pytest.mark.parametrize("name", sorted(_models()))
def test_every_model_refuses_a_plan_release_naming_an_fn_it_lacks_and_ignores_fn_0(name):
    """plan §2.2: "存在しないfnはunknown_function ... 要求全体を何も変えずに断る"; fn 0 exists without a plan: ignored.
    The models released the fns they knew and ignored the rest."""
    model = _models()[name]()
    before = {k: list(v) for k, v in model.extension.plans.items()}
    assert _plan(model, 17, struct.pack("<BHH", 2, 2, 99)) == m.UNKNOWN_FUNCTION
    assert {k: list(v) for k, v in model.extension.plans.items()} == before
    assert _plan(model, 17, struct.pack("<BH", 1, 0)) is None


@pytest.mark.parametrize("name,rows", [
    ("logic", [(2, 0, 0), (2, 0, 1)]), ("logic", [(2, 0, 0), (2, 1, 0)]),
    ("analog", [(2, 1, 0), (2, 1, 1)]), ("analog", [(2, 1, 0), (2, 3, 0)]),
    ("capture-group", [(3, 0, 2), (3, 0, 3)]), ("capture-timeline", [(3, 0, 2), (3, 0, 3)])])
def test_a_capture_role_twice_or_two_roles_on_one_pin_is_unsupported_not_malformed(name, rows):
    """plan §2.4: malformed is only a form error, the same (fn, role, channel) twice or fn 0; "個別機能が定める役割の個数・
    ピンの組を満たさない" is unsupported with the tag as received. The capture models said malformed."""
    model = _models()[name]()
    tags = [(0x90, struct.pack("<HBH", *r)) for r in rows]
    assert _plan(model, 16, b"", tags) == m.UNSUPPORTED


@pytest.mark.parametrize("name,rows", [("logic", [(2, 0, 0), (2, 1, 1)]), ("analog", [(2, 1, 0), (2, 3, 1)])])
def test_a_capture_model_keeps_its_configuration_on_the_same_set_and_refuses_a_change_while_running(name, rows):
    """plan §2.1 "割り当ての集合が変わらないfnは何もしない" / "変更するfnの機能が動作中なら ... unavailable（cause 6）";
    capture §3.2. The logic and analog models dropped the configuration for the same set and changed a running one."""
    model = _models()[name]()
    e = model.extension
    tags = [(0x90, struct.pack("<HBH", *r)) for r in rows]
    assert _plan(model, 16, b"", tags) is None
    e.config, e.state = {"mode": 2}, 3                                        # configured and capturing
    assert _plan(model, 16, b"", tags) is None and e.config == {"mode": 2} and e.state == 3
    swapped = [(0x90, struct.pack("<HBH", rows[0][0], rows[0][1], rows[1][2])),
               (0x90, struct.pack("<HBH", rows[1][0], rows[1][1], rows[0][2]))]
    assert _plan(model, 16, b"", swapped) == m.UNAVAILABLE and e.config == {"mode": 2} and e.state == 3


# ---- link §3: "試し中のcommit要求がverify期限までに届かなければ、unavailable cause 6で断り、応答後のframe境界で戻す" ------

def test_a_commit_arriving_after_verify_ms_is_refused_cause_6_and_the_port_goes_back_after_its_answer():
    """The bench checked verify_ms only on a tick: a commit arriving after the deadline with no tick between was
    accepted (phase 2). Now the frame's arrival sees the deadline: phase 3, the commit refused cause 6 at the raised rate,
    and back at the boot speed once that answer is out."""
    from oep_client import endpoint, host as h, virtual_bench
    from test_port_speed import CAUSE_6, COMMIT, PS, TRY

    class Clock:
        t = 0

        def __call__(self):
            return self.t
    clock = Clock()
    ep = endpoint.Endpoint(virtual_bench.esp32_v003(), clock)
    hst = h.Host(lambda b: ep.handle(b, 0))
    hst.open(60000)
    hst.call(ep.link_fn, PS, struct.pack("<IBH", 500000, TRY, 100))
    ep.after_answer()
    clock.t += 150                                                              # past verify_ms, no tick meanwhile
    r = raw_call(hst, ep.link_fn, PS, struct.pack("<IBH", 500000, COMMIT, 0))
    assert (r.resolution, r.detail, r.payload[:4]) == (m.REJECTED, m.UNAVAILABLE, CAUSE_6)
    assert ep.speed_phase(0) == 3 and ep.port_baud(0) == 500000                 # the refusal goes at the raised rate
    ep.after_answer()
    assert ep.speed_phase(0) == 0 and ep.port_baud(0) == 115200


def raw_call(hst, fn, op, payload):
    from oep_client import host as h
    try:
        return hst.call(fn, op, payload)
    except h.Rejected as e:
        return e.result


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


def test_the_uart_model_refuses_the_format_twice():
    """core §2.3: configure with format 4 and the format again (0xff, bit 7) is malformed, and nothing changes."""
    from oep_client.conformance.uart import UartChecks
    from oep_client.conformance.uart_sample import SampleUartAdapter
    model, c = _uart()
    checker = UartChecks(c, SampleUartAdapter(model))
    checker.cases = ('TWICE',)
    r = checker.run()
    assert next(row for row in r['checks'] if row['id'] == 'OEP-UART-TWICE')['status'] == 'passed'
    e = model.extension
    e.plans[2] = [0, 1]
    from oep_client.conformance.stream_model import Stream
    e.stream = Stream(model.ep, 0, 0)
    with pytest.raises(Reject) as ex:
        e.request_tags(2, 16, m.tlv(1, b'\x04') + m.tlv(1, b'\xff', critical=True))
    assert ex.value.reason == 3 and e.baud == 0


def test_the_uart_checker_requires_a_later_bad_format_copy_refused():
    """The checker's FIRST-WINS case required a configure with a later 0xff format copy to succeed (the old rule: later
    copies ignored); it now requires unsupported 0x81 and catches a model that skips later copies."""
    from oep_client.conformance.uart import UartChecks
    from oep_client.conformance.uart_sample import SampleUartAdapter
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
        from oep_client.conformance.i2c import I2cChecks as Checker
        from oep_client.conformance.i2c_sample import SampleI2cAdapter as Adapter
        from test_conformance_i2c import setup
        prefix = 'OEP-I2C-'
    else:
        from oep_client.conformance.spi import SpiChecks as Checker
        from oep_client.conformance.spi_sample import SampleSpiAdapter as Adapter
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
    from oep_client.conformance.link_routes import LinkRoutesChecks
    from oep_client.conformance.link_routes_sample import SampleLinkRoutesAdapter
    from test_conformance_link_routes import setup
    extended(monkeypatch, success={(4, 16), (4, 17)})
    model, c = setup()
    passed(LinkRoutesChecks(c, SampleLinkRoutesAdapter(model)).run())


def test_port_speed_execution_accepts_source_and_sink_answer_tails(monkeypatch):
    from oep_client.conformance.port_speed_execution import PortSpeedExecutionChecks
    from oep_client.conformance.port_speed_execution_sample import SamplePortSpeedExecutionAdapter
    from test_conformance_port_speed_execution import setup
    extended(monkeypatch, success={(4, 16), (4, 17)})
    model, c = setup()
    passed(PortSpeedExecutionChecks(c, SamplePortSpeedExecutionAdapter(model)).run())


# ---- link §3: 「拒否」はunavailable cause 6 (a TLV list; the probe may add the fn TLV) ---------------------------------
def test_port_speed_accepts_more_tlvs_in_its_refusals_and_a_source_tail(monkeypatch):
    from oep_client.conformance.port_speed import PortSpeedChecks
    from oep_client.conformance.port_speed_sample import SamplePortSpeedAdapter
    from test_conformance_port_speed import setup
    extended(monkeypatch, success={(4, 16)})
    model, c = setup()
    passed(PortSpeedChecks(c, SamplePortSpeedAdapter(model)).run())


def test_port_speed_lifecycle_accepts_more_tlvs_in_its_refusals(monkeypatch):
    from oep_client.conformance.port_speed_lifecycle import PortSpeedLifecycleChecks
    from oep_client.conformance.port_speed_sample import SamplePortSpeedAdapter
    from test_conformance_port_speed import setup
    extended(monkeypatch)
    model, c = setup()
    passed(PortSpeedLifecycleChecks(c, SamplePortSpeedAdapter(model)).run())


def test_port_speed_still_requires_cause_6(monkeypatch):
    from oep_client.conformance.port_speed import PortSpeedChecks
    from oep_client.conformance.port_speed_sample import SamplePortSpeedAdapter
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


# ---- fixture §1-§4: rejected payloads are TLV lists the probe may extend --------------------------------------------
@pytest.mark.parametrize('name', ['gpio', 'uart', 'i2c', 'spi'])
def test_fixture_checkers_accept_more_tlvs_in_refusals(monkeypatch, name):
    import importlib
    test = importlib.import_module(f'test_conformance_{name}')
    checker = {'gpio': ('conformance.gpio', 'GpioChecks', 'conformance.gpio_sample', 'SampleGpioAdapter'),
               'uart': ('conformance.uart', 'UartChecks', 'conformance.uart_sample', 'SampleUartAdapter'),
               'i2c': ('conformance.i2c', 'I2cChecks', 'conformance.i2c_sample', 'SampleI2cAdapter'),
               'spi': ('conformance.spi', 'SpiChecks', 'conformance.spi_sample', 'SampleSpiAdapter')}[name]
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
    from oep_client.conformance.registered_ops import refusal_carries
    assert refusal_carries('unavailable', payload, expected) is ok
    assert refusal_carries('unsupported', b'\xfe' + payload, b'\xfe' + expected) is ok
    assert refusal_carries('unsupported', b'\x00' + payload, b'\xfe' + expected) is False
