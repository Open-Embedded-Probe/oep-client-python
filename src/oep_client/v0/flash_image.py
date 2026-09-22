"""Whole-image operations on top of target.control/memory/flash: preflight, diff, program, verify."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from . import codec
from .client import Client, RequestError
from .services import TargetControl, TargetFlash, TargetMemory

# CH32X03x electronic signature (device-data evidence/device_ids.csv): chip id -> (part, flash bytes)
X035_ESIG_CHIP_ID = 0x1FFFF704
X035_PARTS = {
    0x03510601: ("CH32X035C8T6", 63488),
    0x03570601: ("CH32X035F7P6", 49152),
    0x035E0601: ("CH32X035F8U6", 63488),
    0x035B0601: ("CH32X035G8R6", 63488),
    0x03560601: ("CH32X035G8U6", 63488),
    0x03500601: ("CH32X035R8T6", 63488),
    0x035A0601: ("CH32X033F8P6", 63488),
}
X035_FLASH_OBR = 0x4002201C
X035_FLASH_WPR = 0x40022020


@dataclass
class ImageResult:
    part: str
    chip_id: int
    geometry: tuple[int, int, int]      # base, size, page
    pages_total: int
    pages_changed: int
    pages_failed: list[int]
    image_crc32: int
    verified_crc32: int
    timings: dict[str, float] = field(default_factory=dict)

    @property
    def verified(self) -> bool:
        return not self.pages_failed and self.image_crc32 == self.verified_crc32

    def as_dict(self) -> dict:
        return {"part": self.part, "chip_id": f"0x{self.chip_id:08x}",
                "flash": {"base": f"0x{self.geometry[0]:08x}", "size": self.geometry[1], "page": self.geometry[2]},
                "pages_total": self.pages_total, "pages_changed": self.pages_changed, "pages_failed": self.pages_failed,
                "image_crc32": f"0x{self.image_crc32:08x}", "verified_crc32": f"0x{self.verified_crc32:08x}",
                "verified": self.verified, "timings_seconds": self.timings}


class Target:
    """Bundle of the three target services found on a confirmed client."""

    def __init__(self, client: Client):
        client.confirm()
        client.list_functions()
        found = {}
        for definition, cls in ((codec.DEF_TARGET_CONTROL, TargetControl), (codec.DEF_TARGET_MEMORY, TargetMemory),
                                (codec.DEF_TARGET_FLASH, TargetFlash)):
            function = client.find(*definition[:2])
            if function is None:
                raise RequestError(f"probe does not offer definition {definition[:2]}")
            found[cls] = cls(client, function.function)
        self.client = client
        self.control: TargetControl = found[TargetControl]
        self.memory: TargetMemory = found[TargetMemory]
        self.flash: TargetFlash = found[TargetFlash]

    def preflight(self) -> tuple[str, int, int]:
        """Halt, identify the X035 part and check protection. Fail closed."""
        self.control.halt()
        chip_id = self.memory.read_word(X035_ESIG_CHIP_ID)
        if chip_id not in X035_PARTS:
            raise RequestError(f"unknown CH32X03x chip id 0x{chip_id:08x}; refusing destructive operations")
        part, flash_size = X035_PARTS[chip_id]
        geometry = self.flash.geometry()
        if geometry.size != flash_size:
            raise RequestError(f"probe geometry {geometry.size} bytes does not match {part} ({flash_size} bytes)")
        obr = self.memory.read_word(X035_FLASH_OBR)
        wpr = self.memory.read_word(X035_FLASH_WPR)
        if obr & 0x2:
            raise RequestError("read protection is enabled; refusing")
        if wpr != 0xFFFFFFFF:
            raise RequestError(f"write protection 0x{wpr:08x} is active; refusing")
        return part, chip_id, flash_size


def padded(image: bytes, size: int, fill: int = 0xFF) -> bytes:
    if len(image) > size:
        raise ValueError(f"image {len(image)} bytes exceeds flash {size} bytes")
    return image + bytes((fill,)) * (size - len(image))


def program_image(target: Target, image: bytes, *, verify: bool = True, reset: bool = True) -> ImageResult:
    """Diff against the current flash, program changed physical pages (pipelined), then reset and CRC-verify."""
    timings: dict[str, float] = {}
    t = time.perf_counter()
    part, chip_id, _ = target.preflight()
    geometry = target.flash.geometry()
    timings["preflight"] = time.perf_counter() - t
    desired = padded(image, geometry.size)

    t = time.perf_counter()
    current = target.memory.read_range(geometry.base, geometry.size)
    timings["read_current"] = time.perf_counter() - t
    pages = [(geometry.base + off, desired[off:off + geometry.page])
             for off in range(0, geometry.size, geometry.page)
             if current[off:off + geometry.page] != desired[off:off + geometry.page]]

    t = time.perf_counter()
    failed = target.flash.program_pages(pages) if pages else []
    timings["program"] = time.perf_counter() - t

    verified = 0
    if verify:
        t = time.perf_counter()
        verified = target.flash.verify_crc32(geometry.base, geometry.size)
        timings["verify"] = time.perf_counter() - t
    if reset:
        t = time.perf_counter()
        target.control.reset()
        timings["reset"] = time.perf_counter() - t
    return ImageResult(part, chip_id, (geometry.base, geometry.size, geometry.page),
                       geometry.size // geometry.page, len(pages), failed,
                       TargetFlash.crc32(desired), verified if verify else TargetFlash.crc32(desired), timings)


def verify_image(target: Target, image: bytes, *, reset: bool = True) -> ImageResult:
    timings: dict[str, float] = {}
    t = time.perf_counter()
    part, chip_id, _ = target.preflight()
    geometry = target.flash.geometry()
    timings["preflight"] = time.perf_counter() - t
    desired = padded(image, geometry.size)
    t = time.perf_counter()
    verified = target.flash.verify_crc32(geometry.base, geometry.size)
    timings["verify"] = time.perf_counter() - t
    if reset:
        t = time.perf_counter()
        target.control.reset()
        timings["reset"] = time.perf_counter() - t
    return ImageResult(part, chip_id, (geometry.base, geometry.size, geometry.page), geometry.size // geometry.page,
                       0, [], TargetFlash.crc32(desired), verified, timings)


def read_image(target: Target, *, reset: bool = True) -> tuple[bytes, dict[str, float]]:
    timings: dict[str, float] = {}
    t = time.perf_counter()
    target.preflight()
    geometry = target.flash.geometry()
    data = target.memory.read_range(geometry.base, geometry.size)
    timings["read"] = time.perf_counter() - t
    if reset:
        t = time.perf_counter()
        target.control.reset()
        timings["reset"] = time.perf_counter() - t
    return data, timings
