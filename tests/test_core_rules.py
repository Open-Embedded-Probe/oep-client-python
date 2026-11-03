"""Core and transports rules (oep-core, oep-transports, oep-if-common, oep-if-plan, oep-if-restart) in the host, the
virtual bench and the checkers, each test pinned to the spec sentence it checks."""
from __future__ import annotations
import struct
import types
import pytest
from oep_client import catalog, conformance, settings, console, core, dump, fixture, host as h, link, pins, riscv
from oep_client import message as m, registry as reg
from oep_client.conformance.serial import SerialWire, frame as serial_frame
from oep_client.conformance.tcp import TcpWire
from oep_client.conformance.usb import UsbWire
from oep_client import message as m
from test_link_host import Stream, answering, frame, make_link, result, sent
from fractions import Fraction
from oep_client import arm, capture as c, conformance, host as h, message as m, registry as reg, riscv
from oep_client.conformance import stream as conformance_stream
from oep_client import conformance, link
from oep_client import capture as c, message as m, riscv
from oep_client.conformance.restart import RestartChecks
from oep_client.conformance.restart_sample import SampleRestartAdapter
from test_conformance_restart import setup as restart_setup
import inspect
from oep_client import (catalog, conformance, core, endpoint, host as h, message as m, registry as reg, virtual_bench as vb)
from oep_client.conformance import (retention as conformance_retention, session_smoke as conformance_session_smoke, tcp_peers as conformance_tcp_peers)


CRIT = m.TAG_CRITICAL


def a_host(describes: dict[int, list[tuple[int, bytes]]], fns: dict[str, int] | None = None) -> h.Host:
    def send(_):
        raise AssertionError("no request goes out: every declaration is cached")
    hst = h.Host(send)
    hst._describes.update(describes)
    hst._fns.update(fns or {})
    hst._revisions.update({fn: 1 for fn in (fns or {}).values()})   # the revision this client speaks (core §7.2)
    return hst


def client(cls, hst, fn):
    obj = cls.__new__(cls)
    obj.host, obj.fn, obj.name, obj.prefix = hst, fn, cls.NAME, b""
    return obj


u32 = lambda v: struct.pack("<I", v)   # noqa: E731


# ---- core §2.2: bit 7 in an answer's TLV is an unknown tag -------------------------------------------------------

def test_describe_readers_take_a_tag_with_bit_7_for_an_unknown_one():
    gpio_modes = reg.FIXTURE_GPIO.tlv["describe"]["modes"]
    i2c_depth = reg.FIXTURE_I2C_TARGET.tlv["describe"]["queue_depth"]
    text = reg.CORE.enum["setting_form"]["text"]
    cat = settings.Catalog([(settings.SETTING | CRIT, bytes([text, 0, 0, 0, 32, 0]) + b"label")], [])
    assert cat.items[0] == []                                                 # a setting with bit 7: an unknown tag
    hst = a_host({
        4: [(gpio_modes | CRIT, u32(0x07))],
        5: [(i2c_depth | CRIT, b"\x04")],
        6: [(catalog.MAX_CLOCK_HZ | CRIT, u32(4_000_000))],
    })
    assert client(fixture.Gpio, hst, 4).modes() == 0xFF
    assert client(fixture.I2cTarget, hst, 5).queue_depth is None
    assert client(riscv.Wire, hst, 6)._speed_or_default(None) == riscv.WireBase.DEFAULT_MAX_SPEED


def test_required_missing_does_not_count_a_bit_7_copy_as_the_tag():
    tags = reg.CORE.tlv["describe"]
    ops = catalog.pack_ops(set(reg.CORE.op.values())) if hasattr(catalog, "pack_ops") else None
    describe = [(tags["unit_id"] | CRIT, b"abc"), (tags["transport"], bytes([0, 6, 0xFF])), (tags["max_op_ms"], u32(1000))]
    if ops is not None:
        describe.append((m.TAG_OPS, ops))
    confirm = struct.pack("<4sBHIBI", b"OEP!", 1, 64, 64, 1, 7) + m.tlv(reg.CORE.tlv["confirm_answer"]["transport"], b"\x00")
    assert "describe of fn 0: unit_id" in dump.required_missing(confirm, describe)


def test_a_connections_selected_target_with_bit_7_is_not_used():
    entry = struct.pack("<IHHIBB", 7, 1, 2, 1_000_000, 0, 0)
    tail = bytes([riscv._SELECTED_TARGET | CRIT]) + struct.pack("<H", 5) + b"\x00" + u32(0x01002927)
    w = riscv.Wire.__new__(riscv.Wire)
    w._call = lambda op, body, locked=True: m.Result(1, m.COMPLETED, m.SUCCESS, bytes([0, 1]) + entry + tail)
    assert w.connections()[0].targetsel is None
    good = bytes([riscv._SELECTED_TARGET]) + struct.pack("<H", 5) + b"\x00" + u32(0x01002927)
    w._call = lambda op, body, locked=True: m.Result(1, m.COMPLETED, m.SUCCESS, bytes([0, 1]) + entry + good)
    assert w.connections()[0].targetsel == 0x01002927


# ---- core §2.3: the first copy, of its defined length --------------------------------------------------------------

