"""Host-side parts added on 2026-09-24, against a scripted host (no hardware): the reset-line search, the gpio-reset
fallback, reset-halt / step decoding, and the ARM ADI / MEM-AP helpers."""

import struct

import pytest

from oep_client.v1 import arm, host as h, message as m, target

FNS = {"oep.wire.rvswd": 1, "oep.target.riscv-dm": 2, "oep.fixture.gpio": 3, "oep.wire.swd": 4,
       "oep.target.arm-adi": 5}


class ScriptedHost:
    """Routes (fn, op) to a handler returning (resolution, detail, payload); records every request."""

    def __init__(self, handlers):
        self.handlers, self.log = handlers, []

    def request(self, fn, op, payload=b"", locked=True):
        self.log.append((fn, op, payload))
        res, detail, body = self.handlers[(fn, op)](payload)
        r = m.Result(len(self.log), res, detail, body)
        if res == m.REJECTED:
            raise h.Rejected(r)
        return r

    def pipeline(self, requests, exchange=None, locked=True):
        out = []
        for fn, op, payload in requests:
            self.log.append((fn, op, payload))
            res, detail, body = self.handlers[(fn, op)](payload)
            out.append(m.Result(len(self.log), res, detail, body))
        return out


def ok(body=b""):
    return m.COMPLETED, m.SUCCESS, body


@pytest.fixture(autouse=True)
def names(monkeypatch):
    monkeypatch.setattr(target, "find", lambda hst, name: FNS[name])


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
                        (1, target.Wire.DETACH): lambda p: ok()})
    wire = target.Wire(hst)
    assert wire.find_reset_line([3, 9, 2, 5], tries=3) == [2]
    assert isinstance(wire.last_search[9], h.Rejected)
    assert wire.last_search[2] == [0x1302, 0]
    assert wire.last_search[5] == [None, 0x1305, 0x1305]
    assert wire.last_search[3] == [0x1303] * 3


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
    with pytest.raises(h.Rejected):
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
                        (4, target.Wire.ATTACH): lambda p: ok(struct.pack("<BIB", 1, 0x4c013477, 1))})
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
    assert [struct.unpack("<IH", p[1:])[1] for p in blocks] == [240, 240, 20]
    mem.write_block(0x2007F3F0, list(range(16)))
    assert mem.read_block(0x2007F3F0, 16) == list(range(16))


def test_transfer_fault_raises_with_the_step_count(adi_bench):
    fake, hst = adi_bench
    adi = arm.ArmAdi(hst, 1, adiv6=True)
    adi.select(0xE000)
    with pytest.raises(arm.AdiError, match="after 0 steps: FAULT"):
        adi.transfer(adi.req(True, True, 0x0))
