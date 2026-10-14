"""Rule-by-rule trace of oep-spec a09fa45 interfaces/oep-if-fixture.ja.md (docs/trace/fixture.json): the names and plan
roles (fixture §0), oep.fixture.gpio (§1), oep.fixture.uart (§2), oep.fixture.i2c-target (§3) and
oep.fixture.spi-target (§4), checked on the virtual bench and the client where no other test pins a rule down."""

import random
import struct

import pytest

from oep_client import catalog, core, endpoint, fixture, host, message as m, registry as reg, virtual_bench
from oep_client.conformance_uart import UartChecks
from oep_client.conformance_uart_sample import SampleUartAdapter
from test_conformance_uart import setup as uart_setup
from test_virtual_bench_rules_2026_10_02 import IDLE, PLAN_APPLY, PLAN_RELEASE, SET, Clock, Host, cset, idle_item, una

GPIO, UART, I2C, SPI, CFG = 4, 5, 8, 9, 10                       # p4_x035
V003_I2C, V003_SPI = 7, 8                                         # esp32_v003
_GPIO, _UART, _I2C, _SPI = reg.FIXTURE_GPIO, reg.FIXTURE_UART, reg.FIXTURE_I2C_TARGET, reg.FIXTURE_SPI_TARGET
UNA = reg.CORE.tlv["unavailable_payload"]
INDEX = _GPIO.tlv["unavailable_payload"]["index"]
PROFILES = (virtual_bench.p4_x035, virtual_bench.esp32_v003, virtual_bench.esp32_v003_64, virtual_bench.p4_bench,
            virtual_bench.rp2350_pins)


def bench(profile=virtual_bench.p4_x035):
    ep = endpoint.Endpoint(profile() if callable(profile) else profile, Clock())
    h = Host(ep)
    assert h.open().succeeded
    return ep, h


def assign(*rows) -> bytes:
    return b"".join(m.tlv(0x10, struct.pack("<HBH", *row), critical=True) for row in rows)


def cause(r) -> int:
    assert r.resolution == m.REJECTED and r.detail == m.UNAVAILABLE, r.describe()
    return una(r)[UNA["cause"]][0]


def decl(ep, h, fn) -> dict[int, bytes]:
    r = h.raw(0, m.OP_DESCRIBE, struct.pack("<HH", fn, 0), session=False)
    assert r.succeeded and r.payload[0] == 0                      # one page
    return dict(m.split_tlvs(r.payload[1:]))


def offered(probe, name):
    return [o for o in probe.offered if o.name == name]


def replaced(probe, fn, tlvs):
    """The profile with fn's declarations replaced (VIRTUAL BENCH ONLY: a probe of another shape)."""
    return virtual_bench._again(probe, [virtual_bench._with(o, tlvs) if o.fn == fn else o for o in probe.offered])


# ---- §0 the interfaces --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("name, roles, fn", [
    ("oep.fixture.gpio", {"line": 1}, GPIO),
    ("oep.fixture.uart", {"rx": 1, "tx": 2}, UART),
    ("oep.fixture.i2c-target", {"sda": 1, "scl": 2}, I2C),
    ("oep.fixture.spi-target", {"sck": 1, "mosi": 2, "miso": 3, "cs": 4}, SPI)])
def test_names_revisions_and_plan_roles(name, roles, fn):
    """fixture §0: each fixture is revision 1 and its plan roles are numbered as the table says; a role past them is
    unsupported (the role_assignment's tag)."""
    spec = reg.INTERFACES[name]
    assert spec.revision == 1 and spec.enum["role"] == roles
    assert all(o.revision == 1 for o in offered(virtual_bench.p4_x035(), name))
    ep, h = bench()
    assert ep.names[fn] == name
    chans = [30, 31, 32, 33][:len(roles)]
    assert h.raw(h.plan_fn, PLAN_APPLY, assign(*[(fn, r, ch) for r, ch in zip(roles.values(), chans)])).succeeded
    r = h.raw(h.plan_fn, PLAN_APPLY, assign(*[(fn, r, ch) for r, ch in zip(roles.values(), chans)],
                                            (fn, len(roles) + 1, 40)))
    assert r.detail == m.UNSUPPORTED and r.payload[0] == 0x90


# ---- §1 oep.fixture.gpio ------------------------------------------------------------------------------------------

def test_gpio_set_row_needs_the_lock_and_answers_nothing():
    """fixture §1 (set): op 0x10, n(u8) n x (channel(u16) mode(u8)) [TLV]; no answer payload; needs the lock."""
    ep, h = bench()
    assert _GPIO.op["set"] == 0x10
    h.ok(h.plan_fn, PLAN_APPLY, assign((GPIO, 1, 30), (GPIO, 1, 31)))
    body = bytes([2]) + struct.pack("<HBHB", 30, 3, 31, 4)
    assert h.raw(GPIO, 0x10, body, session=False).detail == m.SESSION_REQUIRED
    assert ep.gpio_log == []
    assert h.ok(GPIO, 0x10, body) == b"" and ep.gpio_log == [(30, 3), (31, 4)]
    assert h.ok(GPIO, 0x10, body + m.tlv(0x3E, b"x")) == b""       # an unknown non-critical TLV: ignored


def test_gpio_read_row_is_lock_free_and_answers_n_levels():
    """fixture §1 (read): op 0x11, n(u8) n x channel(u16) -> n(u8) n x level(u8 0/1) [TLV]; lock-free (session 0, and
    another host while one holds the lock)."""
    ep, h = bench()
    assert _GPIO.op["read"] == 0x11
    h.ok(h.plan_fn, PLAN_APPLY, assign((GPIO, 1, 30), (GPIO, 1, 31)))
    h.ok(GPIO, 0x10, bytes([2]) + struct.pack("<HBHB", 30, 3, 31, 4))
    other = Host(ep, session=0)
    for who in (h, other):
        r = who.raw(GPIO, 0x11, bytes([3]) + struct.pack("<HHH", 31, 30, 31), session=False)
        assert r.succeeded and r.payload == bytes([3, 1, 0, 1])


@pytest.mark.parametrize("mode, alone, pulled_low", [
    (0, None, 0),        # input, floating: what the line outside is (gpio_inputs without a world)
    (1, 1, 0),           # input, pull-up: high unless something outside pulls it low
    (2, 0, 0),           # input, pull-down
    (3, 0, 0),           # output low: driven, the outside does not change it
    (4, 1, 1),           # output high: driven
    (5, 0, 0),           # open-drain low: pulled low
    (6, 1, 0)])          # open-drain released: high by an outside / target pull-up, low when the outside pulls it
def test_gpio_modes_mean_what_the_table_says(mode, alone, pulled_low):
    """fixture §1 mode table: 0 input floating, 1 pull-up, 2 pull-down, 3 output low, 4 output high, 5 open-drain low,
    6 open-drain released. The line takes the mode (pin_state); the level read follows it; driven modes ignore the
    outside, the input / released modes read it."""
    ep, h = bench()
    h.ok(h.plan_fn, PLAN_APPLY, assign((GPIO, 1, 30)))
    h.ok(GPIO, 0x10, bytes([1]) + struct.pack("<HB", 30, mode))
    assert ep.pin_state(30) == f"gpio {mode}"
    read = lambda: h.raw(GPIO, 0x11, bytes([1]) + struct.pack("<H", 30), session=False).payload   # noqa: E731
    assert read() == bytes([1, 0 if alone is None else alone])    # nothing outside drives the line
    if alone is None:
        ep.gpio_inputs[30] = 1                                     # floating: whatever the outside makes it
        assert read() == bytes([1, 1])
    ep.gpio_world = lambda ch, md: 0                              # something outside holds the line low
    assert h.raw(GPIO, 0x11, bytes([1]) + struct.pack("<H", 30), session=False).payload == bytes([1, pulled_low])


def test_gpio_set_checks_everything_first_then_runs_in_order():
    """fixture §1: the list runs one entry after another in the order sent (pull NRST then release it, one request); all
    the checks come first - a refused list does nothing - and a set that passes them succeeds."""
    ep, h = bench()
    h.ok(h.plan_fn, PLAN_APPLY, assign((GPIO, 1, 30), (GPIO, 1, 31)))
    for body in (bytes([2]) + struct.pack("<HBHB", 30, 3, 31, 7),          # the 2nd mode unsupported
                 bytes([2]) + struct.pack("<HBHB", 30, 3, 40, 3)):         # the 2nd channel not assigned
        assert not h.raw(GPIO, 0x10, body).succeeded
        assert ep.gpio_log == [] and 30 not in ep.gpio_modes
    r = h.raw(GPIO, 0x10, bytes([3]) + struct.pack("<HBHBHB", 30, 5, 31, 4, 30, 6))
    assert r.succeeded
    assert ep.gpio_log == [(30, 5), (31, 4), (30, 6)] and ep.pin_state(30) == "gpio 6"


