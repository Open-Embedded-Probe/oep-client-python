"""CH32 flash programming from the host with v1 draft parts (oep-spec experiments/flash-primitives F4 / F5).

The probe knows nothing about flash: the host places a RAM loader, block-writes each chunk into a RAM buffer and
runs the loader until its ebreak (oep.target.riscv-dm), then reads everything back. Pages that fail or read back
wrong are written again, up to twice (a DMI transfer is garbled now and then).

  fast-page  CH32X035 / CH32L103 (QingKe V4, 256-byte fast page programming). Loader: oep-spec
             experiments/flash-primitives/x035_loader.S at 0x20000000; a0 = page, a1 = buffer; ebreak at +0xb0.
  v003-wlink CH32V003 (QingKe V2, 64-byte pages). The wlink RAM loader (MIT / Apache-2.0) at 0x20000000; a0 = flags
             (bit0 unlock, bit1 mass erase, bit2 page erase, bit3 program, bit4 verify), a1 = address, a2 = bytes;
             ebreak at +0x15c. One mass erase, then program-only runs, as WCH-LinkE does.
"""

from __future__ import annotations

import struct
import time
from dataclasses import dataclass, field

from . import host as h, riscv, target

# oep-spec experiments/flash-primitives/x035_loader.S, assembled at 0x20000000 (riscv32-esp-elf binutils)
FAST_PAGE_LOADER = bytes.fromhex(
    "b72202403703020023a8620023aaa2001363030423a86200"
    "83a3c20013fe1300e31c0efe13fe030163160e0823a80200"
    "3703010023a86200b70e0800b3ee6e0023a8d20183a3c200"
    "13fe1300e31c0efeb70e0400b3ee6e00130f0500930f0510"
    "83a3050023207f0023a8d20183a3c20013fe1300e31c0efe"
    "93854500130f4f00e310ffff23a8620023aaa200936e0304"
    "23a8d20183a3c20013fe1300e31c0efe13fe030163180e00"
    "23a80200130500007300100023a802003705008033657500"
    "73001000"
)
# oep-probe-arduino src/OepCh32Dm.cpp kV003FlashLoader (from ch32-rs/wlink), little endian
V003_LOADER = bytes.fromhex(
    "111122cc26ca02c89377150099cfb7066745b72702409386"
    "36123797efcdd4c31307b79ad8c3d4d3d8d3937725009dc7"
    "b7270240984bad66373300401367470098cb984b9386a6aa"
    "1367070498cbd847058b63160710984b6d9b98cb93774500"
    "a9cb9307f60399832ec02d6381763ec4b7320040b7270240"
    "1303a3aafd16984bb70302003367770098cb0247d8cb984b"
    "1367070498cbd847058b69e7984b758f98cb024713070704"
    "3ac022477d173ac479f793778500f1cf9307f6032ec09983"
    "372702403ec41c4bc1662d63d58f1ccb3707002013070720"
    "b7270240b7030800b73200401303a3aa944bb3e6760094cb"
    "d447858af5fe8246ba843704040036c2c14636c692468440"
    "110784c2944bc18e94cbd447858ab1ea9246ba84910636c2"
    "b246fd1636c6f9fe8246d4cb944b93e6060494cbd447858a"
    "85eed447c18a85ced847b706f3fffd1613670701d8c7984b"
    "2145758f98cb6244d244710102902320d300f5b523a06200"
    "3db723a0620055b723a06200c1b782469386060436c0a246"
    "fd1636c4b5f2984bb706f3fffd16758f98cb418919e10145"
    "7dbf2ec00d0602c40982b707002032c69387072094431387"
    "4700a24702468a07b2979c436399f602a24782468a07b697"
    "9443c247b6973ec8a24785073ec42246b246ba87e368d6fc"
    "b707002003a70761c247e306f7fa41459db7ffff"
)


@dataclass(frozen=True)
class FlashProfile:
    method: str            # "fast-page" or "v003-wlink"
    base: int = 0x08000000
    size: int = 0          # bytes of flash
    page: int = 256


