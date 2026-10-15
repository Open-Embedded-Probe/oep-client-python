"""Sixth review, oep-if-debug from §4 (riscv-dm, swd, arm-adi) and oep-if-restart: code divergences from oep-spec
a09fa45 found and fixed, each test failing on the code before the fix.

- debug §4: "host は、dmi の要求で抽象コマンドなどを使って DATA0 / DATA1 を変えたら、hart を走らせる前に、変える前の値を書き戻す。"
  - uiapduino.run_payload wrote mstatus and dpc through abstract commands (DATA0 changed) and then resumed the hart
  with raw DMCONTROL writes, DATA0 never written back.
- debug §4.6: "haveresetの自動確認応答は高水準のopとコンソールの読みで行う。" - the bench's console reads never acknowledged a
  pending havereset (nor marked the stream restart, detail 1 havereset): only the high-level ops did.
- debug §6: "最小の実装はtransferだけでよく、hostがAPの選択、CSW/TARの設定、posted readの回収を生の転送で組む。" - the
  client's MemAp required read_block / write_block (optional) and their max_length: on a probe with transfer only
  it raised NoMaxLength and could not read or write memory at all.
- debug §6: "任意opのためのfeaturesは追加しない。" - the selected-metadata checker failed an arm-adi describe that carries
  a features tag at all, even one with no bit set (the interface defines no bits; it does not forbid the tag).
"""

from __future__ import annotations

import random
import struct

from oep_client import arm, catalog, console, endpoint, host, message as m, registry as reg, riscv, uiapduino, \
    virtual_bench
from test_target_parts import FNS, ScriptedHost, ok
from test_virtual_bench_spec import Clock, Host

RV = reg.TARGET_RISCV_DM.op
W = reg.WIRE_RVSWD.op
CON = reg.TARGET_CONSOLE


# ---- debug §4: DATA0 back before the hart runs (uiapduino.run_payload) -----------------------------------------------

def test_run_payload_writes_data0_back_before_it_resumes_the_hart():
    ep = endpoint.Endpoint(virtual_bench.p4_x035(), Clock())
    hst = host.Host(ep.handle, rng=random.Random(1))
    hst.open(lease_ms=10000)
    tg = ep.target
    tg.dmi[riscv.DATA0] = 0x5A5A0001                              # the console's mailbox word, say
    writes = []
    write = tg.write_dmi

    def logged(address, value):
        writes.append((address, value))
        write(address, value)
    tg.write_dmi = logged
    uiapduino.run_payload(hst, riscv.Wire(hst), [0x0000006F])
    resume = next(i for i, (a, v) in enumerate(writes) if a == riscv.DMCONTROL and v & (1 << 30))
    last_command = max(i for i, (a, _) in enumerate(writes[:resume]) if a == 0x17)
    assert (riscv.DATA0, 0x5A5A0001) in writes[last_command:resume]
    assert tg.dmi[riscv.DATA0] == 0x5A5A0001 and not tg.halted


# ---- debug §4.6: the console's read acknowledges a pending havereset ------------------------------------------------

def _console_bench():
    ep = endpoint.Endpoint(virtual_bench.p4_bench(), Clock())
    hv = Host(ep)
    assert hv.open().succeeded
    pair = ep.pairs[1][0]
    r = hv.raw(1, W["attach"], struct.pack("<BHHI", 0, *pair, 4_000_000))
    assert r.succeeded
    cid = struct.unpack_from("<I", r.payload)[0]
    sid = struct.unpack_from("<I", hv.ok(3, CON.op["open"], struct.pack("<IB", cid, console.Console.DMSEQ)))[0]
    return ep, hv, ep.targets[(1, pair)], cid, sid


def test_the_console_read_acknowledges_a_pending_havereset_with_a_restart_mark():
    ep, hv, tg, cid, sid = _console_bench()
    tg.havereset = True                                            # the target reset itself, and runs
    read = struct.pack("<IHBB", cid, 1, 2, 0x11)
    dmstatus = struct.unpack("<HBHI", hv.ok(2, RV["dmi"], read))[3]
    assert not tg.havereset and not dmstatus & 0xC0000              # the console's read before it acknowledged it
    assert [(mk[2], mk[4]) for mk in ep.streams[sid].marks] == [
        (CON.enum["mark_kind"]["restart"], CON.enum["mark_detail_restart"]["havereset"])]


