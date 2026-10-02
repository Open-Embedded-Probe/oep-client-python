"""oep.fixture.gpio, oep.fixture.uart, oep.fixture.i2c-target and oep.fixture.spi-target, revision 1 (oep-spec
oep-if-fixture). The capture is in `capture` (oep.fixture.logic / analog).

Plan roles: gpio 1 = line; uart 1 = RX, 2 = TX; i2c-target 1 = SDA, 2 = SCL; spi-target 1 = SCK, 2 = MOSI, 3 = MISO, 4 = CS.
Only channels the plan assigned can be used.
"""

from __future__ import annotations

import struct
import time
from dataclasses import dataclass

from . import catalog, host as h, message as m, registry as reg
from .console import PositionStream, StreamIO
from .core import Interface, describe

_GPIO, _UART, _I2C, _SPI = reg.FIXTURE_GPIO, reg.FIXTURE_UART, reg.FIXTURE_I2C_TARGET, reg.FIXTURE_SPI_TARGET
_MODE = _GPIO.enum["mode"]


class Gpio(Interface):
    """oep.fixture.gpio. `set` applies (channel, mode) pairs in order in one request (pull NRST, then release it); a
    channel the plan did not assign or a mode the probe lacks rejects the whole list as unavailable (host.Unavailable:
    .channels, and its position as TLV 0x40). The open-drain modes never drive a line high: the way to move a target's reset line."""
    NAME = "oep.fixture.gpio"
    REVISION = 1
    SET, READ = _GPIO.op["set"], _GPIO.op["read"]
    INPUT, INPUT_PULLUP, INPUT_PULLDOWN = _MODE["input"], _MODE["input_pullup"], _MODE["input_pulldown"]
    OUTPUT_LOW, OUTPUT_HIGH = _MODE["output_low"], _MODE["output_high"]
    OPEN_DRAIN_LOW, OPEN_DRAIN_RELEASE = _MODE["open_drain_low"], _MODE["open_drain_release"]
    INPUT_PULLUP_PULLDOWN = _MODE["input_pullup_pulldown"]   # both pulls: a weak mid level

    def __init__(self, hst: h.Host, fn: int | None = None, name: str | None = None):
        super().__init__(hst, name, fn=fn)

    @staticmethod
    def set_body(pairs: list[tuple[int, int]]) -> bytes:
        return struct.pack("<B", len(pairs)) + b"".join(struct.pack("<HB", ch, mode) for ch, mode in pairs)

    def set(self, pairs: list[tuple[int, int]]) -> None:
        """[(channel, mode)], applied in order."""
        self._call(self.SET, self.set_body(pairs))

    def configure(self, channel: int, mode: int) -> None:
        self.set([(channel, mode)])

    def read(self, channels: list[int]) -> list[int]:
        """-> one level (0 / 1) per channel. Lock-free. The answer is n(u8) n x level [TLV] (fixture §1)."""
        rd = m.Reader(self._call(self.READ, struct.pack("<B", len(channels))
                                 + b"".join(struct.pack("<H", c) for c in channels), locked=False).payload)
        levels = list(rd.counted("B"))
        rd.tail()
        return levels

    def pull_low(self, channel: int) -> None:
        self.set([(channel, self.OPEN_DRAIN_LOW)])

    def release(self, channel: int) -> None:
        self.set([(channel, self.OPEN_DRAIN_RELEASE)])

    def request_release(self, channel: int) -> tuple[int, int, bytes]:
        """The release as a raw request, to pipeline with whatever must follow it at once."""
        return self.request(self.SET, self.set_body([(channel, self.OPEN_DRAIN_RELEASE)]))

    def pulse_low(self, channel: int, low_s: float = 0.02) -> None:
        """Open-drain low, then released to Hi-Z (never driven high)."""
        self.pull_low(channel)
        try:
            time.sleep(low_s)
        finally:
            self.release(channel)


UART_CONFIGURED = {v: k for k, v in _UART.enum["uart_configured"].items()}   # default, session, item, item_fallback