def test_describe_readers_use_the_first_copy_and_never_a_later_one_for_a_broken_first():
    gpio_modes = reg.FIXTURE_GPIO.tlv["describe"]["modes"]
    text = reg.CORE.enum["setting_form"]["text"]
    cat = settings.Catalog([(settings.SETTING, bytes([text, 0, 0, 0, 32, 0]) + b"label"),
                            (settings.SETTING, bytes([text, 0, 0]))], [])   # a second one too short: not a setting
    assert [it.name for it in cat.items[0]] == ["label"]
    stretch = reg.FIXTURE_I2C_TARGET.tlv["describe"]["max_stretch_us"]
    mech = reg.TARGET_CONSOLE.tlv["describe"]["mechanism"]
    restart = reg.PROBE_RESTART.tlv["describe"]["restart_max_ms"]
    roles = reg.PROBE_PLAN.tlv["describe"]["plan_roles"]
    hst = a_host({
        4: [(gpio_modes, b"\x07\x00\x00"), (gpio_modes, u32(0x07))],          # broken first copy (3 bytes)
        5: [(stretch, b"\x10\x00\x00\x00\x00"), (stretch, u32(100))],         # broken first copy (5 bytes)
        6: [(catalog.MAX_CLOCK_HZ, b"\x01\x02\x03"), (catalog.MAX_CLOCK_HZ, u32(4_000_000)),
            (catalog.MAX_LENGTH, b"\x40"), (catalog.MAX_LENGTH, struct.pack("<H", 64))],
        7: [(mech, b"sdi"), (mech, b"dmseq")],                                  # a named list: every copy (core §7.7)
        8: [(restart, b"\x10\x27"), (restart, u32(5000))],
        9: [(roles, b"\x08"), (roles, u32(8))],
    }, {core.RESTART_NAME: 8, core.PLAN_NAME: 9})
    assert client(fixture.Gpio, hst, 4).modes() == 0xFF
    assert client(fixture.I2cTarget, hst, 5).max_stretch_us is None
    assert client(riscv.Wire, hst, 6)._speed_or_default(None) == riscv.WireBase.DEFAULT_MAX_SPEED
    with pytest.raises(riscv.NoMaxLength):
        riscv.declared_max_length(hst, 6, "wire")
    assert client(console.Console, hst, 7).mechanisms() == ["sdi", "dmseq"]
    assert core.restart_max_ms(hst) is None
    assert core.plan_roles(hst) is None


def test_pin_finder_reads_the_first_gpio_modes_copy_only(monkeypatch):
    gpio_modes = reg.FIXTURE_GPIO.tlv["describe"]["modes"]
    hst = a_host({4: [(gpio_modes, b"\x07"), (gpio_modes, u32(0x07))]})
    monkeypatch.setattr(pins, "Gpio", lambda hst: types.SimpleNamespace(fn=4))
    monkeypatch.setattr(pins.riscv, "Wire", lambda hst, name: object())
    assert pins.PinFinder(hst, say=lambda *_: None).modes == 0xFF


def test_the_named_unit_id_check_uses_the_first_copy():
    tag = reg.CORE.tlv["describe"]["unit_id"]
    hst = a_host({0: [(tag, b""), (tag, b"abc")]})
    closed = []
    hst.link = types.SimpleNamespace(close=lambda: closed.append(True))
    with pytest.raises(link.UnitIdMismatch):
        link.check_unit_id(hst, "abc")                       # an empty first copy is broken: no later one stands in
    assert closed


def test_the_hardware_preflight_reads_the_first_copy_of_fn_0s_describe():
    import inspect
    from oep_client import hardware
    src = inspect.getsource(hardware)
    assert "dict(core.describe(hst))" not in src             # dict() keeps the last copy of a tag (core §2.3: the first)


# ---- core §4.3: locked's remaining time is any u32 -------------------------------------------------------------------

def test_the_checker_takes_a_locked_answer_with_remaining_time_0():
    registry = {"limits": {"corr_half_range": 0x8000, "corr_max": 0xFFFF, "request_header_bytes": 10,
                           "result_header_bytes": 5},
                "core": {"op": [{"name": k, "code": v} for k, v in reg.CORE.op.items()]},
                "reject_reasons": dict(reg.REJECT_REASONS), "roles": dict(reg.ROLES),
                "resolutions": dict(reg.RESOLUTIONS), "outcomes": dict(reg.OUTCOMES)}
    request = struct.pack("<BHHBI", 1, 5, 0, reg.CORE.op["open"], 9) + struct.pack("<IB", 3000, 0)
    reply = m.Result(5, m.REJECTED, m.LOCKED, struct.pack("<I", 0)).pack()
    checks = conformance.Checks(lambda _: reply, registry, "abc")
    assert checks.exchange(request) == reply


# ---- core §4.4: the checker waits max_op_ms + host_wait_add_ms + the transfer time -----------------------------------

class SlowBackend:
    """A raw USB backend whose one answer comes `after` seconds (a fake clock) after the request."""

    def __init__(self, reply: bytes, after: float):
        self.now, self.reply, self.after, self.sent_at = 0.0, reply, after, None

    def write(self, data, method="out"):
        self.sent_at = self.now
        return len(data)

    def read(self, timeout):
        self.now += max(timeout, 0.01)
        if self.sent_at is not None and self.reply is not None and self.now - self.sent_at >= self.after:
            out, self.reply = struct.pack("<H", len(self.reply)) + self.reply, None
            return out
        return None

    def close(self):
        pass


def test_the_checkers_wire_waits_at_least_max_op_ms_plus_1_s_for_an_answer():
    request = struct.pack("<BHHBI", 1, 5, 0, reg.CORE.op["end"], 9)
    reply = m.Result(5, m.COMPLETED, m.SUCCESS).pack()
    backend = SlowBackend(reply, 4.5)                        # an end that takes 4.5 s on a probe with max_op_ms 5000
    wire = UsbWire(backend, "bulk", out_packet_size=512, clock=lambda: backend.now, sleep=lambda s: None)
    wire.op_wait_s = 5.0                                     # what Checks.identity sets from max_op_ms 5000
    assert wire.send(request) == reply


