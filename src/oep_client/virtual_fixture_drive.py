"""The fixtures that drive a target, in the virtual bench (oep-spec interfaces/oep-if-fixture.ja.md §5-§9):
oep.fixture.i2c-controller, spi-controller, pwm, counter and dac. A mixin of endpoint.Endpoint: the ops, the plan's rules
for these fns and what they do to their pins. What sits on the far side is the test's (TEST HOOKs): the I2C targets on a
controller's bus (`i2c_devices`), the SPI device behind a controller (`spi_device`), the signals a counter's channels see
(`counter_signals`, besides the PWM outputs on the same channel)."""
from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field
from typing import Callable

from . import catalog, message as m, registry as reg
from .virtual_bench import MAX_OP_MS

I2CC, SPIC, PWM, CNT, DAC = (reg.FIXTURE_I2C_CONTROLLER, reg.FIXTURE_SPI_CONTROLLER, reg.FIXTURE_PWM,
                             reg.FIXTURE_COUNTER, reg.FIXTURE_DAC)
DRIVE = {I2CC.name: "i2cc_op", SPIC.name: "spic_op", PWM.name: "pwm_op", CNT.name: "counter_op", DAC.name: "dac_op"}
EXACT_ROLES = {I2CC.name: set(I2CC.enum["role"].values()), SPIC.name: set(SPIC.enum["role"].values())}
BASE_HZ = 80_000_000          # the clock the virtual timers divide (the probe's own, not declared)
I2C_RESULT, I2C_DIR, I2C_STOP = I2CC.enum["result"], I2CC.enum["dir"], I2CC.enum["transfer_flags"]["stop"]
DUTY_FULL = 0xFFFF            # duty 65535: always high (fixture §7)
EDGES_A_PERIOD = {CNT.enum["edge"]["rising"]: 1, CNT.enum["edge"]["falling"]: 1, CNT.enum["edge"]["both"]: 2}
U32_MAX = 0xFFFFFFFF


@dataclass
class I2cDevice:
    """TEST HOOK: an I2C target on a controller's bus - a register pointer and memory (a write's first byte moves the
    pointer, the rest is stored; a read goes on from the pointer). nack_at: the data byte (from 0) it NACKs in a write;
    stretch: it holds SCL low for good; collide: another controller wins the arbitration on it."""
    memory: bytearray = field(default_factory=lambda: bytearray(256))
    pointer: int = 0
    nack_at: int | None = None
    stretch: bool = False
    collide: bool = False

    def write(self, data: bytes) -> int | None:
        """-> the index of the byte NACKed, or None."""
        for i, b in enumerate(data):
            if self.nack_at is not None and i == self.nack_at:
                return i
            if i == 0:
                self.pointer = b
            else:
                self.memory[self.pointer] = b
                self.pointer = (self.pointer + 1) & 0xFF
        return None

    def read(self, n: int) -> bytes:
        out = bytes(self.memory[(self.pointer + i) & 0xFF] for i in range(n))
        self.pointer = (self.pointer + n) & 0xFF
        return out


@dataclass
class I2cControllerState:
    state: int = 0
    clock_hz: int = 0
    stretch_us: int = 0
    held: bool = False
    transfers: int = 0
    errors: int = 0


@dataclass
class SpiControllerState:
    state: int = 0
    mode: int = 0
    bit_order: int = 0
    clock_hz: int = 0
    transfers: int = 0


def reverse_bits(b: int) -> int:
    return int(f"{b:08b}"[::-1], 2)


def inverting_device(mode: int, mosi: bytes) -> bytes:
    """The SPI device behind a controller by default: answers each byte it shifted in, inverted (MSB-first wire order)."""
    return bytes(b ^ 0xFF for b in mosi)


def divided(hz: int, base: int = BASE_HZ) -> int:
    """The nearest frequency at or below `hz` an integer divider of the base clock makes."""
    return base // math.ceil(base / hz)


