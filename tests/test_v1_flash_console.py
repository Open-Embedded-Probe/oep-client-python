"""ch32_flash, uiapduino, the console views and the fixture UART, against probes modelled by what their operations do
(no hardware)."""

import os
import struct

import pytest

from oep_client.v1 import ch32_flash as cf, console, fixture, host as h, message as m, riscv, uiapduino
from test_v1_target_parts import FNS, ScriptedHost, ok

WIRE, DM, GPIO = FNS["oep.wire.rvswd"], FNS["oep.target.riscv-dm"], FNS["oep.fixture.gpio"]
CONSOLE, UART = 6, 7
FNS.update({"oep.target.console": CONSOLE, "oep.fixture.uart": UART, "oep.wire.swio": WIRE})


class FakeCh32:
    """RAM, flash and the loaders' contract: a run of the fast-page loader copies 256 bytes from the buffer register
    into flash; the V003 loader mass-erases (0x03) or programs a run from its input (0x09 / 0x1d)."""

    def __init__(self, flash_size, garble_once=()):
        self.mem = {}
        self.flash = bytearray(b"\xff" * flash_size)
        self.garble = set(garble_once)          # flash offsets whose first program comes out wrong
        self.runs = 0
        self.ctlr_locked = True
        self.keys = []
        self.timeouts = []

    def read(self, a, n):
        if cf.PROFILES["x035"].base <= a < cf.PROFILES["x035"].base + len(self.flash):
            off = a - cf.PROFILES["x035"].base
            return bytes(self.flash[off:off + n])
        return b"".join(struct.pack("<I", self.mem.get(a + i, 0)) for i in range(0, n, 4))

    def write(self, a, data):
        for i in range(0, len(data), 4):
            (v,) = struct.unpack_from("<I", data, i)
            self.mem[a + i] = v
            if a + i in (cf.KEYR, cf.MODEKEYR):
                self.keys.append(v)
                if len(self.keys) >= 4:
                    self.ctlr_locked = False
        if a == cf.CTLR:
            pass

    def handlers(self):
        def read_block(p):
            a, count = struct.unpack("<IH", p[1:])
            if a == cf.CTLR:
                return ok(struct.pack("<HBI", 1, 0, (cf.LOCK | cf.FLOCK) if self.ctlr_locked else 0))
            return ok(struct.pack("<HB", count, 0) + self.read(a, count * 4))

        def write_block(p):
            a, count = struct.unpack_from("<IH", p, 1)
            assert len(p) == 7 + 4 * count
            self.write(a, p[7:])
            return ok(struct.pack("<HB", count, 0))

        def run(p):
            self.runs += 1
            pc, timeout, n = struct.unpack_from("<IIB", p, 1)
            regs = dict(struct.unpack_from("<HI", p, 10 + 6 * i) for i in range(n))
            n_out = p[10 + 6 * n]
            assert n_out == 1 and struct.unpack_from("<H", p, 11 + 6 * n)[0] == 0x100A    # a0 back
            self.timeouts.append(timeout)
            if 0x100C in regs:                  # V003 loader
                flags, addr, length = regs[0x100A], regs[0x100B], regs[0x100C]
                if flags == 0x03:
                    self.flash[:] = b"\xff" * len(self.flash)
                else:
                    off = addr - cf.PROFILES["v003"].base
                    self.flash[off:off + length] = self.read(cf.V003_INPUT, length)
                return ok(struct.pack("<BBIII", 0, 1, cf.V003_EBREAK, 100, 0))
            addr, buf = regs[0x100A], regs[0x100B]
            off = addr - cf.PROFILES["x035"].base
            data = self.read(buf, 256)
            if off in self.garble:
                self.garble.discard(off)
                data = bytes(b ^ 0x5A for b in data)
            self.flash[off:off + 256] = data
            return ok(struct.pack("<BBIII", 0, 1, cf.FAST_DONE, 100, 0))

        return {(DM, riscv.RiscvDm.READ_BLOCK): read_block, (DM, riscv.RiscvDm.WRITE_BLOCK): write_block,
                (DM, riscv.RiscvDm.RUN): run}