def test_the_checker_sets_the_wires_processing_bound_from_max_op_ms():
    tags = reg.CORE.tlv["describe"]
    rows = [(tags["unit_id"], b"abc"), (tags["max_op_ms"], u32(5000))]
    checks = conformance.Checks.__new__(conformance.Checks)
    checks.reg = {"core": {"tlv": {"describe": dict(tags)}}}
    checks.unit, checks.observed = "abc", {}
    checks.describe = lambda fn: rows
    backend = SlowBackend(None, 0)
    checks.wire, checks.tcp_wire = None, None
    checks.usb_wire = UsbWire(backend, "bulk", out_packet_size=512, clock=lambda: backend.now, sleep=lambda s: None)
    assert checks.usb_wire.op_wait_s == 1.0                  # core_quick_op_max_ms before max_op_ms is known
    checks.identity()
    assert checks.usb_wire.op_wait_s == 5.0
    for cls in (SerialWire, TcpWire):
        assert "op_wait_s + 1" in __import__("inspect").getsource(cls.exchange)


def test_a_result_for_a_corr_not_waited_for_is_read_past_on_a_length_framed_link():
    """core §10.1: "知らない role のフレームと、待っていない corr の応答は捨てる。" - on vendor bulk / HID (length frames that
    are not TCP) too: a well-formed result for another corr is dropped, no resync, no resend."""
    s = Stream(answering({0x13: b"\x00" + bytes(4)}))
    lk = make_link(s)
    s.rx += frame(result(6, b"old"))                              # 6 answered late
    reply = lk.send(m.Request(7, 0, m.OP_LOCK_STATE, b"").pack())
    assert m.Result.unpack(reply).corr == 7
    assert [op for _, _, op in sent(s)] == [m.OP_LOCK_STATE]      # no confirm, nothing sent again
    assert lk.stale == 1 and lk.resyncs == 0 and lk.retries == 0


def _revision_zero(page: bytes) -> bytes:
    """A list page whose fn 5 entry (revision 2 in the declaration model) says revision 0 instead."""
    out, at = bytearray(page), 3
    for _ in range(page[2]):
        fn, n = struct.unpack_from("<H", out, at)[0], out[at + 5]
        if fn == 5:
            out[at + 4] = 0
        at += 6 + n
    return bytes(out)


def test_a_listed_interface_at_revision_0_is_not_refused_by_the_discovery_checks():
    """core §7.2: "entry: fn(u16)、instance(u16)、revision(u8)、name_len(u8)、name" - the interface revision is any u8;
    nothing in core (§2.8, §7.2) or versioning sets a lower bound, so the checker (conformance.Checks.list_snapshot and
    DiscoveryChecks.lists) must not require revision >= 1."""
    from test_conformance_discovery import setup
    model, c, runner = setup()
    plain = model.extension.list_page
    model.extension.list_page = lambda first, limit: _revision_zero(plain(first, limit))
    report = runner.run()
    assert report['status'] == 'passed', report['checks']
    assert any(e['fn'] == 5 and e['revision'] == 0 for e in c.list_snapshot())


def test_the_core_smoke_list_takes_an_interface_at_revision_0():
    """core §7.2 (as above): CoreSmokeChecks' list entries take revision 0."""
    from oep_client.conformance.core_smoke import CoreSmokeChecks
    from test_conformance_core_smoke import REG, SPEC, SmallCore
    probe = SmallCore(1, named=True)
    plain = probe.send

    def send(request):
        reply = plain(request)
        if request[5] == 2 and reply[3] == 1:
            reply = reply[:12] + b"\x00" + reply[13:]               # the one entry's revision byte
        return reply
    report = CoreSmokeChecks(send, REG, 'minimal', spec=SPEC, observe_effects=probe.effects).run()
    assert report['status'] == 'passed', report['checks']


def test_the_equipment_config_takes_any_spec_unit_id_for_usb():
    """core §7.5: "unit_id | 個体の ID。必須。text で 1〜32 byte、使える文字は `a-z 0-9 -` だけ（チップの固有の番号を小文字の
    16 進にしたもの、など）" - hex of 12 or more is an example, not the rule; an x- one still names no unit."""
    import pytest
    from oep_client import hardware
    from test_hardware import physical_equipment
    config = physical_equipment()
    node = config.data['probes'][0]
    for unit in ('fafe', 'bench-probe-a'):
        node['identity'] = {'method': 'oep-unit-id', 'value': unit}
        node['transport'] = {'kind': 'usb', 'unit_id': unit}
        hardware.validate(config.data)
    node['identity'] = {'method': 'oep-unit-id', 'value': 'x-1'}
    node['transport'] = {'kind': 'usb', 'unit_id': 'x-1'}
    with pytest.raises(hardware.ConfigurationError, match='complete'):
        hardware.validate(config.data)


@pytest.mark.parametrize('refuse_replace', [True, False])
def test_the_subscription_checks_take_a_probe_that_bounds_what_it_holds(refuse_replace):
    """core §10.3: "probe は購読用の保持量に上限を置いてよい。要求したデータの送信条件を満たすための保持量が足りない場合は、何も変えずに
    rejected unavailable（cause 2）で断る。" - a probe that cannot hold min_bytes 65535 refuses that subscribe (the
    old subscription, its destination and seq kept); the checker must not require it to succeed."""
    from oep_client import virtual_core
    from test_conformance_subscriptions import build
    model, c, adapter, runner = build()
    extension = model.extension
    dispatch = extension.dispatch

    def bounded(fn, op, payload):
        if op == 1 and struct.unpack('<HI', payload)[0] > 4096:
            raise virtual_core.Reject(m.UNAVAILABLE, m.tlv(1, b'\x02'))   # cause 2: nothing changes
        return dispatch(fn, op, payload)
    extension.dispatch = bounded
    if not refuse_replace:                                          # a wrong refusal still fails: the cause must be 2
        extension.dispatch = lambda fn, op, payload: (_ for _ in ()).throw(virtual_core.Reject(
            m.UNAVAILABLE, m.tlv(1, b'\x05'))) if op == 1 and payload[:2] == b'\xff\xff' else dispatch(fn, op, payload)
    report = runner.run()
    rows = {x['id']: x['status'] for x in report['checks']}
    expected = 'passed' if refuse_replace else 'failed'
    assert rows['IF-SUB-REPLACE'] == rows['IF-SUB-EVENT-THRESHOLD'] == expected, report['checks']


