"""Host-side parts added on 2026-09-24, against a scripted host (no hardware): the reset-line search, the gpio-reset
fallback, reset-halt / step decoding, and the ARM ADI / MEM-AP helpers."""

import struct

import pytest

from oep_client.v1 import arm, host as h, message as m, target

FNS = {"oep.wire.rvswd": 1, "oep.target.riscv-dm": 2, "oep.fixture.gpio": 3, "oep.wire.swd": 4,
       "oep.target.arm-adi": 5}


class ScriptedHost(h.Host):
    """A Host whose requests go to handlers: (fn, op) -> (resolution, detail, payload); records every request.
    Interface names resolve through the host's fn cache, filled in advance."""

    def __init__(self, handlers):
        super().__init__(send=None)
        self.handlers, self.log = handlers, []
        self._fns.update(FNS)

    def request(self, fn, op, payload=b"", *, locked=True):
        self.log.append((fn, op, payload))
        res, detail, body = self.handlers[(fn, op)](payload)
        r = m.Result(len(self.log), res, detail, body)
        if res == m.REJECTED:
            raise h.Rejected(r)
        return r

    def pipeline(self, requests, exchange=None, *, locked=True):
        out = []
        for fn, op, payload in requests:
            self.log.append((fn, op, payload))
            res, detail, body = self.handlers[(fn, op)](payload)
            out.append(m.Result(len(self.log), res, detail, body))
        return out


def ok(body=b""):
    return m.COMPLETED, m.SUCCESS, body


# ---- reset-line search ----------------------------------------------------------------------------

def test_find_reset_line_hits_the_vector_skips_disallowed_and_retries_failures():
    tries = {}

    def aur(p):
        channel, _ = struct.unpack("<HH", p)
        tries[channel] = tries.get(channel, 0) + 1
        if channel == 9:
            return m.REJECTED, m.UNAVAILABLE, b""                      # not allowed on this probe
        if channel == 5 and tries[5] == 1:
            return m.COMPLETED, m.FAILED, b""                          # the attach itself failed once
        dpc = 0 if channel == 2 and tries[2] == 2 else 0x1300 + channel  # the real line, caught on its second try
        return ok(struct.pack("<BI", 1, dpc))

    hst = ScriptedHost({(1, target.Wire.ATTACH_UNDER_RESET): aur,
                        (2, target.RiscvDm.RESUME): lambda p: (m.COMPLETED, m.FAILED, b""),   # an L103 resume
                        (2, target.RiscvDm.RESET): lambda p: ok(struct.pack("<BBI", 3, 1, 0x1234)),
                        (1, target.Wire.DETACH): lambda p: ok()})
    wire = target.Wire(hst)
    assert wire.find_reset_line([3, 9, 2, 5], tries=3) == [2]
    assert isinstance(wire.last_search[9], h.Rejected)
    assert wire.last_search[2] == [0x1302, 0]
    assert wire.last_search[5] == [None, 0x1305, 0x1305]
    assert wire.last_search[3] == [0x1303] * 3
    resets = [p for fn, op, p in hst.log if (fn, op) == (2, target.RiscvDm.RESET)]
    assert resets == [bytes([1, target.RiscvDm.RESET_RUN_CONFIRM])]   # only after the hit: off the vector for real


def test_attach_after_gpio_reset_pipelines_release_with_attach_and_retries():
    attaches = []

    def attach(p):
        attaches.append(p)
        return (ok(struct.pack("<BIB", 1, 0xc82, 0)) if len(attaches) == 3 else (m.COMPLETED, m.FAILED, b""))

    hst = ScriptedHost({(3, 0x01): lambda p: ok(), (1, target.Wire.ATTACH): attach})
    wire = target.Wire(hst)
    conn, status = target.attach_after_gpio_reset(hst, wire, 3, 23, exchange=None, tries=5, low_s=0)
    assert (conn, status) == (1, 0xc82)
    assert len(attaches) == 3 and attaches[0] == b"\x01"                # attach with halt
    ops = [(fn, op) for fn, op, _ in hst.log]
    assert ops[:3] == [(3, 0x01), (3, 0x01), (1, target.Wire.ATTACH)]   # low, then release + attach together


def test_attach_after_gpio_reset_gives_up():
    hst = ScriptedHost({(3, 0x01): lambda p: ok(), (1, target.Wire.ATTACH): lambda p: (m.COMPLETED, m.FAILED, b"")})
    with pytest.raises(h.Failed):
        target.attach_after_gpio_reset(hst, target.Wire(hst), 3, 23, exchange=None, tries=2, low_s=0)


# ---- riscv-dm decoding ----------------------------------------------------------------------------

def test_reset_halt_and_step_decode():
    hst = ScriptedHost({(2, target.RiscvDm.RESET): lambda p: ok(struct.pack("<BBI", 0, 1, 0x0) if p[1] == 2 else b""),
                        (2, target.RiscvDm.STEP): lambda p: ok(struct.pack("<BII", 1, 0x0, 0x17f0))})
    dm = target.RiscvDm(hst, 1)
    assert dm.reset_halt() == 0
    assert hst.log[-1][2] == bytes([1, 2])                              # connection, mode 2
    assert dm.step() == (True, 0x0, 0x17f0)


