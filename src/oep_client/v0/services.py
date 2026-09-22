"""Typed wrappers for the v0 standard definitions. Each takes the offered-function reference."""

from __future__ import annotations

from . import codec
from .client import Client


class ProbeIdentity:
    DEFINITION = codec.DEF_PROBE_IDENTITY

    def __init__(self, client: Client, function: int):
        self.client, self.function = client, function

    def get(self) -> codec.ProbeIdentityGetResult:
        response = self.client.call(self.function, codec.PROBE_IDENTITY_OP_GET, codec.ProbeIdentityGetRequest().pack())
        return codec.ProbeIdentityGetResult.unpack(response.expect_success("probe.identity get"))


def _crc32(data: bytes) -> int:
    import zlib
    return zlib.crc32(data) & 0xFFFFFFFF


class TargetControl:
    DEFINITION = codec.DEF_TARGET_CONTROL

    def __init__(self, client: Client, function: int):
        self.client, self.function = client, function

    def status(self) -> codec.TargetControlStatusResult:
        response = self.client.call(self.function, codec.TARGET_CONTROL_OP_STATUS)
        return codec.TargetControlStatusResult.unpack(response.expect_success("target.control status"))

    def halt(self) -> None:
        self.client.call(self.function, codec.TARGET_CONTROL_OP_HALT).expect_success("target.control halt")

    def resume(self) -> None:
        self.client.call(self.function, codec.TARGET_CONTROL_OP_RESUME).expect_success("target.control resume")

    def reset(self, mode: int = 0) -> None:
        self.client.call(self.function, codec.TARGET_CONTROL_OP_RESET,
                         codec.TargetControlResetRequest(mode=mode).pack()).expect_success("target.control reset")


class TargetMemory:
    DEFINITION = codec.DEF_TARGET_MEMORY

    def __init__(self, client: Client, function: int):
        self.client, self.function = client, function

    @property
    def chunk(self) -> int:
        # result = 5-byte header + data; keep it 4-aligned and within max_frame
        return (self.client.limits.max_frame - 5) & ~3

    def read(self, address: int, length: int) -> bytes:
        response = self.client.call(self.function, codec.TARGET_MEMORY_OP_READ,
                                    codec.TargetMemoryReadRequest(address=address, length=length).pack())
        return codec.TargetMemoryReadResult.unpack(response.expect_success("target.memory read")).data

    def read_word(self, address: int) -> int:
        return int.from_bytes(self.read(address, 4), "little")

    def read_range(self, address: int, length: int) -> bytes:
        """Pipelined read honoring the probe's window; results arrive in order."""
        chunk = self.chunk
        requests = [(self.function, codec.TARGET_MEMORY_OP_READ,
                     codec.TargetMemoryReadRequest(address=address + off, length=min(chunk, length - off)).pack())
                    for off in range(0, length, chunk)]
        out = bytearray()
        for response in self.client.pipeline(requests):
            out += codec.TargetMemoryReadResult.unpack(response.expect_success("target.memory read")).data
        return bytes(out)

    def write(self, address: int, data: bytes) -> int:
        response = self.client.call(self.function, codec.TARGET_MEMORY_OP_WRITE,
                                    codec.TargetMemoryWriteRequest(address=address, data=data).pack())
        return codec.TargetMemoryWriteResult.unpack(response.expect_success("target.memory write")).written


class TargetFlash:
    DEFINITION = codec.DEF_TARGET_FLASH

    def __init__(self, client: Client, function: int):
        self.client, self.function = client, function

    def geometry(self) -> codec.TargetFlashGeometryResult:
        response = self.client.call(self.function, codec.TARGET_FLASH_OP_GEOMETRY)
        return codec.TargetFlashGeometryResult.unpack(response.expect_success("target.flash geometry"))

    def erase_page(self, address: int) -> None:
        self.client.call(self.function, codec.TARGET_FLASH_OP_ERASE_PAGE,
                         codec.TargetFlashErasePageRequest(address=address).pack()).expect_success("target.flash erase")

    def program_page(self, address: int, data: bytes) -> None:
        self.client.call(self.function, codec.TARGET_FLASH_OP_PROGRAM_PAGE,
                         codec.TargetFlashProgramPageRequest(address=address, data=data).pack()).expect_success("target.flash program")

    def program_pages(self, pages: list[tuple[int, bytes]]) -> list[int]:
        """Pipelined page programs. Returns the addresses whose result was not success."""
        requests = [(self.function, codec.TARGET_FLASH_OP_PROGRAM_PAGE,
                     codec.TargetFlashProgramPageRequest(address=a, data=d).pack()) for a, d in pages]
        failed = []
        for (address, _), response in zip(pages, self.client.pipeline(requests)):
            if not response.succeeded:
                failed.append(address)
        return failed

    def verify_crc32(self, address: int, length: int) -> int:
        response = self.client.call(self.function, codec.TARGET_FLASH_OP_VERIFY_CRC32,
                                    codec.TargetFlashVerifyCrc32Request(address=address, length=length).pack())
        return codec.TargetFlashVerifyCrc32Result.unpack(response.expect_success("target.flash verify")).crc32

    @staticmethod
    def crc32(data: bytes) -> int:
        return _crc32(data)
