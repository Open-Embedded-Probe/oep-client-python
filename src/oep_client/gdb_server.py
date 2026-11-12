"""A GDB remote server for a Cortex-M on an OEP probe's SWD: what an IDE's debugger connects to.

The Arduino IDE debugs through cortex-debug, which with `servertype: external` connects gdb to `gdbTarget`
(`target extended-remote HOST:PORT`) and sends `monitor reset halt`, `load` and `monitor halt`. This server answers
gdb's remote protocol on a TCP port and does the work over the probe: registers and memory through the MEM-AP, run,
stop and single-step through DHCSR, breakpoints in the FPB, `load` into flash by the chip's own routine.

The target is a `Target`-shaped object (`Rp2040Debug` here: an RP2040's core 0 through `rp2040_peer`), so the server
runs against a fake one in tests.

What gdb gets:
- `qSupported`: PacketSize, no-ack mode, `qXfer:features:read` (an M-profile target.xml: r0-r12, sp, lr, pc, xpsr)
  and `qXfer:memory-map:read` (the target's flash, RAM and the rest; gdb then puts breakpoints in flash as hardware
  ones and writes flash with vFlashErase / vFlashWrite / vFlashDone).
- `g` `G` `p` `P` `m` `M` `X`, `c` `s` `vCont`, Ctrl-C, `Z0` / `Z1` (both in the FPB) `z0` / `z1`, `qRcmd`
  (`monitor reset halt`, `monitor reset`, `monitor halt`, `monitor resume`), `D`, `k`, `qAttached`, threads as one.
"""

from __future__ import annotations

import binascii
import select
import socket
import struct
import threading
import time
from typing import Callable

POLL = 0.05                 # how often a running core is looked at
KEEPALIVE_S = 5.0           # a request to the probe this often while gdb is idle (the session's lease)

TARGET_XML = """<?xml version="1.0"?>
<!DOCTYPE target SYSTEM "gdb-target.dtd">
<target version="1.0">
<architecture>arm</architecture>
<feature name="org.gnu.gdb.arm.m-profile">
""" + "".join(f'<reg name="r{i}" bitsize="32" regnum="{i}" type="uint32" group="general"/>\n' for i in range(13)) + """\
<reg name="sp" bitsize="32" regnum="13" type="data_ptr" group="general"/>
<reg name="lr" bitsize="32" regnum="14" type="uint32" group="general"/>
<reg name="pc" bitsize="32" regnum="15" type="code_ptr" group="general"/>
<reg name="xpsr" bitsize="32" regnum="16" type="uint32" group="general"/>
</feature>
</target>
"""
NREGS = 17


def _locked(fn):
    """One operation at a time on the probe: the gdb session's and an upload's (the IDE's Upload while gdb is
    connected) come from two threads. An upload holds it from its halt to its run, so gdb never sees it stopped."""
    def run(self, *a, **k):
        with self.lock:
            return fn(self, *a, **k)
    run.__name__, run.__doc__ = fn.__name__, fn.__doc__
    return run


