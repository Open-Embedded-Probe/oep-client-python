"""The ESP32 I2C / SPI slave tools of oep-probe-arduino, custom interfaces revision 1:
io.github.ch32-riscv-ug.esp32.i2c-target and io.github.ch32-riscv-ug.esp32.spi-target (OepP4I2cTarget.h /
OepP4SpiTarget.h). A DUT's I2C / SPI controller talks to them; the host arms what they answer and reads what they got.
Variable byte lists carry a count(u16) in front (oep-core §2.3)."""

from __future__ import annotations

import struct
from dataclasses import dataclass

from . import host as h, message as m
from .core import Interface

NS = "io.github.ch32-riscv-ug.esp32"


@dataclass
class I2cStatus:
    flags: int          # bit0 started, bits 1-2 mode, bit4 armed, bits 5-7 frames queued
    rx_frames: int
    tx_slots: int
    errors: int


@dataclass
class I2cRegisters:
    sr: int
    int_raw: int
    fifo_st: int
    ctr: int
    slave_addr: int
    filter_cfg: int
    scl_stretch_conf: int


class I2cTarget(Interface):
    """The I2C target (E147-E150 contract): fixed rx (arm_rx with the exact length), framed rx (a 1-byte length header
    transaction, then the payload), preloaded tx (slots the controller reads)."""
    NAME = f"{NS}.i2c-target"
    REVISION = 1
    CONFIGURE, ARM_RX, READ_RX, PRELOAD_TX, STATUS, RESET, READ_HW, SET_STRETCH = 1, 2, 3, 4, 5, 6, 0x10, 0x11
    MODE_FIXED_RX, MODE_FRAMED_RX, MODE_PRELOADED_TX = 1, 2, 3
    ROLE_SDA, ROLE_SCL = 1, 2

    def __init__(self, hst: h.Host, fn: int | None = None, name: str | None = None):
        super().__init__(hst, name, fn=fn)

    def assignments(self, sda: int, scl: int) -> list[tuple[int, int, int]]:
        return [(self.fn, self.ROLE_SDA, sda), (self.fn, self.ROLE_SCL, scl)]

    def configure(self, address: int, mode: int) -> None:
        self._call(self.CONFIGURE, struct.pack("<BB", address, mode))

    def arm_rx(self, length: int) -> None:
        self._call(self.ARM_RX, struct.pack("<H", length))

    def read_rx(self) -> tuple[int, bytes]:
        """-> (frames still queued after this one, the oldest frame or b"")."""
        rd = m.Reader(self._call(self.READ_RX).payload)
        pending, count = rd.take("BH")
        data = rd.bytes(count)
        rd.tail()
        return pending, data

    def preload_tx(self, data: bytes) -> int:
        """-> the slots preloaded so far."""
        rd = m.Reader(self._call(self.PRELOAD_TX, struct.pack("<H", len(data)) + data).payload)
        slots = rd.u8()
        rd.tail()
        return slots

    def status(self) -> I2cStatus:
        rd = m.Reader(self._call(self.STATUS, locked=False).payload)
        s = I2cStatus(*rd.take("BIBH"))
        rd.tail()
        return s

    def reset(self) -> None:
        self._call(self.RESET)

    def set_stretch(self, stretch_us: int) -> None:
        """Hold every hardware SCL stretch for stretch_us (0 = off); takes effect at the next configure()."""
        self._call(self.SET_STRETCH, struct.pack("<I", stretch_us))

    def read_hw(self) -> I2cRegisters:
        """Debug view of the P4 I2C0 block (zeros on other chips)."""
        rd = m.Reader(self._call(self.READ_HW, locked=False).payload)
        r = I2cRegisters(*rd.take("7I"))
        rd.tail()
        return r


@dataclass
class SpiStatus:
    flags: int          # bit0 started, bit1 armed, bits 2-3 mode, bit4 LSB first, bits 5-7 queued
    transactions: int
    errors: int


class SpiTarget(Interface):
    """The SPI target (SPI2_HOST, no DMA, <= 64 bytes): one CS-framed transaction at a time - arm() with the MISO
    bytes, then read_rx() after the controller raised CS."""
    NAME = f"{NS}.spi-target"
    REVISION = 1
    CONFIGURE, ARM, READ_RX, STATUS, RESET = 1, 2, 3, 4, 5
    ROLE_SCK, ROLE_MOSI, ROLE_MISO, ROLE_CS = 1, 2, 3, 4
    MSB_FIRST, LSB_FIRST = 0, 1

    def __init__(self, hst: h.Host, fn: int | None = None, name: str | None = None):
        super().__init__(hst, name, fn=fn)

    def assignments(self, sck: int, mosi: int, miso: int, cs: int) -> list[tuple[int, int, int]]:
        return [(self.fn, self.ROLE_SCK, sck), (self.fn, self.ROLE_MOSI, mosi), (self.fn, self.ROLE_MISO, miso),
                (self.fn, self.ROLE_CS, cs)]

    def configure(self, mode: int = 0, bit_order: int = 0) -> None:
        self._call(self.CONFIGURE, struct.pack("<BB", mode, bit_order))

    def arm(self, length: int, tx: bytes = b"") -> None:
        self._call(self.ARM, struct.pack("<HH", length, len(tx)) + tx)

    def read_rx(self) -> tuple[int, int, bytes]:
        """-> (transactions still queued, bits clocked, the MOSI bytes) of the oldest finished transaction."""
        rd = m.Reader(self._call(self.READ_RX).payload)
        pending, bits, count = rd.take("BIH")
        data = rd.bytes(count)
        rd.tail()
        return pending, bits, data

    def status(self) -> SpiStatus:
        rd = m.Reader(self._call(self.STATUS, locked=False).payload)
        s = SpiStatus(*rd.take("BIH"))
        rd.tail()
        return s

    def reset(self) -> None:
        self._call(self.RESET)