def test_the_owner_shown_is_the_first_copy_even_when_a_later_copy_is_broken():
    """core §6.4 owner, read as core §2.3 says: "繰り返すと定義が言わない tag が 2 つ以上あれば、読む側は最初のものを使う。" and a
    response TLV whose value has not the defined length "は壊れている: host はそれを使わない" - the broken later copy is not
    used; it does not hide the first one (lock_state and rejected locked alike)."""
    from oep_client import host as h
    tail = m.tlv(h.OWNER, b"flash-tool") + m.tlv(h.OWNER, b"x" * 33)
    assert h.owner_of(tail) == "flash-tool"
    locked = h.rejection(m.Result(1, m.REJECTED, m.LOCKED, struct.pack("<I", 7) + tail))
    assert locked.owner == "flash-tool"
    assert h.owner_of(m.tlv(h.OWNER, b"x" * 33) + m.tlv(h.OWNER, b"ok")) is None   # a broken first copy: none used


def test_an_interface_client_uses_the_revision_it_speaks_when_the_probe_offers_two():
    """core §7.2: "複数の revision を提供するときは各 revision の組を明示的に選び、保存した設定を別の revision へ自動で移さない。" (and
    §2.8: "host は知らない revision のインターフェースを使わない。") - with oep.fixture.gpio listed at revision 2 first and at
    revision 1 after it, a revision-1 client takes the revision-1 fn instead of refusing the probe; with no revision 1 at
    all it still refuses (UnsupportedRevision)."""
    from oep_client import catalog, core, host as h
    name = b"oep.fixture.gpio"

    def probe(revisions):
        def send(raw):
            req = m.Request.unpack(raw)
            if req.op == m.OP_LIST:
                rows = b"".join(struct.pack("<HHBB", fn, 0, rev, len(name)) + name for fn, rev in revisions)
                return m.Result(req.corr, m.COMPLETED, m.SUCCESS, struct.pack("<HB", len(revisions), len(revisions))
                                + rows).pack()
            if req.op == m.OP_DESCRIBE:
                return m.Result(req.corr, m.COMPLETED, m.SUCCESS, b"\x00" + catalog.ops_tlv({0x10})).pack()
            raise AssertionError(req)
        return h.Host(send)

    class Gpio(core.Interface):
        NAME, REVISION = name.decode(), 1
    assert Gpio(probe([(3, 2), (5, 1)])).fn == 5
    with pytest.raises(core.UnsupportedRevision):
        Gpio(probe([(3, 2)]))


def test_an_optional_probe_interface_in_a_revision_the_client_does_not_speak_is_not_used():
    """core §2.8: "host は知らない revision のインターフェースを使わない。" with §7.2's explicit (name, revision) choice - an
    oep.probe.restart listed at revision 2 only is not used (the same as not offered: restart_max_ms None); listed at
    revision 2 and 1, the revision-1 fn is the one used."""
    from oep_client import catalog, core, host as h
    name = b"oep.probe.restart"

    def probe(revisions):
        def send(raw):
            req = m.Request.unpack(raw)
            if req.op == m.OP_LIST:
                rows = b"".join(struct.pack("<HHBB", fn, 0, rev, len(name)) + name for fn, rev in revisions)
                return m.Result(req.corr, m.COMPLETED, m.SUCCESS, struct.pack("<HB", len(revisions), len(revisions))
                                + rows).pack()
            if req.op == m.OP_DESCRIBE:
                fn = struct.unpack_from("<H", req.payload)[0]
                return m.Result(req.corr, m.COMPLETED, m.SUCCESS,
                                b"\x00" + catalog.tlv(core.TAG_RESTART_MAX_MS, struct.pack("<I", 100 * fn))).pack()
            raise AssertionError(req)
        return h.Host(send)
    assert core.restart_max_ms(probe([(3, 2)])) is None
    assert core.restart_max_ms(probe([(3, 2), (5, 1)])) == 500


def completed(payload: bytes, outcome: int = m.SUCCESS) -> m.Result:
    return m.Result(1, m.COMPLETED, outcome, payload)


def answering_with(cls, result: m.Result, **attrs):
    """A client object of `cls` whose every call gets `result` (nothing goes out)."""
    obj = cls.__new__(cls)
    obj.host, obj.fn, obj.name, obj.prefix = None, 5, cls.NAME, b""
    obj._call = lambda *a, **k: result
    obj._request = lambda *a, **k: result
    for k, v in attrs.items():
        setattr(obj, k, v)
    return obj


# ---- core §2.3: a TLV of another length than its definition's is not used ---------------------------------------

def test_confirms_refusal_takes_supported_only_of_two_bytes():
    sup = reg.CORE.tlv["unsupported_payload"]["supported"]
    good = h.Unsupported(m.Result(1, m.REJECTED, m.UNSUPPORTED, b"\0" + m.tlv(sup, b"\x01\x02")))
    long = h.Unsupported(m.Result(1, m.REJECTED, m.UNSUPPORTED, b"\0" + m.tlv(sup, b"\x01\x02\x03")))
    assert good.supported == (1, 2)
    assert long.supported is None


def rvswd_attach(tail: bytes, flags: int) -> riscv.Wire:
    wire = answering_with(riscv.Wire, completed(struct.pack("<IIBI", 7, 0, flags, 1_000_000) + tail),
                     attach_body=lambda *a, **k: b"", attach_ms=lambda reset: 0)
    wire.attach()
    return wire


