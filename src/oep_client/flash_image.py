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
    for offset in range(0, length, 32):
        count = min(32, length - offset)
        result = memory.read(address + offset, count)
        if not isinstance(result, bytes):
            raise RuntimeError(
                f"memory read failed at 0x{address + offset:08x}: {result}")
        output += result
        if progress:
            progress("read", offset + count, length)
    return bytes(output)


def program_image(memory: TargetMemoryClient, flash: TargetFlashClient,
                  address: int, desired: bytes, retries: int = 2,
                  progress: Progress | None = None) -> ProgramSummary:
    if address & 63 or not desired or len(desired) & 63:
        raise ValueError("image and address must be 64-byte aligned")
    current = read_range(memory, address, len(desired), progress)
    changed = [offset for offset in range(0, len(desired), 64)
               if current[offset:offset + 64] != desired[offset:offset + 64]]
    attempts = 0
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