def test_gpio_refusals_name_the_channel_and_its_place():
    """fixture §1: a channel not assigned (set and read) is rejected unavailable with TLV channel 0x02 and the place in
    the list, tag 0x40 index(u8); a mode not declared is rejected unsupported, payload 0x00 then the same TLVs."""
    ep, h = bench()
    h.ok(h.plan_fn, PLAN_APPLY, assign((GPIO, 1, 30)))
    for op, body in ((0x10, bytes([2]) + struct.pack("<HBHB", 30, 3, 40, 3)),
                     (0x11, bytes([2]) + struct.pack("<HH", 30, 40))):
        r = h.raw(GPIO, op, body, session=op == 0x10)
        assert r.detail == m.UNAVAILABLE
        t = una(r)
        assert t[UNA["channel"]] == struct.pack("<H", 40) and t[INDEX] == b"\x01"
    r = h.raw(GPIO, 0x10, bytes([2]) + struct.pack("<HBHB", 30, 3, 30, 8))
    assert r.detail == m.UNSUPPORTED and r.payload == b"\x00" + m.tlv(UNA["channel"], struct.pack("<H", 30)) \
        + m.tlv(INDEX, b"\x01")
    assert ep.gpio_log == []


def test_gpio_modes_are_declared_and_input_is_always_there():
    """fixture §1: describe's modes (tag 0x40, u32, bit n = mode n) say the modes the probe drives - 0 (input) always;
    a mode left out is unsupported and the client reads the set."""
    for profile in PROFILES:
        for o in offered(profile(), "oep.fixture.gpio"):
            d = dict(m.split_tlvs(b"".join(o.tlvs)))
            assert len(d[0x40]) == 4 and struct.unpack("<I", d[0x40])[0] & 1
    probe = virtual_bench.p4_x035()
    gpio = next(o for o in probe.offered if o.fn == GPIO)
    only_inputs = replaced(probe, GPIO, [t if t[0] != 0x40 else catalog.u32(0x40, 0b111) for t in gpio.tlvs])
    ep, h = bench(only_inputs)
    hst = host.Host(ep.handle, rng=random.Random(1))
    assert fixture.Gpio(hst, GPIO).modes() == 0b111
    h.ok(h.plan_fn, PLAN_APPLY, assign((GPIO, 1, 30)))
    assert h.raw(GPIO, 0x10, bytes([1]) + struct.pack("<HB", 30, 1)).succeeded
    r = h.raw(GPIO, 0x10, bytes([1]) + struct.pack("<HB", 30, 4))
    assert r.detail == m.UNSUPPORTED and r.payload[0] == 0


def test_gpio_plan_keeps_the_line_as_it_was_until_the_first_set():
    """fixture §1: a channel the plan takes keeps its state until the first set - an output idle goes on driving its
    level, a hi-z one stays hi-z; taking it changes no level. A gpio plan takes an output idle channel."""
    ep, h = bench()
    h.ok(CFG, SET, cset(idle_item(30, IDLE["output_high"]) + idle_item(31, IDLE["pull_up"])))
    before = [ep.pin_state(ch) for ch in (30, 31, 32)]
    h.ok(h.plan_fn, PLAN_APPLY, assign((GPIO, 1, 30), (GPIO, 1, 31), (GPIO, 1, 32)))
    assert ep.gpio_log == []
    assert ep.pin_state(30) == "gpio 4"                            # the output-high idle, driving on
    assert ep.pin_state(31) in (before[1], "gpio 1") and ep.pin_state(32) == before[2] == "idle hi-z"
    assert h.raw(GPIO, 0x11, bytes([1]) + struct.pack("<H", 30), session=False).payload == bytes([1, 1])
    h.ok(GPIO, 0x10, bytes([1]) + struct.pack("<HB", 32, 3))
    assert ep.pin_state(32) == "gpio 3"


def test_gpio_plan_takes_an_output_idle_channel_another_fixture_does_not():
    """fixture §1: the gpio's plan accepts a channel whose idle is an output (a uart's TX on it is held_by_settings)."""
    ep, h = bench()
    h.ok(CFG, SET, cset(idle_item(30, IDLE["output_low"])))
    r = h.raw(h.plan_fn, PLAN_APPLY, assign((UART, 2, 30)))
    assert cause(r) == reg.CORE.enum["unavailable_cause"]["held_by_settings"]
    assert h.raw(h.plan_fn, PLAN_APPLY, assign((GPIO, 1, 30))).succeeded
    assert (GPIO, 1, 30) in ep.plan and ep.pin_state(30) == "gpio 3"


def test_gpio_read_only_looks_and_release_or_end_gives_the_line_back():
    """fixture §1: read observes the level and changes nothing (no switch to input, pull or strength); releasing the
    plan or ending the session undoes the gpio's settings and returns the line to its idle (core §8 / §9)."""
    ep, h = bench()
    h.ok(CFG, SET, cset(idle_item(31, IDLE["pull_up"])))
    h.ok(h.plan_fn, PLAN_APPLY, assign((GPIO, 1, 30), (GPIO, 1, 31)))
    h.ok(GPIO, 0x10, bytes([2]) + struct.pack("<HBHB", 30, 4, 31, 5))
    state = (dict(ep.gpio_modes), list(ep.gpio_log), ep.pin_state(30), ep.pin_state(31))
    for _ in range(3):
        h.raw(GPIO, 0x11, bytes([2]) + struct.pack("<HH", 30, 31), session=False)
    assert (dict(ep.gpio_modes), list(ep.gpio_log), ep.pin_state(30), ep.pin_state(31)) == state
    h.ok(h.plan_fn, PLAN_RELEASE, bytes([1]) + struct.pack("<H", GPIO))
    assert ep.pin_state(30) == "idle hi-z" and ep.pin_state(31) == "idle pull-up"
    h.ok(h.plan_fn, PLAN_APPLY, assign((GPIO, 1, 30)))
    h.ok(GPIO, 0x10, bytes([1]) + struct.pack("<HB", 30, 3))
    assert h.raw(0, m.OP_END).succeeded
    assert ep.pin_state(30) == "idle hi-z" and not ep.plan


# ---- §2 oep.fixture.uart ------------------------------------------------------------------------------------------

@pytest.fixture
def uart():
    ep, h = bench()
    h.ok(h.plan_fn, PLAN_APPLY, assign((UART, 1, 20), (UART, 2, 21)))
    return ep, h


def test_uart_configure_row(uart):
    """fixture §2 (configure, required): op 0x10, baud(u32) [TLV] -> baud(u32: the actual one; 0 when failed) [TLV];
    needs the lock."""
    ep, h = uart
    assert _UART.op["configure"] == 0x10
    assert h.raw(UART, 0x10, struct.pack("<I", 115200), session=False).detail == m.SESSION_REQUIRED
    assert struct.unpack("<I", h.ok(UART, 0x10, struct.pack("<I", 115200)))[0] == ep._uart_actual(115200)
    ep.uart_fail_configure.add(UART)
    r = h.raw(UART, 0x10, struct.pack("<I", 9600))
    assert (r.resolution, r.detail, r.payload) == (m.COMPLETED, m.FAILED, struct.pack("<I", 0))


def test_uart_read_row(uart):
    """fixture §2 (read, required): op 0x11, from(u8) arg(u64) max(u16) [TLV] -> start(u64) flags(u8) len(u16) data
    [TLV]; lock-free."""
    ep, h = uart
    assert _UART.op["read"] == 0x11
    h.ok(UART, 0x10, struct.pack("<I", 115200))
    ep.uart_rx(UART, b"abc")
    r = Host(ep, session=0).raw(UART, 0x11, struct.pack("<BQH", 1, 0, 100), session=False)
    assert r.succeeded and r.payload == struct.pack("<QBH", 0, 0, 3) + b"abc"


def test_uart_marks_row(uart):
    """fixture §2 (marks, optional): op 0x12, from_serial(u32) -> more(u8) count(u8) count x mark [TLV]; lock-free."""
    ep, h = uart
    assert _UART.op["marks"] == 0x12
    h.ok(UART, 0x14, b"\x07")
    r = h.raw(UART, 0x12, struct.pack("<I", 0), session=False)
    assert r.succeeded and r.payload[:2] == b"\x00\x01" and len(r.payload) == 2 + 22
    serial, position, kind, _, detail = struct.unpack_from("<IQBQB", r.payload, 2)
    assert (serial, position, kind, detail) == (0, 0, reg.COMMON.enum["mark_kind"]["host"], 7)