def test_an_attach_dpc_of_another_length_than_u32_is_not_used():
    halted = riscv.Wire.FLAGS["halted"]
    assert rvswd_attach(m.tlv(riscv.Wire.TAG_DPC, u32(0x80)), halted).dpc == 0x80
    for bad in (b"\x80\x00\x00", u32(0x80) + b"\x00"):
        assert rvswd_attach(m.tlv(riscv.Wire.TAG_DPC, bad), halted).dpc is None


def test_a_target_id_of_a_known_scheme_needs_its_u32():
    tag = riscv.Wire.TAG_TARGET_ID
    dmi = reg.COMMON.enum["target_id_scheme"]["dmi_7f"]
    assert rvswd_attach(m.tlv(tag, bytes([dmi]) + u32(0x1234)), 0).target_id == (dmi, u32(0x1234))
    for bad in (bytes([dmi]), bytes([dmi]) + b"\x34\x12", bytes([dmi]) + u32(0x1234) + b"\x00"):
        assert rvswd_attach(m.tlv(tag, bad), 0).target_id is None
    assert rvswd_attach(m.tlv(tag, b"\x7e\x01\x02"), 0).target_id == (0x7E, b"\x01\x02")   # unknown scheme: raw
    swd = answering_with(arm.SwdWire, completed(struct.pack("<IIBI", 7, 0x2BA01477, 0, 1_000_000)
                                           + m.tlv(arm.SwdWire.TAG_TARGET_ID, b"\x02\x77\x14")),
                    _attach_fixed=lambda *a: b"", _reset_tlv=lambda r: b"", attach_ms=lambda reset: 0)
    swd.attach()
    assert swd.target_id is None


def test_step_left_is_taken_only_with_its_length_0():
    state = riscv.STATUS["state"]
    fixed = struct.pack("<BBII", state, 0, 0, 0)
    for tail, left in ((m.tlv(riscv.RiscvDm.TAG_STEP_LEFT, b""), True), (m.tlv(riscv.RiscvDm.TAG_STEP_LEFT, b"\x01"), False),
                       (b"", False)):
        dm = answering_with(riscv.RiscvDm, completed(fixed + tail, m.FAILED), restore_data=lambda: None)
        with pytest.raises(riscv.StepError) as e:
            dm.step()
        assert e.value.step_left is left


def test_capture_status_error_is_taken_only_as_one_byte():
    fixed = struct.pack("<BIQBI", 6, 0, 0, 0, 3)
    good = answering_with(c.LogicCapture, completed(fixed + m.tlv(c.STATUS_ERROR, b"\x02"))).status()
    bad = answering_with(c.LogicCapture, completed(fixed + m.tlv(c.STATUS_ERROR, b"\x02\x00"))).status()
    assert good.error == 2 and bad.error is None


def answer(*items):
    return b"".join(m.tlv(t, v) for t, v in items)


def test_configure_answer_uses_the_first_copy_and_only_values_of_their_length():
    rate = struct.pack("<II", 1000, 1)
    cfg = c._config(answer((c.ACTUAL_RATE, rate), (c.ACTUAL_RATE, struct.pack("<II", 5, 1)),
                           (c.ACTUAL_SAMPLES, u32(10)), (c.ACTUAL_SAMPLES, u32(99)),
                           (c.LAYOUT, bytes([1, 2, 0, 1])), (c.LAYOUT, bytes([8, 1, 3]))), analog=False)
    assert (cfg.rate, cfg.samples, cfg.width, cfg.positions) == (Fraction(1000), 10, 1, [0, 1])
    # a broken first copy is not used, and no later copy stands in for it
    cfg = c._config(answer((c.ACTUAL_RATE, rate + b"\0"), (c.ACTUAL_RATE, rate),
                           (c.ACTUAL_SEGMENTS, b"\x02\x00"), (c.LAYOUT, bytes([1, 3, 0, 1]))), analog=False)
    assert (cfg.rate, cfg.segments, cfg.width, cfg.positions) == (Fraction(0), 0, 0, [])
    cfg = c._config(answer((c.LAYOUT, bytes([16, 0, 12, 1, 0, 9])), (c.FRONTEND_USED, b"\x00\x01\x02"),
                           (c.SCALE, struct.pack("<Bii", 0, 1, 2) + b"\0"), (c.SKEW, struct.pack("<BI", 0, 5)[:4]),
                           (c.REFERENCE, struct.pack("<BIB", 0, 3300, 1) + b"\0")), analog=True)
    assert cfg.order == [] and cfg.frontend == {} and cfg.scale_nv == {} and cfg.reference is None
    assert cfg.skew_ns == {}


def test_calibration_takes_only_whole_values_and_the_first_vrefint():
    factory = bytes([0, 3]) + b"a.b" + struct.pack("<H", 2) + b"\x01\x02"
    vref = struct.pack("<IQI", 1500, 77, 1200)
    cal = answering_with(c.AnalogCapture, completed(answer((c.FACTORY, factory), (c.FACTORY, factory + b"\0"),
                                                      (c.FACTORY, factory[:-1]),
                                                      (c.VREFINT, vref), (c.VREFINT, struct.pack("<IQI", 1, 2, 3)))))
    got = cal.calibration()
    assert got.factory == [(0, "a.b", b"\x01\x02")] and got.vrefint == (1500, 77) and got.vrefint_nominal_mv == 1200
    cal = answering_with(c.AnalogCapture, completed(answer((c.VREFINT, vref + b"\0"), (c.VREFINT, vref))))
    assert cal.calibration().vrefint is None


# ---- common §1.3 / core §2.6: the newest kept mark minus the oldest kept, under a quarter --------------------------

