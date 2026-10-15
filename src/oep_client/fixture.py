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
    channel the plan did not assign rejects the whole list as unavailable (host.Unavailable: .channels, and its
    position as TLV 0x40); a mode the probe does not declare, unsupported (0x00, then the same channel / index TLVs). The open-drain modes never drive a line high: the way to move a target's reset line.
    The output strength is the probe's own (fixture §1: revision 1 defines no strength selection)."""
    NAME = "oep.fixture.gpio"
    REVISION = 1
    SET, READ = _GPIO.op["set"], _GPIO.op["read"]
    TAG_MODES = _GPIO.tlv["describe"]["modes"]
    INPUT, INPUT_PULLUP, INPUT_PULLDOWN = _MODE["input"], _MODE["input_pullup"], _MODE["input_pulldown"]
    OUTPUT_LOW, OUTPUT_HIGH = _MODE["output_low"], _MODE["output_high"]
    OPEN_DRAIN_LOW, OPEN_DRAIN_RELEASE = _MODE["open_drain_low"], _MODE["open_drain_release"]

    def __init__(self, hst: h.Host, fn: int | None = None, name: str | None = None):
        super().__init__(hst, name, fn=fn)

    @classmethod
    def set_body(cls, pairs: list[tuple[int, int]]) -> bytes:
        """n(u8) n x (channel(u16) mode(u8)) (fixture §1)."""
        return struct.pack("<B", len(pairs)) + b"".join(struct.pack("<HB", ch, mode) for ch, mode in pairs)

    def set(self, pairs: list[tuple[int, int]]) -> None:
        """[(channel, mode)], applied in order (fixture §1)."""
        self._call(self.SET, self.set_body(pairs))

    def modes(self) -> int:
        """describe modes: a u32 bit set, bit n = mode n (0xFF when not declared)."""
        # the first copy, a u32 exactly; bit 7 in an answer is an unknown tag (core §2.2, §2.3)
        v = catalog.first_fixed(describe(self.host, self.fn), self.TAG_MODES, 4)
        return struct.unpack("<I", v)[0] if v is not None else 0xFF

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


@dataclass(frozen=True, kw_only=True)
class UartStatus:
    """oep.fixture.uart status (lock-free): the baud and format the UART runs with now (fixture §2); baud 0 and format 0
    before the first configure and after a failed one (not configured: nothing is received)."""
    baud: int
    format: int

    @property
    def configured(self) -> bool:
        return self.baud != 0


