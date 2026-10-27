"""oep.fixture.i2c-controller / spi-controller clients (fixture §5, §6): the request bodies and the answers of the spec's
vectors (ops.json), against a scripted host."""

import json
import struct
from pathlib import Path

from oep_client import fixture as et, registry as reg
from test_target_parts import ScriptedHost, ok

I2CC, SPIC = 15, 16
REQUEST_HEADER, RESULT_HEADER = reg.LIMITS["request_header_bytes"], reg.LIMITS["result_header_bytes"]
CASES = {c["name"]: c for c in json.loads((Path(__file__).parent / "vectors/ops.json").read_text())["cases"]}


def vector(name):
    """-> (the request's payload, the answer's payload) of an ops.json case."""
    c = CASES[name]
    return bytes.fromhex(c["request_hex"])[REQUEST_HEADER:], bytes.fromhex(c["answer_hex"])[RESULT_HEADER:]


def host(handlers):
    hst = ScriptedHost(handlers)
    hst._fns.update({et.I2cController.NAME: I2CC, et.SpiController.NAME: SPIC})
    hst._revisions.update({I2CC: 1, SPIC: 1})
    return hst


def test_i2c_controller_vectors():
    configure, configured = vector("i2c-controller configure: the SCL used at or below the one asked")
    register, read = vector("i2c-controller transfer: a register read (write, repeated START, read, STOP)")
    nobody, nack = vector("i2c-controller transfer: nobody at the address")
    second, data_nack = vector("i2c-controller transfer: the target NACKs the second data byte")
    _, held = vector("i2c-controller status: holding the bus after a transfer without STOP")
    answers = iter([read, nack, data_nack])
    hst = host({(I2CC, et.I2cController.CONFIGURE): lambda p: ok(configured),
                (I2CC, et.I2cController.TRANSFER): lambda p: ok(next(answers)),
                (I2CC, et.I2cController.STATUS): lambda p: ok(held)})
    c = et.I2cController(hst)
    assert c.configure(400000, 10000) == 380000 and hst.log[-1][2] == configure
    t = c.write_read(0x50, b"\x10", 2)
    assert hst.log[-1][2] == register
    assert t.ok and t.data == b"\xab\xcd"
    t = c.write(0x51, b"")
    assert hst.log[-1][2] == nobody and t.result == c.ADDRESS_NACK and not t.ok
    t = c.write(0x50, b"\x01\x02\x03")
    assert hst.log[-1][2] == second and t.result == c.DATA_NACK and (t.at, t.index) == (0, 1)
    assert c.status() == et.I2cControllerStatus(state=1, held=True, transfers=1, errors=0)
    assert c.assignments(4, 5) == [(I2CC, 1, 4), (I2CC, 2, 5)]


def test_i2c_controller_stop_and_held():
    hst = host({(I2CC, et.I2cController.TRANSFER): lambda p: ok(struct.pack("<BBHH", 0, 0, 0, 0))})
    c = et.I2cController(hst)
    c.write(0x50, b"\x00", stop=False)
    assert hst.log[-1][2][0] == 0                                       # no STOP flag: the bus held
    c.stop()
    assert hst.log[-1][2] == bytes([c.FLAG_STOP, 0])                    # n 0 with STOP: a STOP alone


def test_spi_controller_vectors():
    configure, configured = vector("spi-controller configure: the SCK used at or below the one asked")
    request, answer = vector("spi-controller transfer: two transfers, the times used at or above the ones asked")
    hst = host({(SPIC, et.SpiController.CONFIGURE): lambda p: ok(configured),
                (SPIC, et.SpiController.TRANSFER): lambda p: ok(answer),
                (SPIC, et.SpiController.STATUS): lambda p: ok(struct.pack("<BBBI", 1, 3, 1, 7))})
    c = et.SpiController(hst)
    assert c.configure(0, 0, 1200000) == 1000000 and hst.log[-1][2] == configure
    done = c.transfer([et.SpiItem(b"\x9f", 0, 1000, 500), et.SpiItem(b"\x01\x02", 2010, 1000, 500)])
    assert hst.log[-1][2] == request
    assert done == [et.SpiDone(gap_ns=0, setup_ns=1000, hold_ns=500, rx=b"\x5a"),
                    et.SpiDone(gap_ns=2025, setup_ns=1000, hold_ns=500, rx=b"\x00\xff")]
    assert c.status() == et.SpiControllerStatus(state=1, mode=3, bit_order=1, transfers=7)
    assert c.assignments(1, 2, 3, 4) == [(SPIC, 1, 1), (SPIC, 2, 2), (SPIC, 3, 3), (SPIC, 4, 4)]
