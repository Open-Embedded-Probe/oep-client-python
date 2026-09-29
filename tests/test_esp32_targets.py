"""The ESP32 I2C / SPI target clients (custom interfaces revision 1) and the I2C decoder, against a scripted host."""

import struct

from oep_client import decode, esp32_targets as et, message as m
from test_target_parts import ScriptedHost, ok

I2C, SPI = 11, 12


def host(handlers):
    hst = ScriptedHost(handlers)
    hst._fns.update({et.I2cTarget.NAME: I2C, et.SpiTarget.NAME: SPI})
    hst._revisions.update({I2C: 1, SPI: 1})
    return hst


def test_i2c_target_shapes():
    hst = host({(I2C, et.I2cTarget.CONFIGURE): lambda p: ok(),
                (I2C, et.I2cTarget.PRELOAD_TX): lambda p: ok(bytes([2])),
                (I2C, et.I2cTarget.READ_RX): lambda p: ok(bytes([1]) + struct.pack("<H", 3) + b"abc"),
                (I2C, et.I2cTarget.STATUS): lambda p: ok(struct.pack("<BIBH", 0x07, 5, 2, 0)),
                (I2C, et.I2cTarget.READ_HW): lambda p: ok(struct.pack("<7I", *range(7)))})
    t = et.I2cTarget(hst)
    t.configure(0x42, t.MODE_PRELOADED_TX)
    assert hst.log[-1][2] == bytes([0x42, 3])
    assert t.preload_tx(b"\x11\x22") == 2 and hst.log[-1][2] == struct.pack("<H", 2) + b"\x11\x22"
    assert t.read_rx() == (1, b"abc")
    assert t.status() == et.I2cStatus(0x07, 5, 2, 0)
    assert t.read_hw().scl_stretch_conf == 6
    assert t.assignments(50, 52) == [(I2C, 1, 50), (I2C, 2, 52)]


def test_spi_target_shapes():
    hst = host({(SPI, et.SpiTarget.ARM): lambda p: ok(),
                (SPI, et.SpiTarget.READ_RX): lambda p: ok(bytes([0]) + struct.pack("<IH", 32, 4) + b"\xa5\x5a\x0f\x01")})
    t = et.SpiTarget(hst)
    t.arm(4, b"\x01\x02")
    assert hst.log[-1][2] == struct.pack("<HH", 4, 2) + b"\x01\x02"
    assert t.read_rx() == (0, 32, bytes.fromhex("a55a0f01"))


def test_decode_i2c_reads_start_bytes_ack_and_stop():
    # START, 0xA5 with ACK, STOP: SCL toggles, SDA changes while SCL is low
    scl, sda = [1, 1], [1, 0]                      # START: SDA falls while SCL high
    for bit in [1, 0, 1, 0, 0, 1, 0, 1, 0]:        # 0xA5, then ACK (0)
        scl += [0, 0, 1, 1]
        sda += [sda[-1], bit, bit, bit]
    scl += [0, 1, 1]
    sda += [0, 0, 1]                               # STOP: SDA rises while SCL high
    trace = decode.decode_i2c(scl, sda)
    assert trace.summary() == "S a5A P"
