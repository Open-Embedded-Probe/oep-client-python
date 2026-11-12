"""gdb_server: gdb's remote protocol against a fake Cortex-M (no probe); and the real arm-none-eabi-gdb when installed."""

from __future__ import annotations

import binascii
import glob
import os
import socket
import struct
import subprocess
import threading

import pytest

from oep_client import gdb_server as gs


class FakeCore:
    """Registers, sparse memory, an FPB of 4 and a run that stops at the first breakpoint."""

    FLASH, RAM = 0x10000000, 0x20000000

    def __init__(self):
        self.regs = [0] * gs.NREGS
        self.regs[15], self.regs[16] = 0x10000100, 0x01000000
        self.mem: dict[int, int] = {}
        self.running = False
        self.bp: list[int] = []
        self.flash_writes: list[tuple[int, int]] = []
        self.opened = self.closed = False
        self.keepalives = 0
        self.events: list[str] = []

    def open(self): self.opened = True
    def close(self): self.closed = True
    def keepalive(self): self.keepalives += 1

    def halted(self):
        if self.running and self.bp:
            self.regs[15] = self.bp[0]
            self.running = False
        return not self.running

    def halt(self):
        self.running = False
        self.events.append("halt")

    def resume(self):
        self.running = True
        self.events.append("resume")

    def step(self):
        self.regs[15] += 2

    def reset_halt(self):
        self.running = False
        self.regs[15] = 0xEE
        self.events.append("reset halt")

    def reset_run(self):
        self.running = True
        self.events.append("reset run")

    def read_registers(self): return list(self.regs)
    def write_register(self, n, v): self.regs[n] = v

    def read_memory(self, a, n): return bytes(self.mem.get(a + i, 0) for i in range(n))

    def write_memory(self, a, data):
        for i, b in enumerate(data):
            self.mem[a + i] = b

    def memory_map(self): return gs.Rp2040Debug.memory_map(gs.Rp2040Debug("oep://fafe00000001/pico"))

    def write_flash(self, a, image, log):
        self.flash_writes.append((a, len(image)))
        self.write_memory(a, image)

    def set_breakpoint(self, a):
        if len(self.bp) >= 4 or a >= 0x20000000:
            return False
        self.bp.append(a)
        return True

    def clear_breakpoint(self, a):
        if a in self.bp:
            self.bp.remove(a)
            return True
        return False


@pytest.fixture
def served():
    fake = FakeCore()
    srv = gs.Server(lambda: fake, port=0, log=lambda m: None)
    stop = threading.Event()
    th = threading.Thread(target=srv.serve_forever, args=(stop,), daemon=True)
    th.start()
    yield fake, srv.port
    stop.set()
    srv.close()
    th.join(2)


class Client:
    def __init__(self, port):
        self.s = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.buf = b""

    def cmd(self, body: bytes) -> bytes:
        self.s.sendall(gs.frame(body))
        while True:
            i = self.buf.find(b"$")
            j = self.buf.find(b"#", i) if i >= 0 else -1
            if i >= 0 and j >= 0 and len(self.buf) >= j + 3:
                out, self.buf = self.buf[i + 1:j], self.buf[j + 3:]
                self.s.sendall(b"+")
                if out.startswith(b"O") and out != b"OK":
                    continue                      # console output before the answer
                return out
            self.buf += self.s.recv(65536)