def test_uart_clear_row(uart):
    """fixture §2 (clear, optional): op 0x13, no request, no answer; needs the lock."""
    ep, h = uart
    assert _UART.op["clear"] == 0x13
    h.ok(UART, 0x10, struct.pack("<I", 115200))
    ep.uart_rx(UART, b"abc")
    assert h.raw(UART, 0x13, session=False).detail == m.SESSION_REQUIRED
    assert h.ok(UART, 0x13) == b""
    assert h.raw(UART, 0x11, struct.pack("<BQH", 1, 0, 100), session=False).payload == struct.pack("<QBH", 3, 0, 0)


def test_uart_mark_row(uart):
    """fixture §2 (mark, optional, needs marks too): op 0x14, value(u8), no answer; needs the lock. Every bench uart
    that offers mark offers marks."""
    ep, h = uart
    assert _UART.op["mark"] == 0x14
    assert h.raw(UART, 0x14, b"\x01", session=False).detail == m.SESSION_REQUIRED
    assert h.ok(UART, 0x14, b"\x01") == b""
    for profile in PROFILES:
        e = endpoint.Endpoint(profile(), Clock())
        for fn, name in e.names.items():
            if name == "oep.fixture.uart" and e.offers(fn, 0x14):
                assert e.offers(fn, 0x12)


def test_uart_write_row(uart):
    """fixture §2 (write, optional): op 0x15, count(u16) data -> accepted(u16) [TLV]; needs the lock."""
    ep, h = uart
    assert _UART.op["write"] == 0x15
    h.ok(UART, 0x10, struct.pack("<I", 115200))
    assert h.raw(UART, 0x15, struct.pack("<H", 2) + b"ab", session=False).detail == m.SESSION_REQUIRED
    assert h.ok(UART, 0x15, struct.pack("<H", 2) + b"ab") == struct.pack("<H", 2)
    assert bytes(ep.uarts[UART].written) == b"ab"


def test_uart_status_row(uart):
    """fixture §2 (status, required): op 0x16, nothing -> baud(u32, the actual one) format(u8) [TLV]; lock-free."""
    ep, h = uart
    assert _UART.op["status"] == 0x16
    actual = struct.unpack("<I", h.ok(UART, 0x10, struct.pack("<I", 57600) + m.tlv(0x81, b"\x14")))[0]
    r = Host(ep, session=0).raw(UART, 0x16, session=False)
    assert r.succeeded and r.payload == struct.pack("<IB", actual, 0x14)


def test_uart_stream_ops_are_the_console_numbers_without_the_stream():
    """fixture §2: ops 0x11-0x15 take common §1's form without the stream u32 and carry oep.target.console's numbers."""
    con = reg.TARGET_CONSOLE.op
    for name in ("read", "marks", "clear", "mark", "write"):
        assert _UART.op[name] == con[name]
    ep, h = bench()
    h.ok(h.plan_fn, PLAN_APPLY, assign((UART, 1, 20)))
    assert h.raw(UART, 0x11, struct.pack("<BQH", 2, 0, 1), session=False).succeeded            # 11 bytes: no stream
    assert h.raw(UART, 0x11, struct.pack("<IBQH", 0, 2, 0, 1), session=False).detail == m.MALFORMED


def test_uart_optional_ops_left_out_are_unknown_and_from_a_mark_needs_marks(uart, monkeypatch):
    """fixture §2: an optional op is offered only in ops; one left out is unknown_operation. Without marks, read from 3
    is unsupported (fixed part, 0x00). No features bit declares an optional op."""
    ep, h = uart
    h.ok(UART, 0x10, struct.pack("<I", 115200))
    assert catalog.FEATURES not in decl(ep, h, UART)
    offers = ep.offers
    monkeypatch.setattr(ep, "offers", lambda fn, op: (fn != UART or op not in (0x12, 0x13, 0x14, 0x15))
                        and offers(fn, op))
    for op, body in ((0x12, struct.pack("<I", 0)), (0x13, b""), (0x14, b"\x01"), (0x15, b"\x00\x00")):
        assert h.raw(UART, op, body).detail == m.UNKNOWN_OPERATION
    r = h.raw(UART, 0x11, struct.pack("<BQH", 3, 0, 10), session=False)
    assert (r.resolution, r.detail, r.payload) == (m.REJECTED, m.UNSUPPORTED, b"\x00")
    assert h.raw(UART, 0x11, struct.pack("<BQH", 2, 0, 10), session=False).succeeded


def test_uart_without_write_takes_no_tx_role():
    """fixture §2: a uart that does not offer write takes no TX role in a plan and declares none (role_channels /
    channel_group); its write is unknown_operation. Every bench profile keeps to it."""
    for profile in PROFILES:
        e = endpoint.Endpoint(profile(), Clock())
        for fn, name in e.names.items():
            if name == "oep.fixture.uart" and not e.offers(fn, 0x15):
                roles = {r for t, v in e.decl[fn] if t == catalog.ROLE_CHANNELS for r in [v[0]]}
                assert 2 not in roles
    probe = virtual_bench.p4_x035()
    old = next(o for o in probe.offered if o.fn == UART)
    rx_only = replaced(probe, UART, virtual_bench.ops_of("oep.fixture.uart", "write")
                       + tuple(t for t in old.tlvs if t[0] != catalog.OPS and not (t[0] == catalog.ROLE_CHANNELS and t[3] == 2)))
    ep, h = bench(rx_only)
    r = h.raw(h.plan_fn, PLAN_APPLY, assign((UART, 1, 20), (UART, 2, 21)))
    assert r.detail == m.UNSUPPORTED and (UART, 2, 21) not in ep.plan
    h.ok(h.plan_fn, PLAN_APPLY, assign((UART, 1, 20)))
    h.ok(UART, 0x10, struct.pack("<I", 115200))
    assert h.raw(UART, 0x15, struct.pack("<H", 1) + b"x").detail == m.UNKNOWN_OPERATION


def test_uart_without_a_plan_is_unavailable_6_after_the_header_and_format_rules():
    """fixture §2: with no RX and no TX in a plan, a valid request of every op it offers is unavailable cause 6; core's
    header, session and format rules come first (session_required, malformed, unsupported)."""
    ep, h = bench()
    six = reg.CORE.enum["unavailable_cause"]["wrong_state"]
    for op, body, locked in ((0x10, struct.pack("<I", 115200), True), (0x11, struct.pack("<BQH", 1, 0, 1), False),
                             (0x12, struct.pack("<I", 0), False), (0x13, b"", True), (0x14, b"\x01", True),
                             (0x15, struct.pack("<H", 1) + b"x", True), (0x16, b"", False)):
        assert cause(h.raw(UART, op, body, session=locked)) == six
        if locked:
            assert h.raw(UART, op, body, session=False).detail == m.SESSION_REQUIRED
    assert h.raw(UART, 0x10, struct.pack("<I", 0)).detail == m.MALFORMED
    assert h.raw(UART, 0x10, struct.pack("<I", 115200) + m.tlv(0x81, b"\x01")).detail == m.UNSUPPORTED
    assert h.raw(UART, 0x11, b"\x01", session=False).detail == m.MALFORMED
    assert h.raw(UART, 0x16, b"\x00", session=False).detail == m.MALFORMED