class Rp2040Debug:
    """An RP2040's core 0 on an OEP port (`oep://<probe>/<port>`), for the server. Opens the probe on `open()`."""

    FLASH, FLASH_SIZE, SECTOR = 0x10000000, 2 * 1024 * 1024, 4096
    RAM, RAM_SIZE = 0x20000000, 0x42000
    FP_CTRL, FP_COMP0 = 0xE0002000, 0xE0002008
    C_STEP = 4

    def __init__(self, address: str, owner: str = "gdb"):
        self.address, self.owner = address, owner
        self.host = self.t = None
        self.bp: dict[int, int] = {}              # address -> comparator
        self.lock = threading.RLock()

    # session
    def open(self) -> None:
        from . import rp2040_peer as rp
        unit, port = rp.parse(self.address)
        self.host = rp.open_host(unit, self.owner)
        self.t = rp.Target(self.host, port)
        self.core, self.mem = self.t.core, self.t.mem
        self.mem.write32(self.FP_CTRL, 3)          # FPB on (KEY | ENABLE)
        n = (self.mem.read32(self.FP_CTRL) >> 4) & 0xF
        self.comparators = n or 4
        for i in range(self.comparators):
            self.mem.write32(self.FP_COMP0 + 4 * i, 0)

    def close(self) -> None:
        if self.t is None:
            return
        try:
            for i in range(self.comparators):
                self.mem.write32(self.FP_COMP0 + 4 * i, 0)
            self.core.release()
        except Exception:
            pass
        try:
            self.t.close()
        finally:
            try:
                self.host.end()
            finally:
                self.host.link.close()
                self.t = None

    @_locked
    def keepalive(self) -> None:
        self.host.keepalive()

    # run control
    @_locked
    def halted(self) -> bool:
        return self.core.halted()

    @_locked
    def halt(self) -> None:
        self.core.halt()

    @_locked
    def resume(self) -> None:
        self.core.resume()

    @_locked
    def step(self) -> None:
        k = self.core
        k.mem.write32(k.DHCSR, k.KEY | k.C_DEBUGEN | k.C_HALT | k.C_MASKINTS)
        k.mem.write32(k.DHCSR, k.KEY | k.C_DEBUGEN | k.C_MASKINTS | self.C_STEP)
        k._wait(k.S_HALT, 1.0)
        k.mem.write32(k.DHCSR, k.KEY | k.C_DEBUGEN | k.C_HALT)

    @_locked
    def reset_halt(self) -> None:
        self.core.reset_halt()

    @_locked
    def reset_run(self) -> None:
        """The core reset (the boot ROM runs boot2 and the image again) and let run. Not the ROM's reboot: that
        resets the whole chip and the debug link with it, and gdb's session would be lost."""
        self.core.reset_halt()
        self.core.resume()

    # registers
    @_locked
    def read_registers(self) -> list[int]:
        return [self.core.reg(n) for n in range(NREGS)]

    @_locked
    def write_register(self, n: int, value: int) -> None:
        self.core.set_reg(n, value)

    # memory
    @_locked
    def read_memory(self, address: int, length: int) -> bytes:
        start, end = address & ~3, (address + length + 3) & ~3
        words = self.mem.read_block(start, (end - start) // 4) if end > start else []
        raw = struct.pack(f"<{len(words)}I", *words)
        return raw[address - start:address - start + length]

    @_locked
    def write_memory(self, address: int, data: bytes) -> None:
        start, end = address & ~3, (address + len(data) + 3) & ~3
        buf = bytearray(self.read_memory(start, end - start)) if (start != address or end != address + len(data)) \
            else bytearray(end - start)
        buf[address - start:address - start + len(data)] = data
        self.mem.write_block(start, list(struct.unpack(f"<{len(buf) // 4}I", bytes(buf))))

    def memory_map(self) -> str:
        return ('<?xml version="1.0"?>\n<!DOCTYPE memory-map PUBLIC "+//IDN gnu.org//DTD GDB Memory Map V1.0//EN" '
                '"http://sourceware.org/gdb/gdb-memory-map.dtd">\n<memory-map>\n'
                '<memory type="rom" start="0x0" length="0x4000"/>\n'
                f'<memory type="flash" start="{self.FLASH:#x}" length="{self.FLASH_SIZE:#x}">'
                f'<property name="blocksize">{self.SECTOR:#x}</property></memory>\n'
                f'<memory type="ram" start="{self.RAM:#x}" length="{self.RAM_SIZE:#x}"/>\n'
                '<memory type="ram" start="0x40000000" length="0x20000000"/>\n'
                '<memory type="ram" start="0xd0000000" length="0x10000000"/>\n'
                '<memory type="ram" start="0xe0000000" length="0x10000000"/>\n'
                '</memory-map>\n')

    @_locked
    def upload(self, image: bytes, log: Callable[[str], None]) -> None:
        """A whole flash image from the flash's start (the IDE's Upload while gdb is connected), then run it."""
        self.core.halt()
        self.core.reset_halt()
        self.write_flash(self.FLASH, image, log)
        self.reset_run()

    @_locked
    def write_flash(self, address: int, image: bytes, log: Callable[[str], None]) -> None:
        from . import rp2040
        if not rp2040.program_and_verify(self.core, image, address - self.FLASH, log=log):
            raise RuntimeError("the flash did not read back as written")

    # breakpoints (the FPB, v1: an address below 0x20000000, the half-word picked by REPLACE)
    @_locked
    def set_breakpoint(self, address: int) -> bool:
        if address in self.bp:
            return True
        if address >= 0x20000000:
            return False
        free = [i for i in range(self.comparators) if i not in self.bp.values()]
        if not free:
            return False
        i = free[0]
        replace = 2 if address & 2 else 1
        self.mem.write32(self.FP_COMP0 + 4 * i, (replace << 30) | (address & 0x1FFFFFFC) | 1)
        self.bp[address] = i
        return True

    @_locked
    def clear_breakpoint(self, address: int) -> bool:
        i = self.bp.pop(address, None)
        if i is None:
            return False
        self.mem.write32(self.FP_COMP0 + 4 * i, 0)
        return True


# ---- the protocol ------------------------------------------------------------------------------------------------

def checksum(body: bytes) -> bytes:
    return b"%02x" % (sum(body) & 0xFF)


def frame(body: bytes) -> bytes:
    return b"$" + body + b"#" + checksum(body)


def unescape(data: bytes) -> bytes:
    out, i = bytearray(), 0
    while i < len(data):
        c = data[i]
        if c == 0x7D and i + 1 < len(data):
            out.append(data[i + 1] ^ 0x20)
            i += 2
        else:
            out.append(c)
            i += 1
    return bytes(out)


def _hex32(v: int) -> bytes:
    return binascii.hexlify(struct.pack("<I", v & 0xFFFFFFFF))


class Session:
    """One gdb connection: reads packets, answers them, runs the target."""

    def __init__(self, conn: socket.socket, target, log: Callable[[str], None] = print):
        self.conn, self.target, self.log = conn, target, log
        self.ack = True
        self.buf = b""
        self.flash: dict[int, bytearray] = {}      # vFlashWrite: start -> contiguous bytes
        self.last_io = time.monotonic()

    # transport
    def _recv(self, timeout: float | None) -> bytes:
        r, _, _ = select.select([self.conn], [], [], timeout)
        if not r:
            return b""
        data = self.conn.recv(65536)
        if not data:
            raise ConnectionError("gdb closed")
        return data

    def next_packet(self) -> bytes | None:
        """The next packet's body (b"\\x03" for an interrupt); None after KEEPALIVE_S of silence."""
        while True:
            while self.buf and self.buf[:1] in (b"+", b"-"):
                self.buf = self.buf[1:]
            if self.buf[:1] == b"\x03":
                self.buf = self.buf[1:]
                return b"\x03"
            start = self.buf.find(b"$")
            if start >= 0:
                end = self.buf.find(b"#", start)
                if end >= 0 and len(self.buf) >= end + 3:
                    body, cs = self.buf[start + 1:end], self.buf[end + 1:end + 3]
                    self.buf = self.buf[end + 3:]
                    if self.ack:
                        self.conn.sendall(b"+" if checksum(body) == cs.lower() else b"-")
                    return body
            elif self.buf and self.buf[:1] not in (b"$", b"+", b"-", b"\x03"):
                self.buf = self.buf[1:]
                continue
            data = self._recv(KEEPALIVE_S)
            if not data:
                return None
            self.buf += data

    def send(self, body: bytes | str) -> None:
        if isinstance(body, str):
            body = body.encode()
        self.conn.sendall(frame(body))             # gdb's '+' is skipped by the next read

    # the loop
    def serve(self) -> None:
        while True:
            body = self.next_packet()
            if body is None:
                self.target.keepalive()
                continue
            if body == b"\x03":
                self.target.halt()
                self.send("T02")
                continue
            reply = self.handle(body)
            if reply is not None:
                self.send(reply)
            if body[:1] in (b"k", b"D"):
                return

    def wait_halt(self) -> str:
        """Run until the core halts or gdb interrupts."""
        while True:
            if self.target.halted():
                return "T05"
            data = self._recv(POLL)
            if data:
                self.buf += data
                if b"\x03" in self.buf:
                    self.buf = self.buf.replace(b"\x03", b"", 1)
                    self.target.halt()
                    return "T02"

    def handle(self, body: bytes) -> str | bytes | None:
        t = self.target
        cmd = body[:1]
        try:
            if body.startswith(b"qSupported"):
                return "PacketSize=4000;QStartNoAckMode+;qXfer:features:read+;qXfer:memory-map:read+;vContSupported+"
            if body == b"QStartNoAckMode":
                self.send("OK")
                self.ack = False
                return None
            if body.startswith(b"qXfer:features:read:target.xml:"):
                return self._xfer(TARGET_XML, body)
            if body.startswith(b"qXfer:memory-map:read::"):
                return self._xfer(t.memory_map(), body)
            if body == b"qAttached":
                return "1"
            if body == b"qC":
                return "QC1"
            if body == b"qfThreadInfo":
                return "m1"
            if body == b"qsThreadInfo":
                return "l"
            if body.startswith(b"qRcmd,"):
                return self._monitor(binascii.unhexlify(body[6:]).decode(errors="replace").strip())
            if cmd == b"?":
                return "T05" if t.halted() else self._stop_then("T02")
            if cmd == b"H" or body.startswith(b"T"):
                return "OK"
            if cmd == b"g":
                return b"".join(_hex32(v) for v in t.read_registers())
            if cmd == b"G":
                raw = binascii.unhexlify(body[1:])
                for n in range(min(NREGS, len(raw) // 4)):
                    t.write_register(n, struct.unpack_from("<I", raw, 4 * n)[0])
                return "OK"
            if cmd == b"p":
                n = int(body[1:], 16)
                return _hex32(t.read_registers()[n]) if n < NREGS else "E01"
            if cmd == b"P":
                n, v = body[1:].split(b"=")
                t.write_register(int(n, 16), struct.unpack("<I", binascii.unhexlify(v))[0])
                return "OK"
            if cmd == b"m":
                a, n = (int(x, 16) for x in body[1:].split(b","))
                return binascii.hexlify(t.read_memory(a, n))
            if cmd == b"M":
                head, data = body[1:].split(b":", 1)
                a, _ = (int(x, 16) for x in head.split(b","))
                t.write_memory(a, binascii.unhexlify(data))
                return "OK"
            if cmd == b"X":
                head, data = body[1:].split(b":", 1)
                a, n = (int(x, 16) for x in head.split(b","))
                if n:
                    t.write_memory(a, unescape(data))
                return "OK"
            if body == b"vCont?":
                return "vCont;c;C;s;S;t"
            if body.startswith(b"vCont;"):
                action = body[6:7]
                if action in (b"s", b"S"):
                    t.step()
                    return "T05"
                if action == b"t":
                    t.halt()
                    return "T00"
                t.resume()
                return self.wait_halt()
            if cmd in (b"c", b"C"):
                t.resume()
                return self.wait_halt()
            if cmd in (b"s", b"S"):
                t.step()
                return "T05"
            if cmd in (b"Z", b"z") and body[1:2] in (b"0", b"1"):
                a = int(body[3:].split(b",")[0], 16)
                ok = t.set_breakpoint(a) if cmd == b"Z" else t.clear_breakpoint(a)
                return "OK" if ok else "E01"
            if body.startswith(b"vFlashErase:"):
                return "OK"                          # the write erases what it covers
            if body.startswith(b"vFlashWrite:"):
                head, data = body[12:].split(b":", 1)
                self._flash_put(int(head, 16), unescape(data))
                return "OK"
            if body == b"vFlashDone":
                return self._flash_done()
            if body == b"vMustReplyEmpty" or body.startswith(b"vKill"):
                return "" if body == b"vMustReplyEmpty" else "OK"
            if cmd == b"D":
                t.resume()
                return "OK"
            if cmd == b"k":
                t.resume()
                return None
            return ""
        except (ValueError, IndexError):
            return "E01"
        except TimeoutError as e:
            self.log(f"gdb: {body[:40]!r}: {e}")
            return "E02"

    def _stop_then(self, reply: str) -> str:
        self.target.halt()
        return reply

    def _xfer(self, text: str, body: bytes) -> str:
        offset, length = (int(x, 16) for x in body.rsplit(b":", 1)[1].split(b","))
        data = text[offset:offset + length]
        return ("l" if offset + length >= len(text) else "m") + data

    def _monitor(self, line: str) -> str:
        t = self.target
        words = line.split()
        if words in (["reset", "halt"], ["reset", "init"]):
            t.reset_halt()
            msg = "core reset and halted\n"
        elif words in (["reset"], ["reset", "run"]):
            t.reset_run()
            msg = "core reset, running\n"
        elif words == ["halt"]:
            t.halt()
            msg = "core halted\n"
        elif words in (["resume"], ["go"]):
            t.resume()
            msg = "core running\n"
        else:
            msg = f"unknown monitor command: {line}\n"
        self.send("O" + binascii.hexlify(msg.encode()).decode())
        return "OK"

    def _flash_put(self, address: int, data: bytes) -> None:
        for start, buf in self.flash.items():
            if start + len(buf) == address:
                buf += data
                return
        self.flash[address] = bytearray(data)

    def _flash_done(self) -> str:
        """The pieces of this load as one image from the first piece's erase block to the last byte, the gaps 0xFF
        (gdb erases whole blocks; its sections can leave a few bytes between them): one write of the chip's routine."""
        if not self.flash:
            return "OK"
        try:
            block = getattr(self.target, "SECTOR", 4096)
            start = min(self.flash) & ~(block - 1)
            end = max(a + len(b) for a, b in self.flash.items())
            image = bytearray(b"\xff" * (end - start))
            for a, b in self.flash.items():
                image[a - start:a - start + len(b)] = b
            self.log(f"gdb: writing {len(image)} B of flash at {start:#010x}")
            self.target.write_flash(start, bytes(image), self.log)
        except Exception as e:
            self.log(f"gdb: flash write failed: {e}")
            return "E03"
        finally:
            self.flash.clear()
        return "OK"


class Server:
    """Listens on a TCP port; each gdb connection opens the target, is served, and closes it."""

    def __init__(self, make_target: Callable[[], object], port: int = 3333, bind: str = "127.0.0.1",
                 log: Callable[[str], None] = print):
        self.make_target, self.log = make_target, log
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((bind, port))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.lock = threading.Lock()               # one gdb at a time

    def serve_forever(self, stop: threading.Event | None = None) -> None:
        self.sock.settimeout(0.5)
        while not (stop and stop.is_set()):
            try:
                conn, peer = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self._client, args=(conn, peer), daemon=True).start()

    def _client(self, conn: socket.socket, peer) -> None:
        if not self.lock.acquire(blocking=False):
            conn.close()
            return
        target = None
        try:
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            target = self.make_target()
            target.open()
            self.log(f"gdb: {peer[0]}:{peer[1]} connected")
            Session(conn, target, self.log).serve()
        except ConnectionError:
            pass
        except Exception as e:
            self.log(f"gdb: session ended: {e}")
        finally:
            if target is not None:
                target.close()
            conn.close()
            self.lock.release()
            self.log("gdb: disconnected")

    def close(self) -> None:
        self.sock.close()