def test_protocol(served):
    fake, port = served
    fake.running = True
    c = Client(port)
    assert b"qXfer:memory-map:read+" in c.cmd(b"qSupported:multiprocess+")
    assert c.cmd(b"?") == b"T02" and "halt" in fake.events      # a running core is stopped on connect (SIGINT)
    xml = c.cmd(b"qXfer:features:read:target.xml:0,fff")
    assert xml.startswith(b"l") and b"m-profile" in xml
    regs = binascii.unhexlify(c.cmd(b"g"))
    assert struct.unpack_from("<I", regs, 15 * 4)[0] == 0x10000100
    assert c.cmd(b"P0=78563412") == b"OK" and fake.regs[0] == 0x12345678
    assert c.cmd(b"M20000000,4:01020304") == b"OK" and c.cmd(b"m20000000,4") == b"01020304"
    assert c.cmd(b"X20000004,3:" + b"a}]c") == b"OK" and fake.read_memory(0x20000004, 3) == b"a}c"
    assert c.cmd(b"Z1,10000200,2") == b"OK"
    assert c.cmd(b"vCont;c") == b"T05" and fake.regs[15] == 0x10000200
    assert c.cmd(b"z1,10000200,2") == b"OK"
    assert c.cmd(b"vCont;s") == b"T05" and fake.regs[15] == 0x10000202
    assert c.cmd(b"qRcmd," + binascii.hexlify(b"reset halt")) == b"OK" and fake.regs[15] == 0xEE
    assert c.cmd(b"vFlashErase:10000000,1000") == b"OK"
    assert c.cmd(b"vFlashWrite:10000000:" + b"\x01" * 16) == b"OK"
    assert c.cmd(b"vFlashWrite:10000010:" + b"\x02" * 16) == b"OK"
    assert c.cmd(b"vFlashWrite:10000024:" + b"\x03" * 4) == b"OK"         # a gap of 4 bytes, as sections leave
    assert c.cmd(b"vFlashDone") == b"OK" and fake.flash_writes == [(0x10000000, 40)]
    assert fake.read_memory(0x10000020, 8) == b"\xff" * 4 + b"\x03" * 4
    assert c.cmd(b"D") == b"OK"
    c.s.close()


def _gdb() -> str | None:
    found = sorted(glob.glob(os.path.expanduser("~/.arduino15/internal/rp2040_pqt-gcc_*/bin/arm-none-eabi-gdb")) +
                   glob.glob(os.path.expanduser("~/.arduino15/packages/rp2040/tools/pqt-gcc/*/bin/arm-none-eabi-gdb")))
    return found[-1] if found else None


@pytest.mark.skipif(_gdb() is None, reason="arm-none-eabi-gdb (arduino-pico's) is not installed here")
def test_real_gdb(served, tmp_path):
    """cortex-debug's external server steps with arduino-pico's gdb: connect, reset halt, load, break, run, step."""
    fake, port = served
    elf = tmp_path / "t.elf"
    elf.write_bytes(_tiny_elf())
    script = tmp_path / "cmds.gdb"
    script.write_text(f"""set pagination off
set confirm off
file {elf}
target extended-remote 127.0.0.1:{port}
monitor reset halt
load
monitor reset halt
hbreak *0x10000010
continue
stepi
info registers pc
x/2xw 0x10000000
detach
""")
    r = subprocess.run([_gdb(), "-nx", "-batch", "-x", str(script)], capture_output=True, text=True, timeout=60)
    out = r.stdout + r.stderr
    assert r.returncode == 0, out
    assert fake.flash_writes and fake.flash_writes[0][0] == 0x10000000, out
    assert "0x10000012" in out, out                       # stopped at the breakpoint, one step on
    assert "0x11111111" in out.lower() or "0x11111111" in out, out
    assert fake.closed


def _tiny_elf() -> bytes:
    """A 32-bit ARM ELF: one .text section (gdb's load reads sections) of 32 bytes at 0x10000000, in one segment."""
    code = struct.pack("<8I", *([0x11111111] * 8))
    shstr = b"\0.text\0.shstrtab\0"
    code_off = 52 + 32
    str_off = code_off + len(code)
    sh_off = (str_off + len(shstr) + 3) & ~3
    eh = struct.pack("<16sHHIIIIIHHHHHH", b"\x7fELF\x01\x01\x01" + b"\0" * 9, 2, 40, 1, 0x10000001, 52, sh_off,
                     0x05000200, 52, 32, 1, 40, 3, 2)
    ph = struct.pack("<8I", 1, code_off, 0x10000000, 0x10000000, len(code), len(code), 5, 4)
    sh = (struct.pack("<10I", 0, 0, 0, 0, 0, 0, 0, 0, 0, 0) +
          struct.pack("<10I", 1, 1, 6, 0x10000000, code_off, len(code), 0, 0, 4, 0) +       # .text: PROGBITS, AX
          struct.pack("<10I", 7, 3, 0, 0, str_off, len(shstr), 0, 0, 1, 0))                # .shstrtab
    body = eh + ph + code + shstr
    return body + b"\0" * (sh_off - len(body)) + sh