@dataclass(frozen=True, kw_only=True)
class UartStatus:
    """oep.fixture.uart status (op 0x07, lock-free): what is in force - "default" (115200 8N1, nothing set),
    "session" (a configure), "item" (the settings' uart item), "item_fallback" (the item's baud could not be made when
    the plan ran the UART: the default applies) - and the baud / format it runs with."""
    configured: str
    baud: int
    format: int

    @property
    def is_default(self) -> bool:
        return self.configured in ("default", "item_fallback")


class FixtureUart(PositionStream):
    """oep.fixture.uart: one position stream per fn, like the console without a stream byte. The stream exists while
    the plan gives the fn RX or TX; received bytes are kept from then on, whatever the session, and the position never
    goes back within one boot (a plan released and applied again carries on). Reads are lock-free and do not consume.
    TX idles high while planned, before configure too."""
    NAME = "oep.fixture.uart"
    REVISION = 1
    CONFIGURE, STATUS = _UART.op["configure"], _UART.op["status"]
    TAG_FORMAT = _UART.tlv["configure"]["format"]
    # format bits: data bits (0 = 8, 1 = 7), parity (0 none, 1 even, 2 odd) << 2, stop bits (0 = 1, 1 = 2) << 4
    EIGHT_N_1 = 0x00

    def __init__(self, hst: h.Host, fn: int | None = None, name: str | None = None):
        super().__init__(hst, name, fn=fn)

    @staticmethod
    def format_byte(data_bits: int = 8, parity: str = "N", stop_bits: int = 1) -> int:
        return ({8: 0, 7: 1}[data_bits] | {"N": 0, "E": 1, "O": 2}[parity.upper()] << 2 | {1: 0, 2: 1}[stop_bits] << 4)

    def configure(self, baud: int, fmt: int | None = None) -> int:
        """-> the actual baud (within 5 % of the one asked, else the probe refuses unsupported). fmt (format_byte())
        goes as a critical TLV: a probe that cannot set it refuses rather than running 8N1; None leaves the default
        8N1. An fn whose plan has neither RX nor TX is rejected unavailable (cause 6)."""
        body = struct.pack("<I", baud)
        if fmt is not None:
            body += m.tlv(self.TAG_FORMAT, bytes([fmt]), critical=True)
        rd = m.Reader(self._call(self.CONFIGURE, body).payload)
        actual = rd.u32()
        rd.tail()
        return actual

    def status(self) -> UartStatus:
        """configured, baud, format as the UART runs now (lock-free)."""
        rd = m.Reader(self._call(self.STATUS, locked=False).payload)
        configured, baud, fmt = rd.take("BIB")
        rd.tail()
        return UartStatus(configured=UART_CONFIGURED.get(configured, str(configured)), baud=baud, format=fmt)


class FixtureUartIO(StreamIO):
    """oep.fixture.uart as a plain byte stream, after a plan gave it RX / TX: configure() starts reading from the
    stream's position at that moment (earlier bytes are skipped); write() splits and waits for the UART."""
    MAX_READ, MAX_WRITE = 480, 256            # fit the smallest probe frame in use (V003: 512 bytes)

    def __init__(self, hst: h.Host, fn: int | None = None, name: str | None = None):
        self.uart = FixtureUart(hst, fn, name)
        self.source, self.position, self.lost = self.uart, None, 0
        self.baud = 0

    def configure(self, baud: int, fmt: int | None = None) -> int:
        self.baud = self.uart.configure(baud, fmt)
        self.position = self.uart.read(PositionStream.FROM_NOW, 0, 0).start
        return self.baud

    def read(self, n: int = 512) -> bytes:
        if self.position is None:                  # not configured here: read from the oldest byte kept
            self.position = self.uart.read(PositionStream.FROM_OLDEST, 0, 0).start
        return super().read(n)


@dataclass(frozen=True, kw_only=True)
class I2cStatus:
    state: int          # 0 not configured, 1 running
    mode: int           # the configure mode
    armed: bool         # mode 1: waiting for a write
    queued: int         # frames waiting for read_rx
    rx_frames: int
    tx_slots: int
    errors: int