PROFILES = {
    "x035": FlashProfile("fast-page", size=63488, page=256),
    "l103": FlashProfile("fast-page", size=65536, page=256),
    "v003": FlashProfile("v003-wlink", size=16384, page=64),
}


@dataclass
class ProgramResult:
    bytes: int
    verified: bool
    rewritten_pages: int = 0
    failures: list = field(default_factory=list)
    timings: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"bytes": self.bytes, "verified": self.verified, "rewritten_pages": self.rewritten_pages,
                "failures": self.failures[:5], "timings_seconds": self.timings}


KEYR, CTLR, MODEKEYR = 0x40022004, 0x40022010, 0x40022024
LOCK, FLOCK = 1 << 7, 1 << 15
LOADER, FAST_BUFFER, FAST_DONE = 0x20000000, 0x20000400, 0x200000B0
V003_INPUT, V003_STACK, V003_EBREAK, V003_RUN = 0x20000200, 0x20000800, 0x2000015C, 1024


def _block_size(hst: h.Host) -> int:
    info = target.confirm(hst)
    # request header 6 + session 4 + connection 1 + address 4 + count 2 (the read answer's 5 + done 2 + status 1 is
    # smaller); a little slack for the result header
    return (info["max_frame"] - 5 - 6 - 4 - 1 - 4 - 2) // 4 * 4


def _write_requests(dm: target.RiscvDm, address: int, data: bytes, block: int) -> list[tuple[int, int, bytes]]:
    return [dm.request(dm.WRITE_BLOCK, dm.write_block_body(address + off, data[off:off + block]))
            for off in range(0, len(data), block)]


def _write_ok(r) -> bool:
    """A write_block result that wrote every word: outcome success and status ok (§5.4)."""
    if not r.succeeded:
        return False
    try:
        rd = target.ran(r)
        rd.u16()
        return rd.u8() == riscv.OK
    except h.OepError:
        return False


def _write(dm: target.RiscvDm, address: int, data: bytes, block: int) -> None:
    for r in dm.host.pipeline_calls(_write_requests(dm, address, data, block)):
        if not _write_ok(r):
            raise riscv.TargetError("write_block", target.ran(r).take("HB")[1], r)


