"""Fifth review, core §0-§5 and transports: code divergences from oep-spec a09fa45 found and fixed.

- core §2.2 / §2.3: the host reads an answer's TLV as its tag only without bit 7 ("応答、出来事、データでは bit 7 は 0 で、
  そこで bit 7 の立った TLV に会った読む側は、知らない tag として読み飛ばす"), uses the first copy of a tag the definition
  does not repeat ("読む側は最初のものを使う"), and never a value without its defined length ("値が定義の決める長さ ...
  を持たないものは壊れている: host はそれを使わない") - in every describe reader, not only catalog's.
- core §4.3: the checker took a locked answer's remaining time 0 for a violation; the table says only "残り時間 ms（u32）".
- core §4.4: the independent checker's wires waited 3 s for every answer, whatever max_op_ms said ("host は要求ごとに
  少なくとも次を待つ: 要求全体の処理時間の上限 + 1000 ms + 転送の時間").
"""

from __future__ import annotations

import struct
import types

import pytest

from oep_client import catalog, config, conformance, console, core, dump, fixture, host as h, link, pins, riscv
from oep_client import message as m, registry as reg
from oep_client.conformance_serial import SerialWire, frame as serial_frame
from oep_client.conformance_tcp import TcpWire
from oep_client.conformance_usb import UsbWire

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
    cfg_tags = reg.PROBE_CONFIG.tlv["describe"]
    gpio_modes = reg.FIXTURE_GPIO.tlv["describe"]["modes"]
    i2c_depth = reg.FIXTURE_I2C_TARGET.tlv["describe"]["queue_depth"]
    hst = a_host({
        3: [(cfg_tags["storage"] | CRIT, u32(512)), (cfg_tags["items"] | CRIT, b"\x02")],
        4: [(gpio_modes | CRIT, u32(0x07))],
        5: [(i2c_depth | CRIT, b"\x04")],
        6: [(catalog.MAX_CLOCK_HZ | CRIT, u32(4_000_000))],
    })
    d = client(config.ProbeConfig, hst, 3).describe()
    assert d.storage_bytes == 0 and d.items == []
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
    cfg_tags = reg.PROBE_CONFIG.tlv["describe"]
    gpio_modes = reg.FIXTURE_GPIO.tlv["describe"]["modes"]
    stretch = reg.FIXTURE_I2C_TARGET.tlv["describe"]["max_stretch_us"]
    mech = reg.TARGET_CONSOLE.tlv["describe"]["mechanisms"]
    restart = reg.PROBE_RESTART.tlv["describe"]["restart_max_ms"]
    roles = reg.PROBE_PLAN.tlv["describe"]["plan_roles"]
    hst = a_host({
        3: [(cfg_tags["storage"], u32(512)), (cfg_tags["storage"], u32(1024)),
            (cfg_tags["items"], b"\x02"), (cfg_tags["items"], b"\x03\x07")],
        4: [(gpio_modes, b"\x07\x00\x00"), (gpio_modes, u32(0x07))],          # broken first copy (3 bytes)
        5: [(stretch, b"\x10\x00\x00\x00\x00"), (stretch, u32(100))],         # broken first copy (5 bytes)
        6: [(catalog.MAX_CLOCK_HZ, b"\x01\x02\x03"), (catalog.MAX_CLOCK_HZ, u32(4_000_000)),
            (catalog.MAX_LENGTH, b"\x40"), (catalog.MAX_LENGTH, struct.pack("<H", 64))],
        7: [(mech, b"\x02\x01"), (mech, b"\x01\x01")],                           # n 2 with one byte: broken
        8: [(restart, b"\x10\x27"), (restart, u32(5000))],
        9: [(roles, b"\x08"), (roles, u32(8))],
    }, {core.RESTART_NAME: 8, core.PLAN_NAME: 9})
    d = client(config.ProbeConfig, hst, 3).describe()
    assert d.storage_bytes == 512 and d.items == [2]                         # the first, not the last
    assert client(fixture.Gpio, hst, 4).modes() == 0xFF
    assert client(fixture.I2cTarget, hst, 5).max_stretch_us is None
    assert client(riscv.Wire, hst, 6)._speed_or_default(None) == riscv.WireBase.DEFAULT_MAX_SPEED
    with pytest.raises(riscv.NoMaxLength):
        riscv.declared_max_length(hst, 6, "wire")
    assert client(console.Console, hst, 7).mechanisms() == []
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