def test_the_console_does_not_read_a_halted_hart():
    ep, hv, tg, cid, sid = _console_bench()
    tg.halted, tg.havereset = True, True
    hv.ok(2, RV["dmi"], struct.pack("<IHBB", cid, 1, 2, 0x11))
    assert tg.havereset and ep.streams[sid].marks == []           # console §3: no reading while it is halted


# ---- debug §6: MemAp over raw transfers when the probe offers transfer only ------------------------------------------

class RawAdi:
    """An ADIv6 MEM-AP at 0x2000 behind transfer alone: CSW / TAR / DRW (D00 / D04 / D0C), posted AP reads, RDBUFF.
    A DRW access past the 1 KB TAR was last written in is an error (ADI guarantees the auto-increment within it)."""

    def __init__(self):
        self.select, self.csw, self.tar, self.tar_set, self.posted, self.mem = 0, 0x43800052, 0, 0, 0, {}
        self.sizes = []

    def transfer(self, p):
        self.sizes.append(m.REQUEST_HEADER + len(p))
        n = struct.unpack_from("<H", p, 4)[0]
        p, at, out = p[6:], 0, []
        for _ in range(n):
            req = p[at]; at += 1
            ap, read, a = req & 1, req >> 1 & 1, (req >> 2 & 3) << 2
            value = None
            if not read:
                value = struct.unpack_from("<I", p, at)[0]; at += 4
            if not ap:
                if not read and a == 0x8:
                    self.select = value
                elif read and a == 0xC:
                    out.append(self.posted)
                continue
            addr = (self.select & ~0xF) | a
            if addr == 0x2D04:
                if read:
                    out.append(self.posted); self.posted = self.tar
                else:
                    self.tar = self.tar_set = value
                continue
            if addr == 0x2D0C:
                assert self.tar >> 10 == self.tar_set >> 10, "DRW past TAR's guaranteed auto-increment"
                if read:
                    out.append(self.posted); self.posted = self.mem.get(self.tar, self.tar)
                else:
                    self.mem[self.tar] = value
                self.tar += 4
                continue
            if addr == 0x2D00:
                if read:
                    out.append(self.posted); self.posted = self.csw
                else:
                    self.csw = value
        body = struct.pack("<HBBH", n, 0, 1, len(out)) + b"".join(struct.pack("<I", v) for v in out)
        assert m.RESULT_HEADER + len(body) <= 64, "an answer past the route's max_frame"
        return ok(body)


def test_mem_ap_reads_and_writes_with_raw_transfers_when_the_probe_has_no_block_ops():
    adi_fn = FNS["oep.target.arm-adi"]
    model = RawAdi()

    def describe(p):
        fn = struct.unpack_from("<H", p)[0]
        return ok(b"\x00" + (catalog.ops_tlv([reg.TARGET_ARM_ADI.op["transfer"]]) if fn == adi_fn else b""))
    hst = ScriptedHost({(0, m.OP_CONFIRM): lambda p: ok(struct.pack("<4sBHIBI", b"OEP!", 1, 64, 4096, 8, 0x1234)),
                        (0, m.OP_DESCRIBE): describe, (adi_fn, arm.ArmAdi.TRANSFER): model.transfer})
    adi = arm.ArmAdi(hst, 1, adiv6=True)
    mem = arm.MemAp(adi, 0x2000)
    base = 0x200003C0                                             # 16 words to the next 1 KB
    mem.write_block(base, list(range(40)))
    assert [model.mem[base + 4 * i] for i in range(40)] == list(range(40))
    assert mem.read_block(base, 40) == list(range(40))
    assert mem.read32(base + 4) == 1
    assert {op for _, op, _ in hst.log} == {m.OP_CONFIRM, m.OP_DESCRIBE, arm.ArmAdi.TRANSFER}   # no block op sent
    assert max(model.sizes) <= 64                                  # every request fits the route


# ---- debug §6: no feature bits for arm-adi - a features tag with none set is no defect ---------------------------------

def test_an_arm_adi_features_tag_without_bits_is_not_a_defect():
    from oep_client.conformance_selected_metadata import SelectedMetadataChecks
    from oep_client.virtual_core import tlv
    from test_conformance_selected_metadata import setup as metadata_setup
    model, c = metadata_setup()
    model.extension.metadata[8] = [tlv(3, struct.pack("<H", 8)), tlv(5, b"\0" * 4)]
    r = SelectedMetadataChecks(c, model.metadata_selection).run()
    assert next(row for row in r["checks"] if row["id"] == "OEP-METADATA-8")["status"] == "passed", r["checks"]