class FixtureUart(PositionStream):
    """oep.fixture.uart: one position stream per fn, like the console without a stream byte (fixture §2). The stream
    exists while the plan gives the fn RX or TX; nothing is received until a configure, and received bytes are kept
    from then on, whatever the session; reconfiguring (it succeeds or fails) keeps them and the position never goes
    back within one boot. Reads are lock-free and do not consume. TX idles high while planned; write needs TX in the
    plan and a configure (else unavailable cause 6). marks, clear, mark and write are optional ops (`offers`)."""
    NAME = "oep.fixture.uart"
    REVISION = 1
    CONFIGURE, STATUS = _UART.op["configure"], _UART.op["status"]
    READ, MARKS, CLEAR, MARK, WRITE = (_UART.op[k] for k in ("read", "marks", "clear", "mark", "write"))
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
        goes as its TLV (critical: this host's choice); a format the probe cannot set is refused unsupported, never run
        as 8N1. None leaves the default 8N1. An fn whose plan has neither RX nor TX is rejected unavailable (cause 6).
        A configure the probe took but could not bring up is completed failed (raised as Failed; its baud is 0): the
        UART is not configured then, received bytes stay readable, and unsent TX is dropped (fixture §2)."""
        body = struct.pack("<I", baud)
        if fmt is not None:
            body += m.tlv(self.TAG_FORMAT, bytes([fmt]), critical=True)
        rd = m.Reader(self._call(self.CONFIGURE, body).payload)
        actual = rd.u32()
        rd.tail()
        return actual

    def status(self) -> UartStatus:
        """baud and format as the UART runs now (lock-free)."""
        rd = m.Reader(self._call(self.STATUS, locked=False).payload)
        baud, fmt = rd.take("IB")
        rd.tail()
        return UartStatus(baud=baud, format=fmt)


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
    queued: int         # frames waiting for read_rx
    rx_frames: int      # frames queued since configure (overflows not counted)
    tx_slots: int       # preload_tx slots not read yet
    errors: int         # overflows and writes past max_length


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
                self._decl.setdefault(t, value)   # the first copy; bit 7 in an answer: an unknown tag (core §2.2, §2.3)
        value = self._decl.get(tag)
        # a value without its defined length is broken and not used (core §2.3)
        return struct.unpack("<" + fmt, value)[0] if value is not None and len(value) == struct.calcsize("<" + fmt) else None

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
        """Frames / transfers the queue holds (describe tag 0x40, u8; i2c-target: also the most unread preload slots)."""
        return self._declared(self.TAG_QUEUE_DEPTH, "B")


class I2cTarget(_TargetDeclarations, Interface):
    """oep.fixture.i2c-target (oep-if-fixture §3): the probe as an I2C target at one address, one form: every
    controller write with data is one frame in the queue (read_rx), every controller read is answered from the
    preload_tx slots in order (0xFF when none is left). stretch (optional, `offers(STRETCH)`) holds SCL after each
    byte."""
    NAME = _I2C.name
    REVISION = _I2C.revision
    CONFIGURE, READ_RX, PRELOAD_TX, STATUS, STRETCH = (
        _I2C.op[k] for k in ("configure", "read_rx", "preload_tx", "status", "stretch"))
    ROLE_SDA, ROLE_SCL = _I2C.enum["role"]["sda"], _I2C.enum["role"]["scl"]
    TAG_QUEUE_DEPTH, TAG_MAX_STRETCH_US = _I2C.tlv["describe"]["queue_depth"], _I2C.tlv["describe"]["max_stretch_us"]
    FEATURE_INTERNAL_PULLUPS = _I2C.enum["features"]["internal_pullups"]

    def __init__(self, hst: h.Host, fn: int | None = None, name: str | None = None):
        super().__init__(hst, name, fn=fn)
        self.last_ns: int | None = None

    def assignments(self, sda: int, scl: int) -> list[tuple[int, int, int]]:
        return [(self.fn, self.ROLE_SDA, sda), (self.fn, self.ROLE_SCL, scl)]

    RESERVED_ADDRESSES = (range(0x00, 0x08), range(0x78, 0x80))   # I2C's own (general call, 10-bit prefix, ...)

    def configure(self, address: int) -> None:
        """Answer at `address` (7 bits), the target made anew (the queue, the slots and the counts emptied; stretch
        kept). 0x00-0x07 and 0x78-0x7F are the I2C specification's reserved addresses, which a probe refuses
        unsupported (fixture §3) - refused here before anything is sent."""
        if any(address in r for r in self.RESERVED_ADDRESSES):
            raise ValueError(f"I2C address 0x{address:02x} is reserved (0x00-0x07, 0x78-0x7F; fixture §3)")
        self._call(self.CONFIGURE, struct.pack("<B", address))

    TAG_NS = _I2C.tlv["read_rx_answer"]["ns"]

    def read_rx(self) -> tuple[int, bytes]:
        """-> (frames still queued after this one, the oldest frame or b""). self.last_ns: the STOP or the next START that
        ended the frame (fixture §3), on the probe's clock, when the probe says - else None. (Was: when the probe received it
        (its clock, ns) when the probe says (TLV ns), else None."""
        rd = m.Reader(self._call(self.READ_RX).payload)
        pending = rd.u8()
        data = rd.counted("H")
        ns = rd.tail().get(self.TAG_NS)
        self.last_ns = struct.unpack("<Q", ns)[0] if ns is not None and len(ns) == 8 else None
        return pending, data

    def preload_tx(self, data: bytes) -> None:
        """One slot the controller's next read is answered from (1 to max_length bytes; at most queue_depth unread:
        then rejected unavailable cause 2)."""
        self._call(self.PRELOAD_TX, struct.pack("<H", len(data)) + data)

    def status(self) -> I2cStatus:
        rd = m.Reader(self._call(self.STATUS, locked=False).payload)
        state, queued, rx, tx, errors = rd.take("BBIBI")
        rd.tail()
        return I2cStatus(state=state, queued=queued, rx_frames=rx, tx_slots=tx, errors=errors)

    @property
    def max_stretch_us(self) -> int | None:
        """The largest stretch_us stretch() accepts (describe tag 0x41, u32); None when the probe does not declare it
        (it does exactly when its ops offer stretch: `offers(STRETCH)`)."""
        return self._declared(self.TAG_MAX_STRETCH_US, "I")

    @property
    def internal_pullups(self) -> bool:
        """Whether the probe enables pull-ups of its own on SDA / SCL while configured (describe features bit2, fixture
        §3); without them the bus needs its own."""
        return bool(self.features & self.FEATURE_INTERNAL_PULLUPS)

    def stretch(self, stretch_us: int) -> None:
        """Hold SCL low for stretch_us after each received byte (0 = off); an optional op, offered when the describe's
        ops set it (`offers(STRETCH)`; otherwise rejected unknown_operation). Above max_stretch_us: host.Unsupported.
        Accepted in any state; configure keeps it, the plan's release clears it."""
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