def test_fast_page_programs_verifies_and_rewrites_a_garbled_page():
    chip = FakeCh32(8 * 1024, garble_once={0x300})
    hst = ScriptedHost(chip.handlers())
    image = os.urandom(5000)                         # 20 pages after padding, the last one partial
    r = cf.program(hst, riscv.RiscvDm(hst, 1), image, cf.PROFILES["x035"])
    padded = image + b"\xff" * (-len(image) % 256)
    assert r.verified and r.rewritten_pages == 1
    assert bytes(chip.flash[:len(padded)]) == padded
    assert not chip.ctlr_locked                      # both key pairs went in before programming


def test_v003_mass_erases_then_programs_in_runs():
    chip = FakeCh32(16 * 1024)
    chip.flash[:] = os.urandom(16 * 1024)            # old contents: the mass erase must clear them
    hst = ScriptedHost(chip.handlers())
    image = os.urandom(3000)
    r = cf.program(hst, riscv.RiscvDm(hst, 1), image, cf.PROFILES["v003"])
    padded = image + b"\xff" * (-len(image) % 64)
    assert r.verified and r.rewritten_pages == 0
    assert bytes(chip.flash[:len(padded)]) == padded and chip.flash[len(padded):4096] == b"\xff" * (4096 - len(padded))


def test_an_unknown_flash_method_is_refused():
    hst = ScriptedHost(FakeCh32(4096).handlers())
    with pytest.raises(ValueError, match="unknown flash method"):
        cf.program(hst, riscv.RiscvDm(hst, 1), b"\0" * 64, cf.FlashProfile("mystery", size=4096, page=64))


def test_run_payload_detaches_even_when_the_payload_does_not_read_back():
    detached = []
    def read_block(p):
        count = struct.unpack("<IH", p[1:])[1]
        return ok(struct.pack("<HB", count, 0) + b"\0" * 4 * count)

    hst = ScriptedHost({(WIRE, riscv.Wire.ATTACH): lambda p: ok(struct.pack("<BIBI", 1, 0x382, 0, 1_000_000)),
                        (WIRE, riscv.Wire.DETACH): lambda p: detached.append(p) or ok(),
                        (DM, riscv.RiscvDm.WRITE_BLOCK): lambda p: ok(struct.pack("<HB", (len(p) - 7) // 4, 0)),
                        (DM, riscv.RiscvDm.READ_BLOCK): read_block})
    with pytest.raises(RuntimeError, match="did not read back"):
        uiapduino.normalize_user(hst, riscv.Wire(hst, "oep.wire.swio"))
    assert detached == [b"\x01"]


def test_console_io_counts_what_the_ring_dropped():
    reads = iter([(100, b"abc", 0), (103, b"de", 0), (200, b"xyz", 2)])   # (start, data, flags); 2 = gap

    def read(p):
        start, data, flags = next(reads)
        return ok(struct.pack("<IB", start, flags) + data)

    hst = ScriptedHost({(CONSOLE, console.Console.READ): read})
    io = console.ConsoleIO(console.Console(hst), start=100)
    assert io.read() + io.read() == b"abcde" and io.lost == 0
    assert io.read() == b"xyz" and io.lost == 200 - 105 and io.position == 203


def test_fixture_uart_write_splits_and_waits_for_the_uart():
    taken = iter([256, 0, 100, 44])                  # the UART takes a chunk, then nothing once, then the rest

    def write(p):
        count = struct.unpack_from("<H", p)[0]
        assert len(p) == 2 + count
        took = min(next(taken), count)
        return m.COMPLETED, m.SUCCESS if took == count else m.PARTIAL, struct.pack("<H", took)   # partial is no error

    hst = ScriptedHost({(UART, fixture.FixtureUart.WRITE): write})
    uart = fixture.FixtureUartIO(hst, UART)
    uart.write(bytes(400))
    sizes = [len(p) - 2 for fn, op, p in hst.log if op == fixture.FixtureUart.WRITE]
    assert sizes == [256, 144, 144, 44]


def test_a_failed_console_open_raises():
    hst = ScriptedHost({(CONSOLE, console.Console.OPEN): lambda p: (m.COMPLETED, m.FAILED, b"")})
    with pytest.raises(h.Failed):
        console.Console(hst).open(1)