class _Adapter:
    adoption = {"mark_capacity": 5}

    def __init__(self, serials, next_serial):
        self.state = {"marks": [(s, 0, 3, 0, 0) for s in serials], "next_serial": next_serial}

    def stream_state(self):
        return self.state


def test_the_stream_checker_holds_the_span_of_the_marks_kept_not_from_next_serial():
    quarter = 1 << 30
    ok = conformance_stream.StreamChecks(None, _Adapter([0, quarter - 1], quarter))
    ok.retained_span()                                       # newest - oldest = 2^30 - 1 < 2^30: allowed
    over = conformance_stream.StreamChecks(None, _Adapter([0, quarter], quarter + 1))
    with pytest.raises(conformance.Violation):
        over.retained_span()


# ---- core §2.3: the checkers read the first copy of a tag that does not repeat ------------------------------------

def test_the_retention_checker_reads_the_first_owner_of_a_lock_state_answer():
    """A probe whose lock_state answer carries a later owner copy (a reader uses the first, core §2.3) passes."""
    import test_conformance_retention as tr
    now, model, adapter, runner = tr.build()
    core = model.ep.current_core
    handle = core.handle

    def doubled(data, transport, **kw):
        out = handle(data, transport, **kw)
        if out and data[0] == 1 and data[3:6] == b"\0\0\x13" and out[3:5] == b"\x01\0" and len(out) > 10:
            out += m.tlv(1, b"later")                            # a second owner after the first: not the one used
        return out
    core.handle = doubled
    report = runner.run()
    assert report["status"] == "passed", [(x["id"], x.get("error")) for x in report["checks"] if x["status"] != "passed"]


def test_two_devices_with_the_same_x_serial_are_two_probes():
    usb = [{"serial": "x-oep", "location": "1-2", "ways": {"vendor"}},
           {"serial": "x-oep", "location": "1-3", "ways": {"vendor"}}]
    ports = [("/dev/ttyACM0", "x-oep", "1-2"), ("/dev/ttyACM1", "x-oep", "1-3")]
    got = link._merge_probes(usb, [{"serial": "x-oep", "location": None, "ways": {"hid"}}], ports)
    assert [(p.location, p.ways, p.ports) for p in got] == [
        ("1-2", ["vendor", "cdc"], ["/dev/ttyACM0"]),               # by location only
        ("1-3", ["vendor", "cdc"], ["/dev/ttyACM1"])]                # the HID entry (no location) is libusb's to show
    alone = link._merge_probes([], [{"serial": "x-oep", "location": None, "ways": {"hid"}},
                                    {"serial": "x-oep", "location": None, "ways": {"hid"}}], [])
    assert len(alone) == 2                                          # nothing ties them together


# ---- core §7.2 / §2.3: TLVs after the list entries ------------------------------------------------------------------

def _bench(monkeypatch, **kw):
    from oep_client import endpoint, virtual_bench
    now = [1000]
    monkeypatch.setattr('oep_client.conformance.time.sleep', lambda s: now.__setitem__(0, now[0] + int(s * 1000)))
    probe = virtual_bench.PROFILES['p4-bench']()
    return endpoint.Endpoint(probe, lambda: now[0], **kw), virtual_bench.unit_id_of(probe)


def _failed(report):
    return [(r['id'], r.get('error')) for r in report['checks'] if r['status'] not in ('passed', 'not_applicable')]


def test_the_core_checker_takes_tlvs_after_the_list_entries(monkeypatch):
    """"各 op の応答の固定部分（と、前に数か長さを置いた並び）の後ろは TLV の並び。後から足すものはすべてここに TLV で足す"
    (core §2.3): a list answer (§7.2) with an extension TLV after its entries, on every page and past the end."""
    from test_conformance import REG
    import copy
    ep, unit = _bench(monkeypatch, tail=b'\x7e\x01\x00x')
    report = conformance.Checks(ep.handle, copy.deepcopy(REG), unit).run()
    assert not [f for f in _failed(report) if f[0].startswith('CORE-')], _failed(report)


def test_the_discovery_and_smoke_checkers_take_tlvs_after_the_list_entries():
    import inspect
    from oep_client.conformance import core_smoke as conformance_core_smoke, discovery as conformance_discovery
    for module in (conformance_core_smoke, conformance_discovery):
        src = inspect.getsource(module)
        assert "at == len(p)" not in src and "tlvs(p[at:])" in src


# ---- core §6.4: owner is optional -------------------------------------------------------------------------------------

def _without_owner(handle):
    """A probe that does not implement owner (core §6.4): owner is an unknown TLV to it - a critical one is refused
    unsupported with the tag as received, a non-critical one ignored (§2.3)."""
    def send(req):
        role, corr, fn, op, sid = struct.unpack_from('<BHHBI', req)
        if fn == 0 and op == 16 and len(req) > 15:
            kept, at = b'', 15
            while at + 3 <= len(req):
                tag, n = struct.unpack_from('<BH', req, at)
                if tag == 0x81:
                    return struct.pack('<BHBB', 2, corr, 0, 11) + bytes((tag,))
                if tag != 0x01:
                    kept += req[at:at + 3 + n]
                at += 3 + n
            req = req[:15] + kept
        return handle(req)
    return send


def test_the_core_checker_takes_a_probe_without_the_owner_display(monkeypatch):
    """"probe は owner の保持と表示を実装しなくてよい。実装しない probe は、owner を知らない TLV として §2.3 に従って扱い、
    非 critical の owner は無視する" (core §6.4): CORE-OWNER is not applicable there, not failed."""
    from test_conformance import REG
    import copy
    ep, unit = _bench(monkeypatch)
    report = conformance.Checks(_without_owner(ep.handle), copy.deepcopy(REG), unit).run()
    rows = {r['id']: r for r in report['checks']}
    assert rows['CORE-OWNER']['status'] == 'not_applicable', rows['CORE-OWNER']
    assert report['status'] == 'passed', _failed(report)