def _read(dm: target.RiscvDm, address: int, length: int, block: int) -> bytes:
    """Read back in blocks, pipelined when the host has the link's exchange (reads are independent)."""
    spans = [(address + off, min(block, length - off) // 4) for off in range(0, length, block)]
    out = b""
    for (a, n), r in zip(spans, dm.host.pipeline_calls([dm.request(dm.READ_BLOCK, struct.pack("<IH", a, n))
                                                         for a, n in spans])):
        data, done, status = dm.read_block_result(r)
        if status != riscv.OK or done != n:
            raise riscv.TargetError("read_block", status, r, done=done, data=data)
        out += data
    return out


PIPELINE_PAGES = 16   # pages per pipelined batch: enough to keep the link busy, small enough to show progress


def program(hst: h.Host, dm: target.RiscvDm, image: bytes, profile: FlashProfile) -> ProgramResult:
    """Program `image` at profile.base on a halted, attached target (dm), verify by reading back."""
    image = image + b"\xff" * (-len(image) % profile.page)
    if profile.size and len(image) > profile.size:
        raise ValueError(f"image of {len(image)} bytes exceeds {profile.size}")
    block = _block_size(hst)
    t = {}
    t0 = time.perf_counter()
    if profile.method == "fast-page":
        loader = FAST_PAGE_LOADER
        if dm.read32(CTLR) & (LOCK | FLOCK):
            for reg in (KEYR, MODEKEYR):
                dm.write32(reg, 0x45670123)
                dm.write32(reg, 0xCDEF89AB)
            if dm.read32(CTLR) & (LOCK | FLOCK):
                raise RuntimeError(f"the flash controller stayed locked (CTLR {dm.read32(CTLR):#x})")
    elif profile.method == "v003-wlink":
        loader = V003_LOADER
    else:
        raise ValueError(f"unknown flash method {profile.method!r}")
    loader = loader + b"\0" * (-len(loader) % 4)

    def place_loader() -> None:
        # Read it back before it runs: a garbled loader is the worst garbling there is - it drives the flash
        # controller, still reaches its ebreak, and writes wrong data into every page after it (the CH32L103's
        # RP2350 probe, 2026-09-24: 38 pages rewritten, none right). A probe's ack proves nothing about content.
        for _ in range(3):
            _write(dm, LOADER, loader, block)
            if _read(dm, LOADER, len(loader), block) == loader:
                return
        raise RuntimeError("the RAM loader did not read back after three tries")

    place_loader()

    def page_job(off: int, length: int, flags: int) -> tuple[list, tuple[int, int, bytes], int]:
        """The requests for one page (buffer writes, then the loader run) and the dpc that means success."""
        if profile.method == "fast-page":
            writes = _write_requests(dm, FAST_BUFFER, image[off:off + profile.page], block)
            run = dm.request(dm.RUN, dm.run_body(LOADER, [(0x100A, profile.base + off), (0x100B, FAST_BUFFER),
                                                          (0x0300, 0)], outs=(riscv.REG_A0,)))
            return writes, run, FAST_DONE
        writes = _write_requests(dm, V003_INPUT, image[off:off + length], block)
        run = dm.request(dm.RUN, dm.run_body(LOADER, [(0x100A, flags), (0x100B, profile.base + off), (0x100C, length),
                                                      (0x1002, V003_STACK), (0x0300, 0)], timeout_ms=1000,
                                                     outs=(riscv.REG_A0,)))
        return writes, run, V003_EBREAK

    def run_pages(jobs: list[tuple[int, int, int]]) -> list[dict]:
        """Pipelined in batches: the probe runs requests in order, so each page's buffer writes land before its
        run. A page whose write or run did not work is reported; the read-back catches anything else."""
        failures = []
        for at in range(0, len(jobs), PIPELINE_PAGES):
            batch = [page_job(*job) for job in jobs[at:at + PIPELINE_PAGES]]
            reqs = [r for writes, run, _ in batch for r in writes + [run]]
            results = iter(hst.pipeline(reqs))
            for (off, _, _), (writes, _, done_pc) in zip(jobs[at:at + PIPELINE_PAGES], batch):
                wrote = all([_write_ok(next(results)) for _ in writes])
                r = next(results)
                if not (wrote and r.ran):
                    failures.append({"address": hex(profile.base + off), "dpc": None})
                    continue
                run = dm.run_result(r, 1)
                a0 = run.values[0]
                if not (r.succeeded and run.status == riscv.OK and run.stopped and run.dpc == done_pc
                        and (profile.method != "fast-page" or a0 == 0)):
                    failures.append({"address": hex(profile.base + off), "dpc": hex(run.dpc)})
        return failures

    if profile.method == "fast-page":
        failures = run_pages([(off, profile.page, 0) for off in range(0, len(image), profile.page)])
    else:
        run = dm.run(LOADER, [(0x100A, 0x03), (0x100B, profile.base), (0x100C, 0),
                              (0x1002, V003_STACK), (0x0300, 0)], timeout_ms=1000)   # unlock + mass erase
        if not (run.stopped and run.dpc == V003_EBREAK):
            raise RuntimeError(f"mass erase did not stop on the loader's ebreak (dpc {run.dpc:#x})")
        failures = run_pages([(off, min(V003_RUN, len(image) - off), 0x09) for off in range(0, len(image), V003_RUN)])
    t["program"] = round(time.perf_counter() - t0, 3)
    t0 = time.perf_counter()
    back = _read(dm, profile.base, len(image), block)
    t["verify"] = round(time.perf_counter() - t0, 3)
    rewritten = 0
    for _ in range(2):
        bad = sorted({int(f["address"], 16) - profile.base for f in failures} |
                     {off for off in range(0, len(image), profile.page)
                      if back[off:off + profile.page] != image[off:off + profile.page]})
        if not bad:
            break
        place_loader()   # the pages came out wrong: the loader itself may have been hit, place it again
        rewritten += len(bad)
        failures = run_pages([(off - off % profile.page, profile.page, 0x1D) for off in bad])
        back = _read(dm, profile.base, len(image), block)
    return ProgramResult(len(image), back == image, rewritten, failures, t)