def wire_bits(data: bytes, bits: int, bit_order: int = 0) -> list[int]:
    """The wire bits of an spi-target transaction in the order they came (fixture §4): wire bit k is in byte k // 8, at
    bit 7 - k % 8 MSB first (bit_order 0) or k % 8 LSB first (1). At most the bits `data` holds (it stops at length)."""
    n = min(bits, 8 * len(data))
    return [(data[k >> 3] >> (k & 7 if bit_order else 7 - (k & 7))) & 1 for k in range(n)]


def pack_wire_bits(seq: list[int], bit_order: int = 0) -> bytes:
    """The inverse of wire_bits: the bytes a probe answers for these wire bits (a partial last byte's missing bits 0)."""
    out = bytearray((len(seq) + 7) // 8)
    for k, b in enumerate(seq):
        if b:
            out[k >> 3] |= 1 << (k & 7 if bit_order else 7 - (k & 7))
    return bytes(out)


class SpiTarget(_TargetDeclarations, Interface):
    """oep.fixture.spi-target (oep-if-fixture §4): one CS-framed transaction at a time - arm() with the MISO bytes,
    then read_rx() after the controller raised CS."""
    NAME = _SPI.name
    REVISION = _SPI.revision
    CONFIGURE, ARM, READ_RX, STATUS = (_SPI.op[k] for k in ("configure", "arm", "read_rx", "status"))
    ROLE_SCK, ROLE_MOSI, ROLE_MISO, ROLE_CS = (_SPI.enum["role"][k] for k in ("sck", "mosi", "miso", "cs"))
    MSB_FIRST, LSB_FIRST = 0, 1
    FEATURE_LSB_FIRST = _SPI.enum["features"]["lsb_first"]
    TAG_QUEUE_DEPTH = _SPI.tlv["describe"]["queue_depth"]
    TAG_CS_SETUP_NS = _SPI.tlv["describe"]["cs_setup_ns"]

    def __init__(self, hst: h.Host, fn: int | None = None, name: str | None = None):
        super().__init__(hst, name, fn=fn)
        self.last_ns: int | None = None

    @property
    def cs_setup_ns(self) -> int:
        """The shortest CS-active-to-first-SCK time (ns) for which the probe guarantees MISO carries the first bit,
        under its normal load (describe tag 0x43, fixture §4); 0 when not declared (MISO is driven at once). A master
        that starts SCK sooner cannot rely on the first bit: show it to the user."""
        return self._declared(self.TAG_CS_SETUP_NS, "I") or 0

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
        when CS went inactive, ending it, on the probe's clock (TLV ns) when the probe says, else None. The bytes hold
        the wire bits as `wire_bits` reads them (fixture §4: bit k in byte k / 8, MSB or LSB first by bit_order, the
        missing bits of a partial last byte 0); bits saturates at 0xFFFFFFFF."""
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