def test_uart_format_bits_default_and_baud_refusals(uart):
    """fixture §2: the format TLV 0x01 (u8): bit0-1 data bits, bit2-3 parity, bit4 stop bits; none = 8N1. A value not
    declared is unsupported with the tag as received; a baud more than 5 % (uart_baud_tolerance_pct) off is unsupported
    0x00; baud 0 is malformed; a format TLV of another length is malformed."""
    ep, h = uart
    assert reg.LIMITS["uart_baud_tolerance_pct"] == 5
    h.ok(UART, 0x10, struct.pack("<I", 115200))
    assert h.ok(UART, 0x16)[4] == 0
    for fmt in (0x04, 0x08, 0x10, 0x14, 0x18):                     # 8E1 8O1 8N2 8E2 8O2 (declared)
        h.ok(UART, 0x10, struct.pack("<I", 115200) + m.tlv(0x01, bytes([fmt])))
        assert h.ok(UART, 0x16)[4] == fmt
    for raw in (0x01, 0x81):
        for fmt in (0x01, 0x02, 0x0C, 0x20):                     # 7N1 not declared, data 2, parity 3, a reserved bit
            r = h.raw(UART, 0x10, struct.pack("<I", 115200) + m.tlv(raw & 0x7F, bytes([fmt]), critical=raw > 0x7F))
            assert (r.detail, r.payload) == (m.UNSUPPORTED, bytes([raw]))
    assert h.raw(UART, 0x10, struct.pack("<I", 0)).detail == m.MALFORMED
    assert h.raw(UART, 0x10, struct.pack("<I", 115200) + m.tlv(0x01, b"\x00\x00")).detail == m.MALFORMED
    ep.uart_clock_hz = 1_000_000                                   # dividers of 1 MHz: 1 MHz, 500 kHz, 333 kHz, ...
    assert struct.unpack("<I", h.ok(UART, 0x10, struct.pack("<I", 476_191)))[0] == 500_000   # just within 5 %: taken
    for baud in (476_190, 420_000):                               # just past 5 %, and far
        r = h.raw(UART, 0x10, struct.pack("<I", baud))
        assert (r.detail, r.payload) == (m.UNSUPPORTED, b"\x00")
    assert h.ok(UART, 0x16)[:4] == struct.pack("<I", 500_000)      # nothing changed


def test_uart_configure_comes_up_or_fails_back_to_not_configured(uart):
    """fixture §2: configure answers success once RX / TX run at the actual baud and format; one that cannot come up
    stops the UART, is not configured, answers completed failed baud 0 - RX back to its idle (core §8), TX kept idling
    high. Either way an accepted configure drops the TX not yet sent."""
    ep, h = uart
    h.ok(h.plan_fn, PLAN_RELEASE, bytes([0]))
    h.ok(CFG, SET, cset(idle_item(20, IDLE["pull_up"])))
    h.ok(h.plan_fn, PLAN_APPLY, assign((UART, 1, 20), (UART, 2, 21)))
    assert ep.pin_state(20) == "idle pull-up" and ep.pin_state(21) == "uart-tx-high"   # RX unused before configure
    ep.uart_accept = 100
    h.ok(UART, 0x10, struct.pack("<I", 115200))
    assert ep.pin_state(20) == "input" and h.ok(UART, 0x16) == struct.pack("<IB", ep._uart_actual(115200), 0)
    h.ok(UART, 0x15, struct.pack("<H", 3) + b"abc")
    ep.uart_rx(UART, b"kept")
    h.ok(UART, 0x10, struct.pack("<I", 57600))
    assert bytes(ep.uarts[UART].written) == b""                   # unsent TX dropped by a configure that succeeds
    h.ok(UART, 0x15, struct.pack("<H", 2) + b"de")
    ep.uart_fail_configure.add(UART)
    r = h.raw(UART, 0x10, struct.pack("<I", 9600))
    assert (r.resolution, r.detail, r.payload) == (m.COMPLETED, m.FAILED, struct.pack("<I", 0))
    assert bytes(ep.uarts[UART].written) == b""                   # ... and by one that fails
    assert h.ok(UART, 0x16) == struct.pack("<IB", 0, 0)
    assert ep.pin_state(20) == "idle pull-up" and ep.pin_state(21) == "uart-tx-high"
    ep.uart_rx(UART, b"lost")
    assert h.raw(UART, 0x11, struct.pack("<BQH", 1, 0, 100), session=False).payload.endswith(b"kept")
    assert cause(h.raw(UART, 0x15, struct.pack("<H", 1) + b"x")) == 6
    h.ok(UART, 0x10, struct.pack("<I", 115200))
    assert h.ok(UART, 0x16)[:4] != b"\0\0\0\0"


def test_uart_status_is_zero_before_the_first_configure_and_after_a_failed_one(uart):
    """fixture §2: status (lock-free) is the actual baud / format once configured; baud 0 format 0 before the first
    configure and after a failed one - 0 marks not configured, nothing runs at a default baud."""
    ep, h = uart
    other = Host(ep, session=0)
    assert other.raw(UART, 0x16, session=False).payload == struct.pack("<IB", 0, 0)
    ep.uart_rx(UART, b"x")
    assert h.raw(UART, 0x11, struct.pack("<BQH", 1, 0, 9), session=False).payload == struct.pack("<QBH", 0, 0, 0)
    actual = struct.unpack("<I", h.ok(UART, 0x10, struct.pack("<I", 115200) + m.tlv(0x01, b"\x04")))[0]
    assert other.raw(UART, 0x16, session=False).payload == struct.pack("<IB", actual, 4)
    ep.uart_fail_configure.add(UART)
    h.raw(UART, 0x10, struct.pack("<I", 9600))
    assert other.raw(UART, 0x16, session=False).payload == struct.pack("<IB", 0, 0)


def test_uart_stream_belongs_to_the_plan_and_a_configure_keeps_what_it_holds(uart):
    """fixture §2: the plan makes the stream and its release ends it with the session's configure; nothing is received
    before a configure; while configured bytes are kept whoever reads; a configure again (success or failure) keeps the
    bytes, marks, position and serial; position and serials never go back within a boot."""
    ep, h = uart
    h.ok(UART, 0x10, struct.pack("<I", 115200))
    ep.uart_rx(UART, b"abc")
    h.ok(UART, 0x14, b"\x05")
    other = Host(ep, session=0)
    assert other.raw(UART, 0x11, struct.pack("<BQH", 1, 0, 9), session=False).payload.endswith(b"abc")
    snapshot = lambda: (bytes(ep.uarts[UART].data), ep.uarts[UART].base, list(ep.uarts[UART].marks),   # noqa: E731
                        ep.uarts[UART].serial)
    before = snapshot()
    h.ok(UART, 0x10, struct.pack("<I", 57600))
    ep.uart_fail_configure.add(UART)
    h.raw(UART, 0x10, struct.pack("<I", 9600))
    assert snapshot() == before
    h.ok(UART, 0x10, struct.pack("<I", 115200))
    h.ok(h.plan_fn, PLAN_RELEASE, bytes([1]) + struct.pack("<H", UART))
    assert UART not in ep.uarts and cause(h.raw(UART, 0x16)) == 6
    h.ok(h.plan_fn, PLAN_APPLY, assign((UART, 1, 20)))
    assert h.ok(UART, 0x16) == struct.pack("<IB", 0, 0)            # the configure went with the plan
    ep.uart_rx(UART, b"early")
    r = h.raw(UART, 0x11, struct.pack("<BQH", 1, 0, 9), session=False)
    assert r.payload == struct.pack("<QBH", 3, 0, 0)               # nothing received, the position carried on
    h.ok(UART, 0x14, b"\x06")
    assert ep.uarts[UART].marks[-1][0] == 1                        # serials go on


def test_uart_configured_reassignment_is_refused_and_an_unconfigured_one_starts_afresh(uart):
    """fixture §2: changing a configured uart's assignments is unavailable cause 6 (the same set is no change); an
    unconfigured one is replaced - buffer and marks go, it stays unconfigured, position and serial carry on."""
    ep, h = uart
    h.ok(UART, 0x10, struct.pack("<I", 115200))
    ep.uart_rx(UART, b"abc")
    h.ok(UART, 0x14, b"\x01")
    assert h.raw(h.plan_fn, PLAN_APPLY, assign((UART, 1, 20), (UART, 2, 21))).succeeded   # the same set
    assert h.ok(UART, 0x16)[:4] != b"\0\0\0\0"
    assert cause(h.raw(h.plan_fn, PLAN_APPLY, assign((UART, 1, 22), (UART, 2, 21)))) == 6
    assert {a for a in ep.plan if a[0] == UART} == {(UART, 1, 20), (UART, 2, 21)}
    ep.uart_fail_configure.add(UART)
    h.raw(UART, 0x10, struct.pack("<I", 9600))                     # not configured now
    h.ok(h.plan_fn, PLAN_APPLY, assign((UART, 1, 22), (UART, 2, 21)))
    s = ep.uarts[UART]
    assert (bytes(s.data), s.marks, s.base, s.serial) == (b"", [], 3, 1)
    assert h.ok(UART, 0x16) == struct.pack("<IB", 0, 0)