# ---- core §10.3: a probe that bounds what it holds for subscriptions ------------------------------------------------

def test_the_subscription_checker_asks_the_unsupported_case_under_a_condition_any_probe_holds():
    """"probe は購読用の保持量に上限を置いてよい。要求したデータの送信条件を満たすための保持量が足りない場合は、何も変えずに
    rejected unavailable（cause 2）で断る" (core §10.3), and a request with several faults is refused with any one of them
    (core §4.3 order 4): IF-SUB-INVALID's unknown critical TLV goes with a condition that such a probe holds."""
    from test_conformance_subscriptions import build
    model, c, adapter, runner = build()

    def bounded(req):
        role, corr, fn, op, sid = struct.unpack_from('<BHHBI', req)
        if fn and op == 1 and len(req) >= 16 and struct.unpack_from('<H', req, 10)[0] > 4096:
            return struct.pack('<BHBB', 2, corr, 0, 4) + b'\x01\x01\x00\x02'   # unavailable, cause 2: nothing changes
        return model.handle(req)
    c.send = bounded
    report = runner.run()
    rows = {x['id']: x for x in report['checks']}
    assert rows['IF-SUB-INVALID']['status'] == 'passed', rows['IF-SUB-INVALID'].get('error')
    assert report['status'] == 'passed', [(x['id'], x.get('error')) for x in report['checks'] if x['status'] != 'passed']


# ---- core §5.1: a request given up on TCP -----------------------------------------------------------------------------

def test_a_tcp_request_given_up_without_a_resend_reopens_the_connection_before_the_next():
    """"再送を諦めたら、その経路の未解決の要求は結果不明となる。host は TCP では接続を閉じて開き直し ... boot_id を確認し"
    (core §5.1): a TCP link with its resend off (open_host's default there) gives up at the first missing answer; the next
    request goes on a new connection, after a confirm there."""
    from oep_client import message as m
    from test_trace_transports import TcpStream, confirming, req
    old = TcpStream(lambda msg: [])                                  # never answers
    new = TcpStream(confirming())
    lk = link.SerialLink.on_stream(old, "length", 0.2)
    lk.resend = False
    lk.reopener = lambda: new
    boots = []
    lk.on_boot_id = boots.append
    with pytest.raises(TimeoutError):
        lk.send(req(7))
    assert m.Result.unpack(lk.send(req(8))).corr == 8
    assert old.closed and boots == [0x11]
    assert [m.Request.unpack(w[2:]).op for w in old.writes] == [m.OP_CLOCK]
    assert [(r.op, r.corr) for r in (m.Request.unpack(w[2:]) for w in new.writes)][-1] == (m.OP_CLOCK, 8)
    assert m.Request.unpack(new.writes[0][2:]).op == m.OP_CONFIRM


def test_an_unknown_reference_how_is_shown_as_it_is():
    """core §2.4: 応答の enum の知らない値で失敗を知らせないもの（cause、scan の entry の kind）は、知らない値として見せる。
    capture §3.3's reference how (0 nominal, 1 measured): an unknown 2 was shown as nominal (False)."""
    def cfg(how):
        return c._config(m.tlv(c.REFERENCE, struct.pack("<BIB", 1, 1100, how)), analog=True).reference
    assert cfg(0) == ("internal", 1100, False) and cfg(1) == ("internal", 1100, True)
    assert cfg(2) == ("internal", 1100, "2") and cfg(0xFF)[2] == "255"


def test_an_unknown_run_stopped_is_shown_as_it_is():
    """core §2.4 (as above): debug §4.4's run stopped (0 timeout_halted, 1 stopped, 2 not_halted, 3 not_run): an unknown
    4 read as timeout_halted - its dpc taken as valid."""
    payload = struct.pack("<BBIIB", riscv.STATUS["timeout"], 4, 0x20000010, 1000, 0)
    res = riscv.RiscvDm.run_result(m.Result(1, m.COMPLETED, m.FAILED, payload))
    assert res.unknown_stopped == 4 and not res.stopped and not res.not_halted and not res.not_run
    assert not res.dpc_valid and "0x04" in res.where()
    known = riscv.RiscvDm.run_result(m.Result(1, m.COMPLETED, m.FAILED, payload[:1] + b"\0" + payload[2:]))
    assert known.unknown_stopped is None and known.dpc_valid


def test_the_restart_checker_takes_any_uptime_since_the_answer():
    """oep-if-restart §2: 再起動の後は、OEP については電源を入れたときの起動と同じである; core §2.7: probe の時計は 1 つ:
    起動からの ns. Nothing says the new boot's clock reads 0 when it first answers: a probe whose clock started 1 ms
    into its 50 ms restart (counting its own start-up) was failed for 'uptime differs'."""
    model, checks = restart_setup()
    boot = model.complete_boot

    def early_clock():
        boot()
        model.ep._origin_ns -= 1_000_000                 # the clock began 1 ms before the boot completed
    model.complete_boot = early_clock
    r = RestartChecks(checks, SampleRestartAdapter(model)).run()
    assert r["status"] == "passed", r["checks"]


PLAN = reg.PROBE_PLAN
APPLY = PLAN.op["plan_apply"]
ASSIGN = PLAN.tlv["plan_apply"]["role_assignment"]
UART = 5                                                  # esp32-v003's uart
PLAN_FN = next(o.fn for o in vb.esp32_v003().offered if o.name == vb.PLAN)


class Clock:
    t = 0

    def __call__(self):
        return self.t


# ---- plan §2.4 / core §7.4: a channel offered for another role only is unsupported ------------------------------------

