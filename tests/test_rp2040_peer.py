"""rp2040_peer without hardware: the ELF's flash image, the RTT rings on a model memory, addresses, the OpenOCD hook."""
import struct

import pytest

from oep_client import rp2040_peer as rp


def elf(segments):
    """A 32-bit little-endian ELF with PT_LOAD segments [(paddr, bytes)]."""
    ph, data, off = b"", b"", 52 + 32 * len(segments)
    for paddr, b in segments:
        ph += struct.pack("<8I", 1, off + len(data), paddr, paddr, len(b), len(b), 5, 4)
        data += b
    head = b"\x7fELF" + bytes([1, 1, 1]) + bytes(9) + struct.pack("<HHIIIIIHHHHHH", 2, 40, 1, 0, 52, 0, 0, 52, 32,
                                                                       len(segments), 40, 0, 0)
    return head + ph + data


def test_image_of_an_elf_lays_out_the_flash_segments(tmp_path):
    p = tmp_path / "a.elf"
    p.write_bytes(elf([(0x10000000, b"\x01\x02"), (0x10000010, b"\x03"), (0x20000000, b"ram")]))
    img = rp.image_of(str(p))
    assert img == b"\x01\x02" + b"\xff" * 14 + b"\x03"          # the gap erased, RAM's segment left out
    b = tmp_path / "a.bin"
    b.write_bytes(b"raw")
    assert rp.image_of(str(b)) == b"raw"


class Mem:
    def __init__(self):
        self.words = {}

    def read32(self, a):
        return self.words.get(a, 0)

    def write32(self, a, v):
        self.words[a] = v & 0xFFFFFFFF

    def read_block(self, a, n):
        return [self.read32(a + 4 * i) for i in range(n)]

    def put(self, a, data):
        data = data + b"\0" * (-len(data) % 4)
        for i in range(0, len(data), 4):
            self.write32(a + i, struct.unpack_from("<I", data, i)[0])

    def get(self, a, n):
        return struct.pack(f"<{(n + 3) // 4}I", *self.read_block(a, (n + 3) // 4))[:n]


def rtt_model(cb=0x20001000, up=0x20002000, down=0x20003000, size=16):
    m = Mem()
    m.put(cb, rp.RTT_ID)
    m.put(cb + 16, struct.pack("<II", 1, 1))
    m.put(cb + 24, struct.pack("<6I", 0, up, size, 0, 0, 0))
    m.put(cb + 48, struct.pack("<6I", 0, down, size, 0, 0, 0))
    return m, cb


def test_rtt_reads_the_up_ring_across_its_end_and_writes_the_down_ring():
    m, cb = rtt_model()
    r = rp.Rtt(m, wait=0)
    assert r.cb == cb and r.up_size == 16
    m.put(0x20002000, b"0123456789abcdef")
    m.write32(cb + 24 + 16, 12)                                # RdOff 12
    m.write32(cb + 24 + 12, 4)                                 # WrOff 4: wrapped
    assert r.read() == b"cdef0123"
    assert m.read32(cb + 24 + 16) == 4 and r.read() == b""
    assert r.write(b"hi") == 2
    assert m.get(0x20003000, 2) == b"hi" and m.read32(cb + 48 + 12) == 2
    m.write32(cb + 48 + 16, 2)                                 # the target has read both
    assert r.write(b"x" * 40) == 15                            # a full ring keeps one slot empty
    assert m.read32(cb + 48 + 12) == (2 + 15) % 16


def test_rtt_up_buffer_need_not_be_word_aligned():
    m, cb = rtt_model(up=0x20002001)
    r = rp.Rtt(m, wait=0)
    m.put(0x20002000, b"_0123456789abcdef")
    m.write32(cb + 24 + 12, 5)
    assert r.read() == b"01234"


def test_no_control_block_is_an_error():
    with pytest.raises(RuntimeError):
        rp.Rtt(Mem(), wait=0)


def test_address_and_openocd_hook(tmp_path):
    assert rp.parse("oep://80f1b2d835d6/pico") == ("80f1b2d835d6", "pico")
    for bad in ("oep://80f1b2d835d6", "tcp://x/pico", "oep:///pico"):
        with pytest.raises(ValueError):
            rp.parse(bad)
    paths = rp.write_openocd_hook(str(tmp_path), "TEST_SERIAL_PORT_PEER_PICO", python="/usr/bin/python3")
    target = (tmp_path / "target/rp2040.cfg").read_text()
    assert len(paths) == 2 and (tmp_path / "interface/cmsis-dap.cfg").exists()
    assert "proc program" in target and "$::env(TEST_SERIAL_PORT_PEER_PICO)" in target
    assert "/usr/bin/python3 -m oep_client.rp2040_peer flash" in target


def test_clear_rtt_ids_wipes_the_old_blocks():
    m, cb = rtt_model()
    m.put(0x20010000, rp.RTT_ID)
    assert rp.clear_rtt_ids(m) == 2
    with pytest.raises(RuntimeError):
        rp.Rtt(m, wait=0)