def test_uart_tx_idles_high_from_the_plan_and_its_release_frees_the_line():
    """fixture §2: TX idles high (not sending) while the plan holds it, before configure too; the plan's release stops
    the UART driving and the line goes to its idle (core §8) - an idle item may make it a pull-up input."""
    ep, h = bench()
    h.ok(CFG, SET, cset(idle_item(23, IDLE["pull_up"])))
    h.ok(h.plan_fn, PLAN_APPLY, assign((UART, 2, 21), (UART, 1, 20)))
    assert ep.pin_state(21) == "uart-tx-high"
    h.ok(h.plan_fn, PLAN_RELEASE, bytes([1]) + struct.pack("<H", UART))
    assert ep.pin_state(21) == "idle hi-z"
    h.ok(h.plan_fn, PLAN_APPLY, assign((UART, 2, 23)))
    h.ok(UART, 0x10, struct.pack("<I", 115200))
    assert ep.pin_state(23) == "uart-tx-high"
    h.ok(h.plan_fn, PLAN_RELEASE, bytes([0]))
    assert ep.pin_state(23) == "idle pull-up"


def test_uart_formats_are_declared_and_8n1_is_one():
    """fixture §2: describe's formats (tag 0x40: n(u8) n x u8, configure's format values) - 8N1 (0) always; a declared
    one is taken, one not declared is unsupported."""
    for profile in PROFILES:
        for o in offered(profile(), "oep.fixture.uart"):
            v = dict(m.split_tlvs(b"".join(o.tlvs)))[0x40]
            assert len(v) == 1 + v[0] and 0 in v[1:]
    probe = virtual_bench.p4_x035()
    old = next(o for o in probe.offered if o.fn == UART)
    only_8n1 = replaced(probe, UART, [t if t[0] != 0x40 else catalog.tlv(0x40, b"\x01\x00") for t in old.tlvs])
    ep, h = bench(only_8n1)
    h.ok(h.plan_fn, PLAN_APPLY, assign((UART, 1, 20)))
    h.ok(UART, 0x10, struct.pack("<I", 115200) + m.tlv(0x01, b"\x00"))
    assert h.raw(UART, 0x10, struct.pack("<I", 115200) + m.tlv(0x01, b"\x04")).payload == b"\x01"


def test_uart_checker_catches_baud_0_answered_unsupported():
    """fixture §2 (mutant): baud 0 is malformed; a fixture answering it unsupported fails OEP-UART-BAUD-BOUNDARY."""
    model, c = uart_setup()
    original = model.handle

    def broken(request):
        reply = original(request)
        if request[3:6] == b"\2\0\x10" and request[10:14] == b"\0\0\0\0" and reply[3:5] == bytes([m.REJECTED, m.MALFORMED]):
            reply = reply[:4] + bytes([m.UNSUPPORTED]) + b"\x00"
        return reply
    c.send = broken
    checker = UartChecks(c, SampleUartAdapter(model))
    checker.cases = ("BAUD-BOUNDARY",)
    r = checker.run()
    assert next(row for row in r["checks"] if row["id"] == "OEP-UART-BAUD-BOUNDARY")["status"] == "failed"


# ---- §3 oep.fixture.i2c-target ------------------------------------------------------------------------------------

@pytest.fixture
def i2c():
    ep, h = bench()
    h.ok(h.plan_fn, PLAN_APPLY, assign((I2C, 1, 30), (I2C, 2, 31)))
    return ep, h


@pytest.mark.parametrize("name, code, body, locked", [
    ("configure", 0x10, b"\x42", True), ("read_rx", 0x11, b"", True), ("preload_tx", 0x12, b"\x01\x00\x55", True),
    ("status", 0x13, b"", False), ("stretch", 0x14, struct.pack("<I", 5), True)],
    ids=["configure", "read_rx", "preload_tx", "status", "stretch"])
def test_i2c_op_rows(i2c, name, code, body, locked):
    """fixture §3 table: the op numbers, which need the lock (all but status), and the answers' shapes: configure,
    preload_tx and stretch nothing; read_rx pending(u8) count(u16) data [TLV ns(u64)]; status state(u8) queued(u8)
    rx_frames(u32) tx_slots(u8) errors(u32)."""
    ep, h = i2c
    assert _I2C.op[name] == code
    h.ok(I2C, 0x10, b"\x42")
    ep.now_ns = lambda: 1234
    ep.i2c_write(I2C, b"\x10\x20")
    r = h.raw(I2C, code, body, session=False)
    if locked:
        assert r.detail == m.SESSION_REQUIRED
        r = h.raw(I2C, code, body)
    assert r.succeeded
    if name == "read_rx":
        assert r.payload == struct.pack("<BH", 0, 2) + b"\x10\x20" + m.tlv(0x01, struct.pack("<Q", 1234))
    elif name == "status":
        assert r.payload == struct.pack("<BBIBI", 1, 1, 1, 0, 0)
    else:
        assert r.payload == b""


def test_i2c_running_assignment_change_is_6_and_release_or_replace_starts_afresh(i2c):
    """fixture §3: in state 1 a change of the (role, channel) set is unavailable cause 6 (the same set is no change);
    releasing the plan or replacing a state-0 one goes back to the start - state 0, queue, slots and counters gone,
    stretch 0."""
    ep, h = i2c
    h.ok(I2C, 0x14, struct.pack("<I", 40))
    h.ok(I2C, 0x10, b"\x42")
    ep.i2c_write(I2C, b"\x01")
    h.ok(h.plan_fn, PLAN_APPLY, assign((I2C, 1, 30), (I2C, 2, 31)))
    assert ep.i2c[I2C].state == 1 and len(ep.i2c[I2C].queue) == 1
    assert cause(h.raw(h.plan_fn, PLAN_APPLY, assign((I2C, 1, 32), (I2C, 2, 31)))) == 6
    h.ok(h.plan_fn, PLAN_RELEASE, bytes([1]) + struct.pack("<H", I2C))
    assert h.ok(I2C, 0x13) == struct.pack("<BBIBI", 0, 0, 0, 0, 0) and ep.i2c[I2C].stretch_us == 0
    h.ok(h.plan_fn, PLAN_APPLY, assign((I2C, 1, 30), (I2C, 2, 31)))
    h.ok(I2C, 0x14, struct.pack("<I", 40))
    h.ok(h.plan_fn, PLAN_APPLY, assign((I2C, 1, 32), (I2C, 2, 31)))   # state 0: replaced
    assert ep.i2c[I2C].stretch_us == 0 and ep.i2c[I2C].state == 0


@pytest.mark.parametrize("pullups", [False, True])
def test_i2c_drives_open_drain_only_with_its_pullups_as_declared(pullups):
    """fixture §3: SDA / SCL are only pulled low or released, never driven high; a probe that puts its own pull-ups on
    them while configured declares features bit2, one that does not puts none."""
    probe = virtual_bench.p4_x035()
    ep, h = bench(virtual_bench.with_i2c_pullups(probe) if pullups else probe)
    features = struct.unpack("<I", decl(ep, h, I2C)[catalog.FEATURES])[0]
    assert bool(features & 0x04) == pullups
    h.ok(h.plan_fn, PLAN_APPLY, assign((I2C, 1, 30), (I2C, 2, 31)))
    h.ok(I2C, 0x10, b"\x42")
    want = "open-drain pull-up" if pullups else "open-drain"
    assert ep.pin_state(30) == ep.pin_state(31) == want
    ep.i2c_write(I2C, b"\x01\x02")
    ep.i2c_read(I2C, 2)
    assert ep.pin_state(30) == ep.pin_state(31) == want


def test_i2c_state_0_acks_nothing_and_leaves_the_lines_idle():
    """fixture §3: in state 0 no address is ACKed, the I2C function drives nothing and its pull-ups are off: the
    channels are in their idle (core §8) - an idle's pull-up is that, not the I2C function's."""
    ep, h = bench(virtual_bench.with_i2c_pullups(virtual_bench.p4_x035()))
    h.ok(CFG, SET, cset(idle_item(30, IDLE["pull_up"])))
    h.ok(h.plan_fn, PLAN_APPLY, assign((I2C, 1, 30), (I2C, 2, 31)))
    assert (ep.pin_state(30), ep.pin_state(31)) == ("idle pull-up", "idle hi-z")
    assert not ep.i2c_write(I2C, b"\x01") and ep.i2c_read(I2C, 1) is None
    h.ok(I2C, 0x10, b"\x42")
    assert ep.pin_state(30) == "open-drain pull-up"
    ep.target_fail_configure.add(I2C)
    h.raw(I2C, 0x10, b"\x42")
    assert (ep.pin_state(30), ep.pin_state(31)) == ("idle pull-up", "idle hi-z")
    assert not ep.i2c_write(I2C, b"\x01", 0x42) and ep.i2c_read(I2C, 1, 0x42) is None


