import pytest

from oep_client.flash_image import (
    X035_ESIG_CHIP_ID, X035_FLASH_OBR, X035_FLASH_WPR, padded_image,
    preflight_x035_f8u6, program_image, read_range, verify_image,
)
from oep_client.prototype import FunctionResult, RESOLUTION_COMPLETED, OUTCOME_FAILED, OUTCOME_SUCCESS


class Memory:
    def __init__(self, data: bytes):
        self.data = bytearray(data)

    def read(self, address: int, length: int):
        return bytes(self.data[address:address + length])


class Flash:
    def __init__(self, memory: Memory, failures: int = 0):
        self.memory = memory
        self.failures = failures
        self.calls = 0

    def program_page64(self, address: int, data: bytes):
        self.calls += 1
        if self.failures:
            self.failures -= 1
            return FunctionResult(RESOLUTION_COMPLETED, 0x0103, OUTCOME_FAILED, b"\xe1")
        self.memory.data[address:address + 64] = data
        return FunctionResult(RESOLUTION_COMPLETED, 0x0103, OUTCOME_SUCCESS, b"")


class StagedFlash(Flash):
    def __init__(self, memory: Memory):
        super().__init__(memory)
        self.staged: dict[int, bytearray] = {}
        self.stage_calls = 0
        self.commit_calls = 0

    def stage_page64(self, address: int, data: bytes):
        self.stage_calls += 1
        page = address & ~255
        image = self.staged.setdefault(page, bytearray(256))
        image[address - page:address - page + 64] = data
        return FunctionResult(RESOLUTION_COMPLETED, 0x0103, OUTCOME_SUCCESS, b"")

    def commit_page256(self, address: int):
        self.commit_calls += 1
        self.memory.data[address:address + 256] = self.staged[address]
        return FunctionResult(RESOLUTION_COMPLETED, 0x0103, OUTCOME_SUCCESS, b"")


def test_padding_and_bounds():
    assert padded_image(b"abc", 64)[:5] == b"abc\xff\xff"
    with pytest.raises(ValueError):
        padded_image(bytes(65), 64)
    with pytest.raises(ValueError):
        padded_image(b"", 65)


def test_read_program_retry_and_verify():
    memory = Memory(bytes(128))
    desired = bytes(64) + bytes(range(64))
    flash = Flash(memory, failures=1)
    summary = program_image(memory, flash, 0, desired)
    assert summary.pages_programmed == 1
    assert summary.attempts == 2
    assert flash.calls == 2
    verify_image(memory, 0, desired)
    assert read_range(memory, 0, 128) == desired


def test_program_groups_changed_fragments_by_physical_page():
    memory = Memory(bytes(256))
    desired = bytes(range(256))
    flash = StagedFlash(memory)
    summary = program_image(memory, flash, 0, desired)
    assert summary.pages_programmed == 1
    assert summary.attempts == 1
    assert flash.stage_calls == 4
    assert flash.commit_calls == 1
    assert bytes(memory.data) == desired


def test_verify_reports_first_mismatch():
    with pytest.raises(RuntimeError, match="0x00000001"):
        verify_image(Memory(b"aXcd"), 0, b"abcd")


class RegisterMemory:
    def __init__(self, words: dict[int, int]):
        self.words = words

    def read(self, address: int, length: int):
        assert length == 4
        return self.words[address].to_bytes(4, "little")


def test_x035_f8u6_preflight_accepts_unprotected_target():
    memory = RegisterMemory({
        X035_ESIG_CHIP_ID: 0x035E0601,
        X035_FLASH_OBR: 0x03FFFFFC,
        X035_FLASH_WPR: 0xFFFFFFFF,
    })
    result = preflight_x035_f8u6(memory, 0x08000000, 63488)
    assert result.chip_id == 0x035E0601


def test_x035_f8u6_preflight_rejects_wrong_target_or_protection():
    memory = RegisterMemory({
        X035_ESIG_CHIP_ID: 0x03510601,
        X035_FLASH_OBR: 0x03FFFFFC,
        X035_FLASH_WPR: 0xFFFFFFFF,
    })
    with pytest.raises(RuntimeError, match="expected CH32X035F8U6"):
        preflight_x035_f8u6(memory, 0x08000000, 63488)
    memory.words[X035_ESIG_CHIP_ID] = 0x035E0601
    memory.words[X035_FLASH_OBR] = 0x03FFFFFE
    with pytest.raises(RuntimeError, match="read protection"):
        preflight_x035_f8u6(memory, 0x08000000, 63488)