class _TargetDeclarations:
    """The describe declarations of a fixture target (oep-if-fixture §3 / §4), read once per instance (describe is
    cached on the host): max_length, max_clock_hz, queue_depth (None when the probe does not declare them) and
    features (0 when not declared)."""
    host: h.Host
    fn: int
    _decl: dict[int, bytes] | None = None

    def _declared(self, tag: int, fmt: str) -> int | None:
        if self._decl is None:
            self._decl = {}
            for t, value in describe(self.host, self.fn):
                self._decl.setdefault(t & ~catalog.CRITICAL, value)
        value = self._decl.get(tag)
        return struct.unpack_from("<" + fmt, value)[0] if value is not None and len(value) >= struct.calcsize(fmt) else None

    @property
    def max_length(self) -> int | None:
        """Bytes one frame / transfer may hold (describe max_length)."""
        return self._declared(catalog.MAX_LENGTH, "H")

    @property
    def max_clock_hz(self) -> int | None:
        """The verified upper limit of the bus clock (describe max_clock_hz)."""
        return self._declared(catalog.MAX_CLOCK_HZ, "I")

    @property
    def features(self) -> int:
        """The describe features bits (0 when not declared)."""
        return self._declared(catalog.FEATURES, "I") or 0

    @property
    def queue_depth(self) -> int | None:
        """Frames / transfers the queue holds (describe tag 0x40, u8; i2c mode 3: also the most unread preload slots)."""
        return self._declared(self.TAG_QUEUE_DEPTH, "B")


class I2cTarget(_TargetDeclarations, Interface):
    """oep.fixture.i2c-target (oep-if-fixture §3): the probe as an I2C target. Mode 1 fixed rx (arm_rx with the exact
    length), 2 framed rx (a 1-byte length write, then the payload), 3 preloaded tx (slots the controller reads)."""
    NAME = _I2C.name
    REVISION = _I2C.revision
    CONFIGURE, ARM_RX, READ_RX, PRELOAD_TX, STATUS, RESET, STRETCH = (
        _I2C.op[k] for k in ("configure", "arm_rx", "read_rx", "preload_tx", "status", "reset", "stretch"))
    MODE_FIXED_RX, MODE_FRAMED_RX, MODE_PRELOADED_TX = (_I2C.enum["mode"][k] for k in ("fixed_rx", "framed_rx", "preloaded_tx"))
    ROLE_SDA, ROLE_SCL = _I2C.enum["role"]["sda"], _I2C.enum["role"]["scl"]
    FEATURE_PRELOADED_TX, FEATURE_STRETCH = _I2C.enum["features"]["preloaded_tx"], _I2C.enum["features"]["stretch"]
    TAG_QUEUE_DEPTH, TAG_MAX_STRETCH_US = _I2C.tlv["describe"]["queue_depth"], _I2C.tlv["describe"]["max_stretch_us"]

    def __init__(self, hst: h.Host, fn: int | None = None, name: str | None = None):
        super().__init__(hst, name, fn=fn)
        self.last_ns: int | None = None

    def assignments(self, sda: int, scl: int) -> list[tuple[int, int, int]]:
        return [(self.fn, self.ROLE_SDA, sda), (self.fn, self.ROLE_SCL, scl)]

    def configure(self, address: int, mode: int) -> None:
        self._call(self.CONFIGURE, struct.pack("<BB", address, mode))

    def arm_rx(self, length: int) -> None:
        self._call(self.ARM_RX, struct.pack("<H", length))

    TAG_NS = _I2C.tlv["read_rx_answer"]["ns"]

    def read_rx(self) -> tuple[int, bytes]:
        """-> (frames still queued after this one, the oldest frame or b""). self.last_ns: when the probe received it
        (its clock, ns) when the probe says (TLV ns), else None."""
        rd = m.Reader(self._call(self.READ_RX).payload)
        pending = rd.u8()
        data = rd.counted("H")
        ns = rd.tail().get(self.TAG_NS)
        self.last_ns = struct.unpack("<Q", ns)[0] if ns is not None and len(ns) == 8 else None
        return pending, data

    def preload_tx(self, data: bytes) -> int:
        """-> the slots preloaded so far (u8, wraps)."""
        rd = m.Reader(self._call(self.PRELOAD_TX, struct.pack("<H", len(data)) + data).payload)
        slots = rd.u8()
        rd.tail()
        return slots

    def status(self) -> I2cStatus:
        rd = m.Reader(self._call(self.STATUS, locked=False).payload)
        state, mode, armed, queued, rx, tx, errors = rd.take("BBBBIBI")
        rd.tail()
        return I2cStatus(state=state, mode=mode, armed=bool(armed), queued=queued, rx_frames=rx, tx_slots=tx, errors=errors)

    def reset(self) -> None:
        self._call(self.RESET)

    @property
    def max_stretch_us(self) -> int | None:
        """The largest stretch_us stretch() accepts (describe tag 0x41, u32); None when the probe does not declare it
        (it does exactly when features has bit1)."""
        return self._declared(self.TAG_MAX_STRETCH_US, "I")

    def stretch(self, stretch_us: int) -> None:
        """Hold SCL low for stretch_us after each received byte (0 = off); probes declaring features bit1 only. Above
        max_stretch_us: host.Unsupported. Accepted in any state; configure and reset keep it, the plan's release
        clears it."""
        self._call(self.STRETCH, struct.pack("<I", stretch_us))