def test_i2c_configure_makes_the_target_anew_and_refuses_in_order(i2c):
    """fixture §3: configure makes the target anew (frames, slots, rx_frames and errors go; stretch stays); without
    this fn's plan it is unavailable cause 6; address > 0x7F malformed; 0x00-0x07 and 0x78-0x7F unsupported 0x00."""
    ep, h = i2c
    h.ok(I2C, 0x14, struct.pack("<I", 9))
    h.ok(I2C, 0x10, b"\x42")
    ep.i2c_write(I2C, bytes(200))
    h.ok(I2C, 0x12, b"\x01\x00\x01")
    assert h.ok(I2C, 0x13) == struct.pack("<BBIBI", 1, 1, 1, 1, 1)
    h.ok(I2C, 0x10, b"\x43")
    assert h.ok(I2C, 0x13) == struct.pack("<BBIBI", 1, 0, 0, 0, 0) and ep.i2c[I2C].stretch_us == 9
    for address in (0x00, 0x07, 0x78, 0x7F):
        r = h.raw(I2C, 0x10, bytes([address]))
        assert (r.detail, r.payload) == (m.UNSUPPORTED, b"\x00")
    for address in (0x08, 0x77):
        assert h.raw(I2C, 0x10, bytes([address])).succeeded
    assert h.raw(I2C, 0x10, b"\x80").detail == m.MALFORMED
    h.ok(h.plan_fn, PLAN_RELEASE, bytes([0]))
    assert cause(h.raw(I2C, 0x10, b"\x42")) == 6
    assert h.raw(I2C, 0x10, b"\xff").detail == m.MALFORMED        # the form before the state


def test_i2c_failed_configure_goes_back_to_state_0_keeping_the_stretch(i2c):
    """fixture §3: configure answers success in state 1; one that cannot come up goes to state 0 with the queue and
    slots empty and rx_frames / errors 0, keeps stretch, and answers completed failed with no fixed part."""
    ep, h = i2c
    h.ok(I2C, 0x14, struct.pack("<I", 12))
    h.ok(I2C, 0x10, b"\x42")
    assert h.ok(I2C, 0x13)[0] == 1
    ep.i2c_write(I2C, bytes(200))
    h.ok(I2C, 0x12, b"\x01\x00\x01")
    ep.target_fail_configure.add(I2C)
    r = h.raw(I2C, 0x10, b"\x42")
    assert (r.resolution, r.detail, r.payload) == (m.COMPLETED, m.FAILED, b"")
    assert h.ok(I2C, 0x13) == struct.pack("<BBIBI", 0, 0, 0, 0, 0) and ep.i2c[I2C].stretch_us == 12
    assert cause(h.raw(I2C, 0x11)) == 6
    h.ok(I2C, 0x10, b"\x42")
    assert h.ok(I2C, 0x13)[0] == 1


def test_i2c_preload_is_ready_for_the_next_read_once_answered(i2c):
    """fixture §3: preload_tx's success means the data answers a controller read that starts after it."""
    ep, h = i2c
    h.ok(I2C, 0x10, b"\x42")
    h.ok(I2C, 0x12, b"\x02\x00\xab\xcd")
    assert ep.i2c_read(I2C, 2) == b"\xab\xcd"


def test_i2c_counts_stop_at_their_limits_and_go_with_configure_or_release(i2c):
    """fixture §3 status: queued stops at 255 (pending too), rx_frames and errors saturate at 0xFFFFFFFF, and both go
    back to 0 with configure or the plan's release."""
    ep, h = i2c
    h.ok(I2C, 0x10, b"\x42")
    st = ep.i2c[I2C]
    st.queue.extend((b"x", 0) for _ in range(300))
    st.rx_frames = st.errors = 0xFFFFFFFF
    ep.target_decl[I2C] = (ep.target_decl[I2C][0], ep.target_decl[I2C][1], 1000, ep.target_decl[I2C][3])
    ep.i2c_write(I2C, bytes(200))                                  # over max_length: an error, a frame
    assert h.ok(I2C, 0x13) == struct.pack("<BBIBI", 1, 255, 0xFFFFFFFF, 0, 0xFFFFFFFF)
    assert h.ok(I2C, 0x11)[0] == 255
    h.ok(I2C, 0x10, b"\x42")
    assert h.ok(I2C, 0x13) == struct.pack("<BBIBI", 1, 0, 0, 0, 0)
    ep.i2c_write(I2C, bytes(200))
    h.ok(h.plan_fn, PLAN_RELEASE, bytes([0]))
    assert h.ok(I2C, 0x13) == struct.pack("<BBIBI", 0, 0, 0, 0, 0)


def test_i2c_describe_declares_its_limits():
    """fixture §3 describe: ops, role_channels, max_length, max_clock_hz, features (bit2 only), queue_depth (0x40 u8),
    max_stretch_us (0x41 u32, at least 1) exactly when stretch is offered; the client reads them."""
    for profile in (virtual_bench.p4_x035, virtual_bench.esp32_v003):
        ep, h = bench(profile)
        fn = ep.fns["oep.fixture.i2c-target"]
        d = decl(ep, h, fn)
        assert {catalog.OPS, catalog.ROLE_CHANNELS, catalog.MAX_LENGTH, catalog.MAX_CLOCK_HZ, catalog.FEATURES, 0x40} <= set(d)
        assert len(d[0x40]) == 1 and d[0x40][0] >= 1
        assert not struct.unpack("<I", d[catalog.FEATURES])[0] & ~0x04
        stretch = ep.offers(fn, 0x14)
        assert (0x41 in d) == stretch and (not stretch or struct.unpack("<I", d[0x41])[0] >= 1)
        t = fixture.I2cTarget(host.Host(ep.handle, rng=random.Random(1)), fn)
        assert t.queue_depth == d[0x40][0] and t.max_length == struct.unpack("<H", d[catalog.MAX_LENGTH])[0]


@pytest.mark.parametrize("profile", PROFILES)
def test_target_max_length_fits_the_frame(profile):
    """fixture §3 / §4: max_length keeps preload_tx (10 + 2 + data) and read_rx's answer (5 + 1 + 2 + data) of the i2c
    target, arm (10 + 2 + 2 + tx) and read_rx's answer (5 + 1 + 4 + 2 + data) of the spi target within max_frame."""
    probe = profile()
    for o in probe.offered:
        if o.name in ("oep.fixture.i2c-target", "oep.fixture.spi-target"):
            length = struct.unpack("<H", dict(m.split_tlvs(b"".join(o.tlvs)))[catalog.MAX_LENGTH])[0]
            need = (12 if o.name == "oep.fixture.i2c-target" else 14) + length
            assert need <= probe.max_frame


# ---- §4 oep.fixture.spi-target ------------------------------------------------------------------------------------

@pytest.fixture
def spi():
    ep, h = bench()
    h.ok(h.plan_fn, PLAN_APPLY, assign((SPI, 1, 30), (SPI, 2, 31), (SPI, 3, 32), (SPI, 4, 33)))
    return ep, h


@pytest.mark.parametrize("name, code, body, locked", [
    ("configure", 0x10, b"\x00\x00", True), ("arm", 0x11, struct.pack("<HH", 2, 1) + b"\x5a", True),
    ("read_rx", 0x12, b"", True), ("status", 0x13, b"", False)],
    ids=["configure", "arm", "read_rx", "status"])
def test_spi_op_rows(spi, name, code, body, locked):
    """fixture §4 table: the op numbers, the lock (all but status) and the answers: configure and arm nothing; read_rx
    pending(u8) bits(u32) count(u16) data [TLV ns(u64)]; status state mode bit_order armed queued (u8 each)
    transactions(u32) errors(u32)."""
    ep, h = spi
    assert _SPI.op[name] == code
    h.ok(SPI, 0x10, b"\x01\x01")
    ep.now_ns = lambda: 77
    h.ok(SPI, 0x11, struct.pack("<HH", 2, 0))
    ep.spi_transfer(SPI, b"\x01\x02", bits=12)
    r = h.raw(SPI, code, body, session=False)
    if locked:
        assert r.detail == m.SESSION_REQUIRED
        r = h.raw(SPI, code, body)
    assert r.succeeded
    if name == "read_rx":
        assert r.payload == struct.pack("<BIH", 0, 12, 2) + b"\x01\x02" + m.tlv(0x01, struct.pack("<Q", 77))
    elif name == "status":
        assert r.payload == struct.pack("<BBBBBII", 1, 1, 1, 0, 1, 1, 0)
    else:
        assert r.payload == b""