class FixtureDrive:
    """Mixed into endpoint.Endpoint (which supplies names, decl, plan and the Take / Reject helpers)."""

    def _drive_init(self) -> None:
        self.i2cc: dict[int, I2cControllerState] = {}
        self.spic: dict[int, SpiControllerState] = {}
        self.pwm_out: dict[int, dict[int, tuple[int, int, int]]] = {}   # fn -> role -> (num, den, duty) after a set
        self.dac_out: dict[int, dict[int, int]] = {}                    # fn -> role -> value after a set (on)
        self.i2c_devices: dict[int, dict[int, I2cDevice]] = {}          # TEST HOOK: fn -> address -> target
        self.spi_device: dict[int, Callable[[int, bytes], bytes]] = {}  # TEST HOOK: fn -> device(mode, mosi) -> miso
        self.counter_signals: dict[int, float] = {}                     # TEST HOOK: channel -> Hz from outside
        self.drive_fail_configure: set[int] = set()                     # TEST HOOK: the next configure fails to come up
        for fn, name in self.names.items():
            if name == I2CC.name:
                self.i2cc[fn] = I2cControllerState()
                self.i2c_devices[fn] = {0x50: I2cDevice()}
            elif name == SPIC.name:
                self.spic[fn] = SpiControllerState()
                self.spi_device[fn] = inverting_device
            elif name == PWM.name:
                self.pwm_out[fn] = {}
            elif name == DAC.name:
                self.dac_out[fn] = {}

    # ---- declarations --------------------------------------------------------------------------
    def _decl_u(self, fn: int, tag: int, fmt: str, default: int = 0) -> int:
        return next((struct.unpack("<" + fmt, v)[0] for t, v in self.decl[fn] if t == tag), default)

    def _drive_channels(self, fn: int) -> int:
        """pwm / counter / dac: the outputs or inputs it has (describe channels, tag 0x40)."""
        own = {PWM.name: PWM, CNT.name: CNT, DAC.name: DAC}[self.names[fn]]
        return self._decl_u(fn, own.tlv["describe"]["channels"], "B")

    def _drive_roles(self, fn: int) -> set[int] | None:
        """The plan roles fn takes, or None when fn is not one of these fixtures."""
        name = self.names.get(fn)
        if name in EXACT_ROLES:
            return EXACT_ROLES[name]
        if name in (PWM.name, CNT.name, DAC.name):
            return set(range(self._drive_channels(fn)))
        return None

    def _planned_roles(self, fn: int) -> dict[int, int]:
        return {role: ch for f, role, ch in self.plan if f == fn}

    # ---- the plan's rules for these fns (fixture §5-§9) ----------------------------------------
    def _check_drive_roles(self, got, tag_of=None) -> None:
        """i2c / spi controller: each role exactly once on distinct channels (§5, §6); pwm / dac: each role at most once,
        on distinct channels (§7, §9); counter: each role at most once (§8: it listens, channels may repeat). Else
        unsupported with the role_assignment's tag as received."""
        from .endpoint import ROLE_ASSIGNMENT, Reject
        for fn in sorted({f for f, _, _ in got}):
            name = self.names.get(fn)
            mine = [(role, ch) for f, role, ch in got if f == fn]
            roles = [r for r, _ in mine]
            if name in EXACT_ROLES:
                bad = sorted(roles) != sorted(EXACT_ROLES[name]) or len({c for _, c in mine}) != len(mine)
            elif name in (PWM.name, DAC.name):
                bad = len(set(roles)) != len(roles) or len({c for _, c in mine}) != len(mine)
            elif name == CNT.name:
                bad = len(set(roles)) != len(roles)
            else:
                continue
            if bad:
                raise Reject(m.UNSUPPORTED, bytes([tag_of(fn) if tag_of else ROLE_ASSIGNMENT]))

    def _drive_running(self, fn: int) -> bool:
        """A controller in state 1: its set of assignments may not change (cause 6, §5, §6)."""
        return (fn in self.i2cc and self.i2cc[fn].state == 1) or (fn in self.spic and self.spic[fn].state == 1)

    def _drive_plan_changed(self, fn: int) -> None:
        """fn's plan moved or went: back to the start - a controller to state 0 (holding the bus: a STOP first; the
        counts gone), a pwm's outputs stopped, a dac's off (§5-§9)."""
        if fn in self.i2cc:
            self.i2cc[fn] = I2cControllerState()
        if fn in self.spic:
            self.spic[fn] = SpiControllerState()
        if fn in self.pwm_out:
            self.pwm_out[fn] = {}
        if fn in self.dac_out:
            self.dac_out[fn] = {}

    def _drive_pin_state(self, fn: int, role: int, idle: str) -> str | None:
        """What fn does to its planned pin now, or None when fn is not one of these fixtures."""
        if fn in self.i2cc:
            if self.i2cc[fn].state == 0:
                return idle
            pullups = self._decl_u(fn, catalog.FEATURES, "I") & I2CC.enum["features"]["internal_pullups"]
            return "open-drain pull-up" if pullups else "open-drain"
        if fn in self.spic:
            if self.spic[fn].state == 0:
                return idle
            return "input" if role == SPIC.enum["role"]["miso"] else "output"
        if fn in self.pwm_out:
            out = self.pwm_out[fn].get(role)
            if out is None:
                return idle                                       # kept as it was until the first set (§7)
            return "output low" if out[0] == 0 else "pwm"
        if fn in self.dac_out:
            return "analog" if role in self.dac_out[fn] else idle  # not driven before the first set (§9)
        return None

    def _signal_hz(self, ch: int) -> float:
        """What a counter sees on channel ch: a PWM output there (it listens, §8), else the test's signal."""
        for fn, outs in self.pwm_out.items():
            for role, (num, den, duty) in outs.items():
                if self._planned_roles(fn).get(role) == ch and num and 0 < duty < DUTY_FULL:
                    return num / den
        return self.counter_signals.get(ch, 0.0)

    # ---- oep.fixture.i2c-controller (§5) -------------------------------------------------------
    def _i2cc_op(self, fn: int, op: int, t) -> tuple[int, int, bytes]:
        from .endpoint import Reject, unavailable, unsupported_fixed
        st, ops = self.i2cc[fn], I2CC.op
        max_length = self._decl_u(fn, catalog.MAX_LENGTH, "H")
        if op == ops["configure"]:                                 # clock_hz(u32) stretch_timeout_us(u32) [TLV]
            clock, stretch = t.take("II")
            t.tail()
            if clock == 0 or stretch == 0:
                raise Reject(m.MALFORMED)
            low, high = self._decl_u(fn, catalog.MIN_CLOCK_HZ, "I", 1), self._decl_u(fn, catalog.MAX_CLOCK_HZ, "I")
            if not low <= clock <= high or stretch > self._decl_u(fn, I2CC.tlv["describe"]["max_stretch_timeout_us"], "I"):
                raise unsupported_fixed()
            if not self._planned_roles(fn):
                raise unavailable("wrong_state")
            if fn in self.drive_fail_configure:
                self.drive_fail_configure.discard(fn)
                self.i2cc[fn] = I2cControllerState()
                return self._answer(b"", m.FAILED)
            self.i2cc[fn] = I2cControllerState(state=1, clock_hz=divided(clock), stretch_us=stretch)   # a STOP first
            return self._answer(struct.pack("<I", self.i2cc[fn].clock_hz))
        if op == ops["transfer"]:                                  # flags(u8) n(u8) n x segment [TLV]
            flags, n = t.take("BB")
            segments = []
            for _ in range(n):
                direction, address, count = t.take("BBH")
                data = t.bytes(count) if direction == I2C_DIR["write"] else b""
                segments.append((direction, address, count, data))
            t.tail()
            if (flags & ~I2C_STOP or (n == 0 and not flags & I2C_STOP)
                    or any(d > I2C_DIR["read"] or a > 0x7F or (d == I2C_DIR["read"] and c == 0) for d, a, c, _ in segments)):
                raise Reject(m.MALFORMED)
            total = sum(c for _, _, c, _ in segments)
            if total > max_length:
                raise unsupported_fixed()
            if st.state == 0:
                raise unavailable("wrong_state")
            if (total + n) * 9 * 1000 // st.clock_hz > MAX_OP_MS:   # past max_op_ms: like max_length
                raise unsupported_fixed()
            if n == 0:                                             # a STOP alone (nothing when the bus is not held)
                st.held = False
                return self._answer(struct.pack("<BBHH", I2C_RESULT["done"], 0, 0, 0))
            result, at, index, read = I2C_RESULT["done"], 0, 0, b""
            for k, (direction, address, count, data) in enumerate(segments):
                device = self.i2c_devices[fn].get(address)
                if device is None:
                    result, at = I2C_RESULT["address_nack"], k
                elif device.collide:
                    result, at = I2C_RESULT["bus_error"], k
                elif device.stretch:
                    result, at = I2C_RESULT["stretch_timeout"], k
                elif direction == I2C_DIR["write"]:
                    nacked = device.write(data)
                    if nacked is not None:
                        result, at, index = I2C_RESULT["data_nack"], k, nacked
                else:
                    read += device.read(count)
                if result != I2C_RESULT["done"]:
                    break                                          # 1, 2, 4: a STOP; 3: the lines let go (held 0)
            st.held = result == I2C_RESULT["done"] and not flags & I2C_STOP
            st.transfers = min(st.transfers + 1, U32_MAX)
            st.errors = min(st.errors + (result != I2C_RESULT["done"]), U32_MAX)
            return self._answer(struct.pack("<BBHH", result, at, index, len(read)) + read)
        if op == ops["status"]:
            t.tail()
            return self._answer(struct.pack("<BBII", st.state, int(st.held), st.transfers, st.errors))
        return m.REJECTED, m.UNKNOWN_OPERATION, b""

    # ---- oep.fixture.spi-controller (§6) -------------------------------------------------------
    def _spic_op(self, fn: int, op: int, t) -> tuple[int, int, bytes]:
        from .endpoint import Reject, unavailable, unsupported_fixed
        st, ops, own = self.spic[fn], SPIC.op, SPIC.tlv["describe"]
        if op == ops["configure"]:                                 # mode(u8) bit_order(u8) clock_hz(u32) [TLV]
            mode, order, clock = t.take("BBI")
            t.tail()
            if mode > 3 or order > 1 or clock == 0:
                raise Reject(m.MALFORMED)
            low, high = self._decl_u(fn, catalog.MIN_CLOCK_HZ, "I", 1), self._decl_u(fn, catalog.MAX_CLOCK_HZ, "I")
            lsb = self._decl_u(fn, catalog.FEATURES, "I") & SPIC.enum["features"]["lsb_first"]
            if (not self._decl_u(fn, own["modes"], "B") >> mode & 1 or (order and not lsb)
                    or not low <= clock <= high):
                raise unsupported_fixed()
            if not self._planned_roles(fn):
                raise unavailable("wrong_state")
            if fn in self.drive_fail_configure:
                self.drive_fail_configure.discard(fn)
                self.spic[fn] = SpiControllerState()
                return self._answer(b"", m.FAILED)
            self.spic[fn] = SpiControllerState(state=1, mode=mode, bit_order=order, clock_hz=divided(clock))
            return self._answer(struct.pack("<I", self.spic[fn].clock_hz))
        if op == ops["transfer"]:                                  # n(u8) n x (gap setup hold count tx) [TLV]
            n = t.take("B")
            items = []
            for _ in range(n):
                gap, setup, hold, count = t.take("IIIH")
                items.append((gap, setup, hold, t.bytes(count)))
            t.tail()
            if n == 0:
                raise Reject(m.MALFORMED)
            step, max_delay = self._decl_u(fn, own["time_step_ns"], "I", 1), self._decl_u(fn, own["max_delay_ns"], "I")
            total = sum(len(tx) for *_, tx in items)
            if total > self._decl_u(fn, catalog.MAX_LENGTH, "H") or any(max(g, s, h) > max_delay for g, s, h, _ in items):
                raise unsupported_fixed()
            if st.state == 0:
                raise unavailable("wrong_state")
            nanos = sum(g + s + h for g, s, h, _ in items) + total * 8 * 1_000_000_000 // st.clock_hz
            if nanos // 1_000_000 > MAX_OP_MS:
                raise unsupported_fixed()
            up = lambda ns: -(-ns // step) * step                  # noqa: E731  the nearest step at or above
            out = b""
            for k, (gap, setup, hold, tx) in enumerate(items):
                wire = bytes(reverse_bits(b) for b in tx) if st.bit_order else tx      # the order on MOSI (§4)
                miso = self.spi_device[fn](st.mode, wire)[:len(tx)] if tx else b""
                rx = bytes(reverse_bits(b) for b in miso) if st.bit_order else miso
                out += struct.pack("<IIIH", 0 if k == 0 else up(gap), up(setup), up(hold), len(rx)) + rx
            st.transfers = min(st.transfers + n, U32_MAX)
            return self._answer(out)
        if op == ops["status"]:
            t.tail()
            return self._answer(struct.pack("<BBBI", st.state, st.mode, st.bit_order, st.transfers))
        return m.REJECTED, m.UNKNOWN_OPERATION, b""

    # ---- oep.fixture.pwm (§7) ------------------------------------------------------------------
    def _pwm_op(self, fn: int, op: int, t) -> tuple[int, int, bytes]:
        from .endpoint import Reject, unavailable, unsupported_fixed
        outs, ops = self.pwm_out[fn], PWM.op
        planned = self._planned_roles(fn)
        if op == ops["set"]:                                       # n(u8) n x (role(u8) hz(u32) duty(u16)) [TLV]
            n = t.take("B")
            rows = [t.take("BIH") for _ in range(n)]
            t.tail()
            if n == 0:
                raise Reject(m.MALFORMED)
            low, high = self._decl_u(fn, catalog.MIN_CLOCK_HZ, "I", 1), self._decl_u(fn, catalog.MAX_CLOCK_HZ, "I")
            roles = [r for r, _, _ in rows]
            if (any(r >= self._drive_channels(fn) for r in roles) or len(set(roles)) != len(roles)
                    or any(hz and not low <= hz <= high for _, hz, _ in rows)):
                raise unsupported_fixed()
            if any(r not in planned for r in roles):
                raise unavailable("wrong_state")
            out = b""
            for role, hz, duty in rows:
                if hz == 0:
                    made = (0, 1, 0)                               # stopped: the line driven low
                else:
                    div = max(1, round(BASE_HZ / hz))
                    g = math.gcd(BASE_HZ, div)
                    steps = min(div, 1 << 16)                      # the duty's resolution at this frequency
                    made_duty = duty if duty in (0, DUTY_FULL) else round(round(duty * steps / DUTY_FULL) * DUTY_FULL / steps)
                    made = (BASE_HZ // g, div // g, made_duty)
                outs[role] = made
                out += struct.pack("<BIIH", role, *made)
            return self._answer(out)
        if op == ops["status"]:
            t.tail()
            rows = [(r, *outs.get(r, (0, 1, 0))) for r in sorted(planned)]
            return self._answer(bytes([len(rows)]) + b"".join(struct.pack("<BIIH", *r) for r in rows))
        return m.REJECTED, m.UNKNOWN_OPERATION, b""

    # ---- oep.fixture.counter (§8) --------------------------------------------------------------
    def _counter_op(self, fn: int, op: int, t) -> tuple[int, int, bytes]:
        from .endpoint import Reject, unavailable, unsupported_fixed
        if op == CNT.op["measure"]:                                # gate_us(u32) edge(u8) [TLV]
            gate_us, edge = t.take("IB")
            t.tail()
            if gate_us == 0 or edge not in EDGES_A_PERIOD:
                raise Reject(m.MALFORMED)
            if gate_us > self._decl_u(fn, CNT.tlv["describe"]["max_gate_us"], "I"):
                raise unsupported_fixed()
            planned = self._planned_roles(fn)
            if not planned:
                raise unavailable("wrong_state")
            window_ns = gate_us * 1000
            limit = self._decl_u(fn, catalog.MAX_CLOCK_HZ, "I")
            out = b""
            for role in sorted(planned):
                hz = min(self._signal_hz(planned[role]), limit)    # faster edges may be missed (§8)
                count = min(int(hz * window_ns / 1e9) * EDGES_A_PERIOD[edge], U32_MAX)
                out += struct.pack("<BI", role, count)
            return self._answer(struct.pack("<QB", window_ns, len(planned)) + out)
        return m.REJECTED, m.UNKNOWN_OPERATION, b""

    # ---- oep.fixture.dac (§9) ------------------------------------------------------------------
    def _dac_op(self, fn: int, op: int, t) -> tuple[int, int, bytes]:
        from .endpoint import Reject, unavailable, unsupported_fixed
        outs, ops, own = self.dac_out[fn], DAC.op, DAC.tlv["describe"]
        planned = self._planned_roles(fn)
        if op == ops["set"]:                                       # n(u8) n x (role(u8) value(u32)) [TLV]
            n = t.take("B")
            rows = [t.take("BI") for _ in range(n)]
            t.tail()
            if n == 0:
                raise Reject(m.MALFORMED)
            full = (1 << self._decl_u(fn, own["bits"], "B")) - 1
            if any(r >= self._drive_channels(fn) or v > full for r, v in rows):
                raise unsupported_fixed()
            if any(r not in planned for r, _ in rows):
                raise unavailable("wrong_state")
            for role, value in rows:
                outs[role] = value
            return self._answer(b"")
        if op == ops["status"]:
            t.tail()
            rows = [(r, outs.get(r, 0), int(r in outs)) for r in sorted(planned)]
            return self._answer(bytes([len(rows)]) + b"".join(struct.pack("<BIB", *r) for r in rows))
        return m.REJECTED, m.UNKNOWN_OPERATION, b""