# ---- ARM ADI / MEM-AP -----------------------------------------------------------------------------

class FakeAdi:
    """A tiny ADIv6 DP + one MEM-AP at 0x2000 behind the probe's transfer / block operations."""

    def __init__(self):
        self.select, self.csw, self.tar, self.mem, self.posted = 0, 0x43800052, 0, {}, 0
        self.selects = []

    def transfer(self, p):
        p, at, out, done = p[1:], 0, b"", 0
        while at < len(p):
            req = p[at]; at += 1
            ap, read, a = req & 1, req >> 1 & 1, (req >> 2 & 3) << 2
            value = 0
            if not read:
                value = struct.unpack_from("<I", p, at)[0]; at += 4
            if not ap and not read and a == 0x8:
                self.select = value; self.selects.append(value)
            elif not ap and read and a == 0xC:
                out += struct.pack("<I", self.posted)
            elif not ap and read and a == 0x4:
                out += struct.pack("<I", 0xF0000000)
            elif not ap and not read:
                pass
            elif ap:
                addr = (self.select & ~0xF) | a
                if addr == 0x2D00:
                    if read: out += struct.pack("<I", self.posted); self.posted = self.csw
                    else: self.csw = value
                elif addr == 0xE000:
                    return ok(struct.pack("<HBB", done, 2, 4) + out)     # FAULT
            done += 1
        return ok(struct.pack("<HBB", done, 0, 1) + out)

    def read_block(self, p):
        address, count = struct.unpack("<IH", p[1:])
        assert self.select == 0x2D00                                     # TAR / DRW bank selected
        return ok(b"".join(struct.pack("<I", self.mem.get(address + 4 * i, address + 4 * i)) for i in range(count)))

    def write_block(self, p):
        address = struct.unpack_from("<I", p, 1)[0]
        for i in range((len(p) - 5) // 4):
            self.mem[address + 4 * i] = struct.unpack_from("<I", p, 5 + 4 * i)[0]
        return ok()


@pytest.fixture
def adi_bench():
    fake = FakeAdi()
    hst = ScriptedHost({(5, arm.ArmAdi.TRANSFER): fake.transfer, (5, arm.ArmAdi.READ_BLOCK): fake.read_block,
                        (5, arm.ArmAdi.WRITE_BLOCK): fake.write_block,
                        (4, target.Wire.ATTACH): lambda p: ok(struct.pack("<BIB", 1, 0x4c013477, 1)),
                        (0, m.OP_CONFIRM): lambda p: ok(struct.pack("<4sBHHB", b"OEP!", 1, 1024, 4096, 8))})
    return fake, hst


def test_swd_attach_decodes_dpidr_and_dormant(adi_bench):
    fake, hst = adi_bench
    assert arm.SwdWire(hst).attach() == (1, 0x4c013477, True)
    arm.SwdWire(hst).attach(targetsel=0x01002927)
    assert hst.log[-1][2] == struct.pack("<I", 0x01002927)


def test_ap_read_uses_adiv6_select_and_the_posted_value(adi_bench):
    fake, hst = adi_bench
    adi = arm.ArmAdi(hst, 1, adiv6=True)
    assert adi.ap_read(0x2000, 0xD00) == 0x43800052
    assert fake.selects == [0x2D00]
    adi.ap_read(0x2000, 0xD00)
    assert fake.selects == [0x2D00]                                     # cached: no second SELECT write


def test_mem_ap_sets_csw_from_the_caller_and_chunks_blocks(adi_bench):
    fake, hst = adi_bench
    adi = arm.ArmAdi(hst, 1, adiv6=True)
    mem = arm.MemAp(adi, 0x2000, csw_clear=1 << 30)
    assert fake.csw == (0x43800052 & ~0x37 | 0x12) & ~(1 << 30)
    words = mem.read_block(0x1000, 500)
    assert words == [0x1000 + 4 * i for i in range(500)]
    blocks = [p for fn, op, p in hst.log if op == arm.ArmAdi.READ_BLOCK]
    assert [struct.unpack("<IH", p[1:])[1] for p in blocks] == [252, 248]   # (1024 - 15) // 4 words per block
    mem.write_block(0x2007F3F0, list(range(16)))
    assert mem.read_block(0x2007F3F0, 16) == list(range(16))


def test_transfer_fault_raises_with_the_step_count(adi_bench):
    fake, hst = adi_bench
    adi = arm.ArmAdi(hst, 1, adiv6=True)
    adi.select(0xE000)
    with pytest.raises(arm.AdiError, match="after 0 steps: FAULT"):
        adi.transfer(adi.req(True, True, 0x0))


# ---- Cortex-M call primitive and the RP2350 ROM flash sequence, on a fake core --------------------------------

class FakeCortexM:
    """A MemAp stand-in: DHCSR / DCRSR / DCRDR semantics, plus a ROM whose table lookup and flash functions are
    modelled by what they do to the fake's registers and flash, so the host's sequence can be checked end to end."""
    ROM = {("I", "F"): 0xE3D, ("E", "X"): 0xF65, ("R", "E"): 0xF15, ("R", "P"): 0xEDD, ("F", "C"): 0x3801,
           ("C", "X"): 0x659, ("R", "B"): 0x6F3}

    def __init__(self):
        self.regs = {i: 0 for i in range(17)}
        self.regs[16] = 0x01000000
        self.ram = {}
        self.flash = bytearray(b"\xff" * (64 * 1024))
        self.dhcsr = 0
        self.xip = True
        self.log = []

    def read32(self, a):
        if a == 0xE000EDF0: return self.dhcsr | (1 << 16)
        if a == 0xE000EDF8: return self.regs[self.sel]
        if a == 0x14: return 0x008D0000                              # table_lookup ptr 0x8d in the high halfword
        if 0x10000000 <= a < 0x10000000 + len(self.flash):
            assert self.xip, "flash read while XIP is off"
            return struct.unpack_from("<I", self.flash, a - 0x10000000)[0]
        return self.ram.get(a, 0)

    def write32(self, a, v):
        if a == 0xE000EDF0:
            was_halted = self.dhcsr & 1 << 17
            self.dhcsr = v & 0xF
            if not v & 2 and was_halted:                             # let go (debug on or off): run the "function" at PC
                self.run()
            self.dhcsr |= 1 << 17 if v & 2 else 0
        elif a == 0xE000EDF4:
            self.sel = v & 0xFF
            if v >> 16:
                self.regs[self.sel] = self.pending
        elif a == 0xE000EDF8:
            self.pending = v
        else:
            self.ram[a] = v

    def read_block(self, a, n):
        return [self.read32(a + 4 * i) for i in range(n)]

    def write_block(self, a, vals):
        for i, v in enumerate(vals):
            self.write32(a + 4 * i, v)

    def write_many(self, pairs):
        for a, v in pairs:
            self.write32(a, v)

    def run(self):
        pc, r = self.regs[15], self.regs
        if pc == 0x6F2:                                              # reboot: runs with interrupts on, never returns
            assert not self.dhcsr & 8, "reboot with C_MASKINTS left set"
            self.log.append(f"reboot flags {r[0]} delay {r[1]}"); return
        assert self.dhcsr & 8, "a ROM call without C_MASKINTS"
        if pc == 0x8C:                                              # rom_table_lookup(code, mask)
            r[0] = self.ROM.get((chr(r[0] & 0xFF), chr(r[0] >> 8)), 0)
        elif pc == 0xF64:  self.xip = False; self.log.append("exit_xip")
        elif pc == 0xE3C:  self.log.append("connect")
        elif pc == 0xF14:
            assert not self.xip
            self.flash[r[0]:r[0] + r[1]] = b"\xff" * r[1]; self.log.append(f"erase {r[0]:#x}+{r[1]}")
        elif pc == 0xEDC:
            assert not self.xip
            for i in range(r[2] // 4):
                struct.pack_into("<I", self.flash, r[0] + 4 * i, self.ram.get(r[1] + 4 * i, 0))
            self.log.append(f"program {r[0]:#x}+{r[2]}")
        elif pc == 0x3800: self.log.append("flush")
        elif pc == 0x658:  self.xip = True; self.log.append("enter_cmd_xip")
        else: raise AssertionError(f"call to {pc:#x}")
        assert self.regs[13] == 0x20080000 and self.regs[14] == 0x20040001 and self.regs[16] & 1 << 24
        self.regs[15] = 0x20040000                                    # returned onto the breakpoint
        self.dhcsr |= 1 << 17


def test_cortexm_call_returns_r0_and_clears_maskints():
    fake = FakeCortexM()
    core = arm.CortexM(fake, bkpt_at=0x20040000, stack_top=0x20080000)
    core.halt()
    assert core.call(0x8C, (ord("R") | ord("B") << 8, 4)) == 0x6F3
    assert fake.ram[0x20040000] == 0xBE00BE00
    assert not fake.dhcsr & 8                                          # C_MASKINTS cleared after the call


def test_rp2350_flash_program_runs_the_sdk_sequence_and_verifies():
    from oep_client.v1 import rp2350
    fake = FakeCortexM()
    core = arm.CortexM(fake, bkpt_at=rp2350.BKPT_AT, stack_top=rp2350.STACK_TOP)
    core.halt()
    image = bytes(range(256)) * 70 + b"\x12\x34"                      # 17922 B: 70 pages + a partial one
    assert rp2350.program_and_verify(core, image, 0)
    assert fake.log[:3] == ["connect", "exit_xip", "erase 0x0+20480"]  # erase rounded to 4 KiB sectors
    assert fake.log[3:5] == ["program 0x0+16384", "program 0x4000+1792"]   # 16 KiB buffer, page-padded tail
    assert fake.log[5:] == ["flush", "enter_cmd_xip"]
    assert bytes(fake.flash[:len(image)]) == image and fake.flash[len(image):20480] == b"\xff" * (20480 - len(image))
    rp2350.Rom(core).reboot()
    assert fake.log[-1] == "reboot flags 0 delay 10" and not fake.dhcsr & 8