@dataclass(frozen=True, kw_only=True)
class SpiStatus:
    state: int
    mode: int
    bit_order: int
    armed: bool
    queued: int
    transactions: int
    errors: int


class SpiTarget(_TargetDeclarations, Interface):
    """oep.fixture.spi-target (oep-if-fixture §4): one CS-framed transaction at a time - arm() with the MISO bytes,
    then read_rx() after the controller raised CS."""
    NAME = _SPI.name
    REVISION = _SPI.revision
    CONFIGURE, ARM, READ_RX, STATUS, RESET = (_SPI.op[k] for k in ("configure", "arm", "read_rx", "status", "reset"))
    ROLE_SCK, ROLE_MOSI, ROLE_MISO, ROLE_CS = (_SPI.enum["role"][k] for k in ("sck", "mosi", "miso", "cs"))
    MSB_FIRST, LSB_FIRST = 0, 1
    FEATURE_LSB_FIRST = _SPI.enum["features"]["lsb_first"]
    TAG_QUEUE_DEPTH = _SPI.tlv["describe"]["queue_depth"]

    def __init__(self, hst: h.Host, fn: int | None = None, name: str | None = None):
        super().__init__(hst, name, fn=fn)
        self.last_ns: int | None = None

    def assignments(self, sck: int, mosi: int, miso: int, cs: int) -> list[tuple[int, int, int]]:
        return [(self.fn, self.ROLE_SCK, sck), (self.fn, self.ROLE_MOSI, mosi), (self.fn, self.ROLE_MISO, miso),
                (self.fn, self.ROLE_CS, cs)]

    def configure(self, mode: int = 0, bit_order: int = 0) -> None:
        self._call(self.CONFIGURE, struct.pack("<BB", mode, bit_order))

    def arm(self, length: int, tx: bytes = b"") -> None:
        self._call(self.ARM, struct.pack("<HH", length, len(tx)) + tx)

    TAG_NS = _SPI.tlv["read_rx_answer"]["ns"]

    def read_rx(self) -> tuple[int, int, bytes]:
        """-> (transactions still queued, bits clocked, the MOSI bytes) of the oldest finished transaction. self.last_ns:
        when it ended on the probe's clock (TLV ns) when the probe says, else None."""
        rd = m.Reader(self._call(self.READ_RX).payload)
        pending, bits = rd.take("BI")
        data = rd.counted("H")
        ns = rd.tail().get(self.TAG_NS)
        self.last_ns = struct.unpack("<Q", ns)[0] if ns is not None and len(ns) == 8 else None
        return pending, bits, data

    def status(self) -> SpiStatus:
        rd = m.Reader(self._call(self.STATUS, locked=False).payload)
        state, mode, order, armed, queued, n, errors = rd.take("BBBBBII")
        rd.tail()
        return SpiStatus(state=state, mode=mode, bit_order=order, armed=bool(armed), queued=queued, transactions=n,
                         errors=errors)

    def reset(self) -> None:
        self._call(self.RESET)
