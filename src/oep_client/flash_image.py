"""Whole-image helpers for the destructive OEP prototype."""

from dataclasses import dataclass
from typing import Callable

from .prototype import FunctionResult, TargetControlClient, TargetFlashClient, TargetMemoryClient


Progress = Callable[[str, int, int], None]


@dataclass(frozen=True)
class ProgramSummary:
    bytes_compared: int
    pages_programmed: int
    attempts: int


@dataclass(frozen=True)
class X035Preflight:
    chip_id: int
    option_bytes: int
    write_protection: int


X035_F8U6_CHIP_ID = 0x035E0601
X035_ESIG_CHIP_ID = 0x1FFFF704
X035_FLASH_OBR = 0x4002201C
X035_FLASH_WPR = 0x40022020
X035_FLASH_BYTES = 63488


def padded_image(data: bytes, size: int, fill: int = 0xFF) -> bytes:
    if size <= 0 or size & 63:
        raise ValueError("flash size must be a positive multiple of 64")
    if len(data) > size:
        raise ValueError("image exceeds configured flash size")
    if not 0 <= fill <= 0xFF:
        raise ValueError("fill must be a byte")
    return data + bytes((fill,)) * (size - len(data))


def read_range(memory: TargetMemoryClient, address: int, length: int,
               progress: Progress | None = None) -> bytes:
    if address & 3 or length <= 0 or length & 3:
        raise ValueError("range must be non-empty and 4-byte aligned")
    output = bytearray()
    for offset in range(0, length, 88):
        count = min(88, length - offset)
        result = memory.read(address + offset, count)
        if not isinstance(result, bytes):
            raise RuntimeError(
                f"memory read failed at 0x{address + offset:08x}: {result}")
        output += result
        if progress:
            progress("read", offset + count, length)
    return bytes(output)


def preflight_x035_f8u6(memory: TargetMemoryClient, flash_base: int,
                        flash_size: int) -> X035Preflight:
    """Fail closed before destructive X035F8U6 image programming."""
    if flash_base != 0x08000000 or flash_size != X035_FLASH_BYTES:
        raise ValueError(
            "X035F8U6 requires flash base 0x08000000 and size 63488")

    def read_word(address: int) -> int:
        result = memory.read(address, 4)
        if not isinstance(result, bytes) or len(result) != 4:
            raise RuntimeError(f"preflight read failed at 0x{address:08x}: {result}")
        return int.from_bytes(result, "little")

    chip_id = read_word(X035_ESIG_CHIP_ID)
    option_bytes = read_word(X035_FLASH_OBR)
    write_protection = read_word(X035_FLASH_WPR)
    if chip_id != X035_F8U6_CHIP_ID:
        raise RuntimeError(
            f"refusing destructive write: expected CH32X035F8U6 "
            f"0x{X035_F8U6_CHIP_ID:08x}, got 0x{chip_id:08x}")
    if option_bytes & 0x2:
        raise RuntimeError("refusing destructive write: read protection is enabled")
    if write_protection != 0xFFFFFFFF:
        raise RuntimeError(
            f"refusing destructive write: write protection is 0x{write_protection:08x}")
    return X035Preflight(chip_id, option_bytes, write_protection)


def program_image(memory: TargetMemoryClient, flash: TargetFlashClient,
                  address: int, desired: bytes, retries: int = 2,
                  progress: Progress | None = None) -> ProgramSummary:
    if address & 63 or not desired or len(desired) & 63:
        raise ValueError("image and address must be 64-byte aligned")
    current = read_range(memory, address, len(desired), progress)
    changed = [offset for offset in range(0, len(desired), 64)
               if current[offset:offset + 64] != desired[offset:offset + 64]]
    attempts = 0
    # Revision-2 probes accept a complete physical erase page as four staged
    # transport-sized fragments and commit it once.  Keep the old 64-byte
    # path for lightweight fakes and earlier probe firmware.
    if hasattr(flash, "stage_page64") and hasattr(flash, "commit_page256"):
        changed_physical = [offset for offset in range(0, len(desired), 256)
                            if current[offset:offset + 256] != desired[offset:offset + 256]]
        for index, offset in enumerate(changed_physical, 1):
            for fragment in range(0, 256, 64):
                staged = flash.stage_page64(
                    address + offset + fragment, desired[offset + fragment:offset + fragment + 64])
                if not staged.succeeded:
                    raise RuntimeError(
                        f"flash stage failed at 0x{address + offset + fragment:08x}: {staged}")
            last: FunctionResult | None = None
            for _ in range(retries + 1):
                attempts += 1
                last = flash.commit_page256(address + offset)
                if last.succeeded:
                    break
            else:
                raise RuntimeError(
                    f"flash commit failed at 0x{address + offset:08x}: {last}")
            if progress:
                progress("program", index, len(changed_physical))
        return ProgramSummary(len(desired), len(changed_physical), attempts)

    for index, offset in enumerate(changed, 1):
        last: FunctionResult | None = None
        for _ in range(retries + 1):
            attempts += 1
            last = flash.program_page64(
                address + offset, desired[offset:offset + 64])
            if last.succeeded:
                break
        else:
            raise RuntimeError(
                f"flash program failed at 0x{address + offset:08x}: {last}")
        if progress:
            progress("program", index, len(changed))
    return ProgramSummary(len(desired), len(changed), attempts)


def verify_image(memory: TargetMemoryClient, address: int, expected: bytes,
                 progress: Progress | None = None) -> None:
    actual = read_range(memory, address, len(expected), progress)
    if actual == expected:
        return
    first = next(i for i, pair in enumerate(zip(actual, expected))
                 if pair[0] != pair[1])
    raise RuntimeError(
        f"verify mismatch at 0x{address + first:08x}: "
        f"actual=0x{actual[first]:02x} expected=0x{expected[first]:02x}")


def reset_target(target: TargetControlClient) -> None:
    result = target.normalize_user()
    if not result.succeeded:
        raise RuntimeError(f"target reset failed: {result}")
