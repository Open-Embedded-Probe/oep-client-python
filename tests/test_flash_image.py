import pytest

from oep_client.flash_image import padded_image, program_image, read_range, verify_image
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


def test_verify_reports_first_mismatch():
    with pytest.raises(RuntimeError, match="0x00000001"):
        verify_image(Memory(b"aXcd"), 0, b"abcd")