def test_spi_running_assignment_change_is_6_and_release_starts_afresh(spi):
    """fixture §4: in state 1 a change of the (role, channel) set is unavailable cause 6 (the same set is no change);
    releasing the plan or replacing a state-0 one goes back to state 0 with mode and bit_order 0, queue, arm and counts
    gone."""
    ep, h = spi
    h.ok(SPI, 0x10, b"\x03\x01")
    h.ok(SPI, 0x11, struct.pack("<HH", 1, 0))
    rows = ((SPI, 1, 30), (SPI, 2, 31), (SPI, 3, 32), (SPI, 4, 33))
    assert h.raw(h.plan_fn, PLAN_APPLY, assign(*rows)).succeeded and ep.spi[SPI].armed is not None
    assert cause(h.raw(h.plan_fn, PLAN_APPLY, assign(*rows[:3], (SPI, 4, 34)))) == 6
    h.ok(h.plan_fn, PLAN_RELEASE, bytes([1]) + struct.pack("<H", SPI))
    assert h.ok(SPI, 0x13) == struct.pack("<BBBBBII", 0, 0, 0, 0, 0, 0, 0)
    h.ok(h.plan_fn, PLAN_APPLY, assign(*rows))
    h.ok(h.plan_fn, PLAN_APPLY, assign(*rows[:3], (SPI, 4, 34)))    # state 0: replaced
    assert h.ok(SPI, 0x13) == struct.pack("<BBBBBII", 0, 0, 0, 0, 0, 0, 0)


def test_spi_configure_refuses_a_mode_it_does_not_declare_and_changes_nothing():
    """fixture §4: a mode not in describe's modes is unsupported (fixed part, 0x00) and changes nothing; mode > 3 or
    bit_order > 1 malformed; bit_order 1 without features bit0 unsupported; no plan unavailable 6; configure makes the
    target anew."""
    probe = virtual_bench.p4_x035()
    old = next(o for o in probe.offered if o.fn == SPI)
    ep, h = bench(replaced(probe, SPI, [t if t[0] != 0x41 else catalog.u8(0x41, 0b0101) for t in old.tlvs]))
    assert cause(h.raw(SPI, 0x10, b"\x00\x00")) == 6
    h.ok(h.plan_fn, PLAN_APPLY, assign((SPI, 1, 30), (SPI, 2, 31), (SPI, 3, 32), (SPI, 4, 33)))
    h.ok(SPI, 0x10, b"\x02\x01")
    h.ok(SPI, 0x11, struct.pack("<HH", 1, 0))
    before = h.ok(SPI, 0x13)
    for body in (b"\x01\x00", b"\x03\x00"):
        r = h.raw(SPI, 0x10, body)
        assert (r.detail, r.payload) == (m.UNSUPPORTED, b"\x00")
    for body in (b"\x04\x00", b"\x00\x02"):
        assert h.raw(SPI, 0x10, body).detail == m.MALFORMED
    assert h.ok(SPI, 0x13) == before
    ep.spi_transfer(SPI, b"\x01")
    h.ok(SPI, 0x10, b"\x00\x00")
    assert h.ok(SPI, 0x13) == struct.pack("<BBBBBII", 1, 0, 0, 0, 0, 0, 0)
    ep2, h2 = bench(virtual_bench.esp32_v003)
    h2.ok(h2.plan_fn, PLAN_APPLY, assign((V003_SPI, 1, 18), (V003_SPI, 2, 19), (V003_SPI, 3, 5), (V003_SPI, 4, 4)))
    r = h2.raw(V003_SPI, 0x10, b"\x00\x01")
    assert (r.detail, r.payload) == (m.UNSUPPORTED, b"\x00")


def test_spi_failed_configure_goes_back_to_state_0_and_the_idle_lines(spi):
    """fixture §4: configure answers success in state 1; one that cannot come up goes to state 0 with mode, bit_order,
    armed, queued, transactions and errors 0, drops the queue and the arm's data, stops driving (core §8 idle), and
    answers completed failed with no fixed part; transfers in state 0 are neither kept nor counted."""
    ep, h = spi
    h.ok(SPI, 0x10, b"\x01\x01")
    assert h.ok(SPI, 0x13)[0] == 1
    ep.spi_transfer(SPI, b"\x01")
    h.ok(SPI, 0x11, struct.pack("<HH", 1, 1) + b"\x55")
    ep.target_fail_configure.add(SPI)
    r = h.raw(SPI, 0x10, b"\x02\x00")
    assert (r.resolution, r.detail, r.payload) == (m.COMPLETED, m.FAILED, b"")
    assert h.ok(SPI, 0x13) == struct.pack("<BBBBBII", 0, 0, 0, 0, 0, 0, 0)
    assert ep.spi[SPI].armed is None and not ep.spi[SPI].queue
    assert {ep.pin_state(ch) for ch in (30, 31, 32, 33)} == {"idle hi-z"}
    assert ep.spi_transfer(SPI, b"\x01") == b"\x00" and h.ok(SPI, 0x13)[5:] == struct.pack("<II", 0, 0)


def test_spi_arm_in_state_0_is_6_and_changes_nothing(spi):
    """fixture §4: arm in state 0 is rejected unavailable cause 6 and leaves the state as it was."""
    ep, h = spi
    before = h.ok(SPI, 0x13)
    assert cause(h.raw(SPI, 0x11, struct.pack("<HH", 1, 1) + b"\x55")) == 6
    assert h.ok(SPI, 0x13) == before and ep.spi[SPI].armed is None
    h.ok(SPI, 0x10, b"\x00\x00")
    assert ep.spi_transfer(SPI, b"\x01") == b"\x00"                # the refused arm left nothing waiting


def test_spi_arm_that_cannot_get_miso_ready_fails_and_arms_nothing(spi):
    """fixture §4: arm answers success once tx can go out on the next transfer; a configured target that cannot get
    there arms nothing and answers completed failed (no fixed part)."""
    ep, h = spi
    h.ok(SPI, 0x10, b"\x00\x00")
    ep.spi_fail_arm.add(SPI)
    r = h.raw(SPI, 0x11, struct.pack("<HH", 1, 1) + b"\x55")
    assert (r.resolution, r.detail, r.payload) == (m.COMPLETED, m.FAILED, b"")
    assert h.ok(SPI, 0x13)[3] == 0
    h.ok(SPI, 0x11, struct.pack("<HH", 1, 1) + b"\x55")
    assert ep.spi_transfer(SPI, b"\x00") == b"\x55"


def test_spi_counts_stop_at_their_limits_and_go_with_configure_or_release(spi):
    """fixture §4: bits stops at 0xFFFFFFFF, queued (and pending) at 255; transactions and errors saturate at
    0xFFFFFFFF and go back to 0 with configure or the plan's release."""
    ep, h = spi
    h.ok(SPI, 0x10, b"\x00\x00")
    h.ok(SPI, 0x11, struct.pack("<HH", 1, 0))
    ep.spi_transfer(SPI, b"\x01", bits=1 << 33)
    assert struct.unpack_from("<BIH", h.ok(SPI, 0x12)) == (0, 0xFFFFFFFF, 1)
    st = ep.spi[SPI]
    st.queue.extend((8, b"x", 0) for _ in range(300))
    st.transactions = st.errors = 0xFFFFFFFF
    ep.spi_transfer(SPI, b"\x01")                                  # not armed: both would grow
    assert h.ok(SPI, 0x13) == struct.pack("<BBBBBII", 1, 0, 0, 0, 255, 0xFFFFFFFF, 0xFFFFFFFF)
    assert h.ok(SPI, 0x12)[0] == 255
    h.ok(SPI, 0x10, b"\x00\x00")
    assert h.ok(SPI, 0x13)[4:] == struct.pack("<BII", 0, 0, 0)
    ep.spi_transfer(SPI, b"\x01")
    h.ok(h.plan_fn, PLAN_RELEASE, bytes([0]))
    assert h.ok(SPI, 0x13) == struct.pack("<BBBBBII", 0, 0, 0, 0, 0, 0, 0)