def _uart_split_roles() -> vb.VirtualProbe:
    """esp32-v003 whose uart offers RX (role 1) on channel 4 only and TX (role 2) on channel 5 only."""
    p = vb.esp32_v003()
    offered = [vb._with(o, [t for t in o.tlvs if t[0] != catalog.ROLE_CHANNELS]
                        + [catalog.role_channels(1, [4]), catalog.role_channels(2, [5])]) if o.fn == UART else o
               for o in p.offered]
    return vb._again(p, offered)


def test_plan_apply_refuses_a_role_on_a_channel_role_channels_offers_another_role_only():
    ep = endpoint.Endpoint(_uart_split_roles(), Clock())
    hst = h.Host(lambda b: ep.handle(b, 0))
    hst.open(3000)
    for critical in (False, True):
        body = m.tlv(ASSIGN, struct.pack("<HBH", UART, 1, 5), critical=critical)   # RX on TX's channel
        r = m.Result.unpack(ep.handle(m.Request(hst.next_corr(hst.session), PLAN_FN, APPLY, body, hst.session).pack(), 0))
        assert (r.resolution, r.detail) == (m.REJECTED, m.UNSUPPORTED)
        assert r.payload[0] == (ASSIGN | m.TAG_CRITICAL if critical else ASSIGN)          # the tag as received
        assert not ep.plan
    core.plan_apply(hst, [(UART, 1, 4), (UART, 2, 5)])                                 # each role on its own: taken
    assert ep.plan == {(UART, 1, 4), (UART, 2, 5)}


# ---- core §6.4 / §4.3: a held lock's remaining time may read 0 -----------------------------------------------------------

class _Answers:
    """A checker stand-in answering lock_state with (1, 0): held, at the very end of its lease."""
    core = {"lock_state": reg.CORE.op["lock_state"]}

    def request(self, op, payload=b"", **_):
        return op

    def success(self, _request):
        return struct.pack("<BI", 1, 0)


def test_the_tcp_peer_checker_takes_lock_state_held_with_remaining_0():
    held, remaining, owner = conformance_tcp_peers.lock_state(_Answers())
    assert (held, remaining, owner) == (1, 0, b"")


class _SmokeCore:
    reg = {"reject_reasons": dict(reg.REJECT_REASONS)}
    observed: dict = {}

    def call(self, name, payload=b"", **_):
        return m.Result(1, m.COMPLETED, m.SUCCESS, struct.pack("<BI", 1, 0)).pack()

    def success(self, reply):
        return reply[5:]


def test_the_session_smoke_checker_takes_remaining_0_while_held():
    checks = conformance_session_smoke.SessionSmokeChecks(_SmokeCore(), wait_ms=lambda ms: None)
    checks.state(True)                                                       # lock_state (1, 0)
    checks.locked(m.Result(1, m.REJECTED, m.LOCKED, struct.pack("<I", 0)).pack())   # locked, 0 ms left


REG = {
    "roles": {"request": 1, "result": 2},
    "resolutions": {"rejected": 0, "completed": 1},
    "outcomes": {"success": 0, "failed": 1, "partial": 2},
    "reject_reasons": dict(reg.REJECT_REASONS),
    "describe_common": {"ops": 7},
    "limits": {"request_header_bytes": 10, "result_header_bytes": 5, "corr_max": 0xFFFF, "corr_half_range": 0x8000},
    "core": {"op": [{"name": k, "code": v} for k, v in reg.CORE.op.items()],
             "tlv": {"confirm_answer": {"transport": 1},
                     "describe": {"firmware": 64, "model": 65, "unit_id": 66, "channels": 67, "transport": 73,
                                  "chip": 76, "max_op_ms": 77}}},
    "interface": [],
}


def test_the_checkers_keepalive_case_takes_lock_state_held_with_remaining_0():
    ep = endpoint.Endpoint(vb.esp32_v003(), Clock())
    ep._remaining = lambda: 0                                 # every held lock read at the very end of its lease
    checks = conformance.Checks(lambda b: ep.handle(b, 0), REG, vb.unit_id_of(vb.esp32_v003()))
    checks.confirm()
    checks.keepalive()


def test_the_retention_owner_checks_take_remaining_0():
    src = inspect.getsource(conformance_retention.RetentionChecks.owner_lifecycle)
    assert "0 < remaining" not in src and "0 < struct.unpack_from('<I', refused)" not in src


# ---- core §4.4: a request whose bound is max_op_ms waits for max_op_ms, read first when not known yet ------------------

def test_a_lock_free_interface_request_waits_max_op_ms_even_before_the_host_read_it():
    """core §4.4: "host は要求ごとに少なくとも次を待つ: 要求全体の処理時間の上限 + 1000 ms + 転送の時間。処理時間の上限は
    max_op_ms" - core_quick_op_max_ms stands in only for fn 0's quick ops while max_op_ms is unknown. The host used it
    for any request (a gpio read before any session: 2 s instead of max_op_ms + 1 s)."""
    ep = endpoint.Endpoint(vb.esp32_v003(), Clock())
    seen = []

    def send(raw):
        req = m.Request.unpack(raw)
        seen.append((req.fn, req.op, hst.expect_ms))
        return ep.handle(raw, 0)

    hst = h.Host(send)
    hst.confirm()                                             # as a link does when it binds the host
    gpio = core.find(hst, "oep.fixture.gpio")                 # list: a quick fn 0 op
    read = reg.FIXTURE_GPIO.op["read"]
    try:
        hst.request(gpio, read, b"\x01\x04\x00", locked=False)   # unplanned: refused, but waited for as long
    except h.Unavailable:
        pass
    assert seen[-1] == (gpio, read, vb.MAX_OP_MS) and vb.MAX_OP_MS > h.QUICK_OP_MAX_MS
    assert [s[2] for s in seen if s[0] == m.CORE_FN] == [h.QUICK_OP_MAX_MS] * (len(seen) - 1)   # the quick ops' bound