@pytest.mark.parametrize("profile, cs_setup", [(virtual_bench.p4_x035, 0), (virtual_bench.esp32_v003, 4000)])
def test_spi_describe_declares_modes_and_cs_setup(profile, cs_setup):
    """fixture §4 describe: ops, role_channels / channel_group, max_length, max_clock_hz, features (bit0 only),
    queue_depth (0x40 u8), modes (0x41 u8, required: bits 4-7 0, at least one mode), cs_setup_ns (0x43 u32, left out
    = 0: MISO driven at once). The client shows cs_setup_ns."""
    ep, h = bench(profile)
    fn = ep.fns["oep.fixture.spi-target"]
    d = decl(ep, h, fn)
    assert {catalog.OPS, catalog.MAX_LENGTH, catalog.MAX_CLOCK_HZ, catalog.FEATURES, 0x40, 0x41} <= set(d)
    assert catalog.ROLE_CHANNELS in d or catalog.CHANNEL_GROUP in d
    assert len(d[0x41]) == 1 and 1 <= d[0x41][0] <= 0x0F
    assert not struct.unpack("<I", d[catalog.FEATURES])[0] & ~0x01
    assert len(d[0x40]) == 1 and d[0x40][0] >= 1
    assert (0x43 in d) == bool(cs_setup) and (not cs_setup or struct.unpack("<I", d[0x43])[0] == cs_setup)
    assert fixture.SpiTarget(host.Host(ep.handle, rng=random.Random(1)), fn).cs_setup_ns == cs_setup


# ---- review additions (independent review of docs/trace/fixture.json) ---------------------------------------------

@pytest.mark.parametrize("mode, level", [(0, 1), (1, 1), (2, 1), (3, 0), (4, 1), (5, 0), (6, 1)])
def test_gpio_modes_against_an_outside_high(mode, level):
    """fixture §1 mode table, the other half: with something outside holding the line high, the input modes (0 floating,
    1 pull-up, 2 pull-down) and the released open drain (6) read it high, while output low (3) and open-drain low (5)
    keep pulling the line low and output high (4) drives it high - the driven modes ignore the outside."""
    ep, h = bench()
    h.ok(h.plan_fn, PLAN_APPLY, assign((GPIO, 1, 30)))
    h.ok(GPIO, 0x10, bytes([1]) + struct.pack("<HB", 30, mode))
    ep.gpio_world = lambda ch, md: 1
    assert h.raw(GPIO, 0x11, bytes([1]) + struct.pack("<H", 30), session=False).payload == bytes([1, level])


def test_uart_clear_may_come_without_marks(uart, monkeypatch):
    """fixture §2: clear may be offered without marks (and then needs no clear mark): it empties the stream and the
    position carries on; marks and mark stay unknown_operation, read from 3 unsupported."""
    ep, h = uart
    h.ok(UART, 0x10, struct.pack("<I", 115200))
    offers = ep.offers
    monkeypatch.setattr(ep, "offers", lambda fn, op: (fn != UART or op not in (0x12, 0x14)) and offers(fn, op))
    ep.uart_rx(UART, b"abc")
    assert h.ok(UART, 0x13) == b""
    assert h.raw(UART, 0x11, struct.pack("<BQH", 1, 0, 9), session=False).payload == struct.pack("<QBH", 3, 0, 0)
    assert h.raw(UART, 0x12, struct.pack("<I", 0), session=False).detail == m.UNKNOWN_OPERATION
    r = h.raw(UART, 0x11, struct.pack("<BQH", 3, 0, 9), session=False)
    assert (r.detail, r.payload) == (m.UNSUPPORTED, b"\x00")


def test_uart_write_needs_tx_and_a_tx_only_plan_receives_nothing():
    """fixture §2: a uart that offers write answers write unavailable cause 6 while its plan has no TX (configured RX
    only) or is not configured; a TX-only plan and a plan not configured receive nothing, while what was kept before
    stays readable by the ordinary read."""
    ep, h = bench()
    h.ok(h.plan_fn, PLAN_APPLY, assign((UART, 1, 20)))
    assert cause(h.raw(UART, 0x15, struct.pack("<H", 1) + b"x")) == 6          # RX only, not configured
    h.ok(UART, 0x10, struct.pack("<I", 115200))
    assert cause(h.raw(UART, 0x15, struct.pack("<H", 1) + b"x")) == 6          # RX only, configured: still no TX
    ep.uart_rx(UART, b"kept")
    h.ok(UART, 0x10, struct.pack("<I", 115200))
    h.ok(h.plan_fn, PLAN_RELEASE, bytes([1]) + struct.pack("<H", UART))
    h.ok(h.plan_fn, PLAN_APPLY, assign((UART, 2, 21)))                          # TX only
    h.ok(UART, 0x10, struct.pack("<I", 115200))
    ep.uart_rx(UART, b"lost")
    r = h.raw(UART, 0x11, struct.pack("<BQH", 1, 0, 9), session=False)
    assert r.succeeded and r.payload == struct.pack("<QBH", 4, 0, 0)            # nothing received, the position kept
    assert h.ok(UART, 0x15, struct.pack("<H", 2) + b"tx") == struct.pack("<H", 2)
    ep2, h2 = bench()                                                           # kept bytes read while not configured
    h2.ok(h2.plan_fn, PLAN_APPLY, assign((UART, 1, 20), (UART, 2, 21)))
    h2.ok(UART, 0x10, struct.pack("<I", 115200))
    ep2.uart_rx(UART, b"kept")
    ep2.uart_fail_configure.add(UART)
    h2.raw(UART, 0x10, struct.pack("<I", 9600))
    ep2.uart_rx(UART, b"lost")
    r = h2.raw(UART, 0x11, struct.pack("<BQH", 1, 0, 9), session=False)
    assert r.payload == struct.pack("<QBH", 0, 0, 4) + b"kept"
    assert cause(h2.raw(UART, 0x15, struct.pack("<H", 1) + b"x")) == 6


def _at_the_bound(fn, length):
    probe = virtual_bench.esp32_v003_64()
    old = next(o for o in probe.offered if o.fn == fn)
    return replaced(probe, fn, [t if t[0] != catalog.MAX_LENGTH else catalog.u16(catalog.MAX_LENGTH, length)
                                for t in old.tlvs])


def test_i2c_read_rx_timestamp_only_when_it_fits():
    """fixture §3: max_length keeps preload_tx and read_rx's answer within max_frame; the optional timestamp TLV is put
    on read_rx only when it fits - a frame of max_length (= max_frame - 12) comes back whole, without it."""
    ep, h = bench(_at_the_bound(V003_I2C, 64 - 12))
    ep.now_ns = lambda: 99
    h.ok(h.plan_fn, PLAN_APPLY, assign((V003_I2C, 1, 25), (V003_I2C, 2, 26)))
    h.ok(V003_I2C, 0x10, b"\x42")
    assert h.ok(V003_I2C, 0x12, struct.pack("<H", 52) + bytes(52)) == b""        # 10 + 2 + 52 = 64
    ep.i2c_write(V003_I2C, b"\x01")
    ep.i2c_write(V003_I2C, bytes(range(52)))
    assert h.ok(V003_I2C, 0x11) == struct.pack("<BH", 1, 1) + b"\x01" + m.tlv(0x01, struct.pack("<Q", 99))
    r = h.raw(V003_I2C, 0x11)
    assert r.succeeded and r.payload == struct.pack("<BH", 0, 52) + bytes(range(52))
    assert 5 + len(r.payload) <= 64


def test_spi_read_rx_timestamp_only_when_it_fits():
    """fixture §4: max_length keeps the longest arm and read_rx's answer within max_frame; the optional timestamp TLV is
    put on read_rx only when it fits - a transfer of max_length (= max_frame - 14) comes back whole, without it."""
    ep, h = bench(_at_the_bound(V003_SPI, 64 - 14))
    ep.now_ns = lambda: 99
    h.ok(h.plan_fn, PLAN_APPLY, assign((V003_SPI, 1, 18), (V003_SPI, 2, 19), (V003_SPI, 3, 5), (V003_SPI, 4, 4)))
    h.ok(V003_SPI, 0x10, b"\x00\x00")
    h.ok(V003_SPI, 0x11, struct.pack("<HH", 50, 50) + bytes(50))                 # 10 + 2 + 2 + 50 = 64
    ep.spi_transfer(V003_SPI, bytes(range(50)))
    r = h.raw(V003_SPI, 0x12)
    assert r.succeeded and r.payload == struct.pack("<BIH", 0, 400, 50) + bytes(range(50))
    assert 5 + len(r.payload) <= 64
    h.ok(V003_SPI, 0x11, struct.pack("<HH", 1, 0))
    ep.spi_transfer(V003_SPI, b"\x07")
    assert h.ok(V003_SPI, 0x12) == struct.pack("<BIH", 0, 8, 1) + b"\x07" + m.tlv(0x01, struct.pack("<Q", 99))
