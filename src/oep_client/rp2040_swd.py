"""An RP2040 board as a test's peer over an OEP probe's SWD alone: written over SWD (rp2040), its console the SEGGER RTT
rings read and written through the MEM-AP while the core runs. For a peer whose native USB goes to the device under test,
so neither the USB writing nor a USB console is there.

The address is `oep://<probe>/<port>`: the probe's channels labelled `<port>.swdio` / `<port>.swclk` are the wire. When
a broker serves that probe, the session shares its lock (oep_client.brokers: OEP host guide §7.1, §7.2); else the probe
is opened directly.

- `python -m oep_client.rp2040_swd flash oep://<probe>/<port> <image.elf|.bin>`: write, verify, reboot.
- `python -m oep_client.rp2040_swd console oep://<probe>/<port> [seconds]`: print the RTT output.
- pyserial: `serial.protocol_handler_packages.append("oep_client.pyserial")` makes `serial_for_url("oep://...")` this
  console (the handler is oep_client.pyserial.protocol_oep).
- pytest: oep_client.pytest_plugin writes an RP2040 board's build here when its port is `oep://<probe>/<port>`
  (pytest-embedded-arduino-cli's upload hook).
"""

from __future__ import annotations

import struct
import sys
import threading
import time
import urllib.parse

from . import acquire, arm, brokers, core, rp2040

LEASE_MS = 60000
RAM, RAM_END = 0x20000000, 0x20042000
RTT_ID = b"SEGGER RTT\0\0\0\0\0\0"
FLASH_BASE, FLASH_END = 0x10000000, 0x11000000


def parse(address: str) -> tuple[str, str]:
    """oep://<probe>/<port>[?...] -> (unit id, port)."""
    u = urllib.parse.urlparse(address)
    port = u.path.strip("/").split("/")[0] if u.path.strip("/") else ""
    if u.scheme != "oep" or not u.netloc or not port:
        raise ValueError(f"{address}: want oep://<probe>/<port> (the port's labels <port>.swdio / <port>.swclk)")
    return u.netloc, port


def open_host(unit: str, owner: str):
    """A Host in a session on the probe: sharing a broker's lock when one serves it, else directly (oep_client.brokers)."""
    return brokers.open_host(unit, owner, lease_ms=LEASE_MS, direct=f"oep://{unit}?via=usb")


class Target:
    """The RP2040's core 0 on the port's SWD pair: wire, connection, MEM-AP, CortexM."""

    def __init__(self, h, port: str):
        swdio, swclk = acquire._channels(core, h, [f"{port}.swdio", f"{port}.swclk"])
        self.host, self.wire = h, arm.SwdWire(h)
        self.conn, self.dpidr, _ = self.wire.attach(targetsel=rp2040.TARGETSEL_CORE0, pins=(swdio, swclk))
        adi = arm.ArmAdi(h, self.conn)
        adi.power_up()
        self.mem = arm.MemAp(adi, 0)
        self.core = rp2040.core_of(self.mem)

    def close(self) -> None:
        try:
            self.wire.detach(self.conn)
        except Exception:
            pass


def image_of(path: str) -> bytes:
    """A flash image from 0x10000000: a .bin as it is, an ELF's loadable segments in flash laid out by address."""
    data = open(path, "rb").read()
    if data[:4] != b"\x7fELF":
        return data
    if data[4] != 1 or data[5] != 1:
        raise ValueError(f"{path}: want a 32-bit little-endian ELF")
    phoff, = struct.unpack_from("<I", data, 28)
    phentsize, phnum = struct.unpack_from("<HH", data, 42)
    parts = []
    for i in range(phnum):
        p_type, p_offset, _vaddr, p_paddr, p_filesz = struct.unpack_from("<IIIII", data, phoff + i * phentsize)
        if p_type == 1 and p_filesz and FLASH_BASE <= p_paddr < FLASH_END:
            parts.append((p_paddr, data[p_offset:p_offset + p_filesz]))
    if not parts:
        raise ValueError(f"{path}: no loadable segment in flash")
    end = max(a + len(b) for a, b in parts)
    image = bytearray(b"\xff" * (end - FLASH_BASE))
    for a, b in parts:
        image[a - FLASH_BASE:a - FLASH_BASE + len(b)] = b
    return bytes(image)


def clear_rtt_ids(mem) -> int:
    """Wipe every SEGGER RTT control block's ID left in RAM (RAM outlives a reboot): a console opened after the reboot
    then finds only the block the new image writes, never the old one - whose offsets, written back while the new
    image set them up again, lost its first lines. The core must be halted. -> how many were wiped."""
    n, chunk = 0, 16 * 1024
    for at in range(RAM, RAM_END, chunk):
        words = mem.read_block(at, min(chunk, RAM_END - at) // 4)
        raw = struct.pack(f"<{len(words)}I", *words)
        i = raw.find(RTT_ID)
        while i >= 0:
            if i % 4 == 0:
                mem.write32(at + i, 0)
                n += 1
            i = raw.find(RTT_ID, i + 1)
    return n


def flash(address: str, path: str, log=print) -> None:
    """Write the image over SWD, verify it, reboot the chip. Raises on a mismatch."""
    unit, port = parse(address)
    image = image_of(path)
    h = open_host(unit, f"rp2040 flash {port}")
    try:
        t = Target(h, port)
        try:
            t.core.halt()
            t.core.reset_halt()                       # a clean core to call the ROM from
            if not rp2040.program_and_verify(t.core, image, 0, log=log):
                raise RuntimeError(f"{address}: the flash did not read back as written ({len(image)} B)")
            log(f"{address}: {len(image)} B written and verified")
            clear_rtt_ids(t.mem)
            rp2040.Rom(t.core).reboot()
        finally:
            t.close()
    finally:
        try:
            h.end()
        finally:
            h.link.close()


class Rtt:
    """SEGGER RTT's up buffer 0 (target -> host) and down buffer 0 (host -> target) through the MEM-AP, the core
    running. The control block is found by its ID in RAM (it is written at run time; `wait` seconds for it)."""

    def __init__(self, mem, wait: float = 5.0):
        self.mem = mem
        self.cb = self._find(wait)
        nup, ndown = mem.read_block(self.cb + 16, 2)
        self.up = self.cb + 24                        # aUp[0]: sName pBuffer SizeOfBuffer WrOff RdOff Flags
        self.down = self.cb + 24 + 24 * nup           # aDown[0]
        _, self.up_buf, self.up_size, _, _, _ = mem.read_block(self.up, 6)
        _, self.down_buf, self.down_size, _, _, _ = mem.read_block(self.down, 6) if ndown else (0, 0, 0, 0, 0, 0)

    def _find(self, wait: float) -> int:
        deadline = time.monotonic() + wait
        chunk = 16 * 1024
        while True:
            for at in range(RAM, RAM_END, chunk):
                words = self.mem.read_block(at, min(chunk, RAM_END - at) // 4)
                raw = struct.pack(f"<{len(words)}I", *words)
                i = raw.find(RTT_ID)
                while i >= 0:
                    if i % 4 == 0:
                        return at + i
                    i = raw.find(RTT_ID, i + 1)
            if time.monotonic() > deadline:
                raise RuntimeError("no SEGGER RTT control block in RAM (is the sketch using RTT and running?)")
            time.sleep(0.2)

    def read(self) -> bytes:
        wr, rd = self.mem.read_block(self.up + 12, 2)
        if wr == rd or not self.up_size:
            return b""
        base = self.up_buf & ~3                       # the buffer need not be word aligned; block reads are
        skew = self.up_buf - base
        words = self.mem.read_block(base, (skew + self.up_size + 3) // 4)
        buf = struct.pack(f"<{len(words)}I", *words)[skew:skew + self.up_size]
        out = buf[rd:wr] if wr > rd else buf[rd:self.up_size] + buf[:wr]
        self.mem.write32(self.up + 16, wr)
        return out

    def write(self, data: bytes) -> int:
        if not self.down_size:
            return 0
        wr, rd = self.mem.read_block(self.down + 12, 2)
        room = (rd - wr - 1) % self.down_size
        data = data[:room]
        for b in data:
            at = self.down_buf + wr
            word = self.mem.read32(at & ~3)
            sh = 8 * (at & 3)
            self.mem.write32(at & ~3, (word & ~(0xFF << sh)) | (b << sh))
            wr = (wr + 1) % self.down_size
        self.mem.write32(self.down + 12, wr)
        return len(data)


class Console:
    """The peer's console: a session, the SWD connection and the RTT rings, polled in a thread into a buffer."""

    POLL_S = 0.01

    def __init__(self, address: str, wait: float = 10.0):
        unit, port = parse(address)
        self.address = address
        self.host = open_host(unit, f"rp2040 console {port}")
        self.target = Target(self.host, port)
        self.rtt = Rtt(self.target.mem, wait)
        self._buf = bytearray()
        self._out = bytearray()
        self._lock = threading.Lock()
        self._stop = False
        self.error: Exception | None = None
        self._thread = threading.Thread(target=self._run, name="rp2040-rtt", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        last = time.monotonic()
        while not self._stop:
            try:
                with self._lock:
                    out, self._out = bytes(self._out), bytearray()
                while out:
                    n = self.rtt.write(out)
                    out = out[n:]
                    if out:
                        time.sleep(self.POLL_S)
                data = self.rtt.read()
                if data:
                    with self._lock:
                        self._buf += data
                if time.monotonic() - last > 5:       # the lease: polling is requests, but keep it explicit
                    last = time.monotonic()
            except Exception as e:                    # the console's end: kept for the reader to see
                self.error = e
                return
            time.sleep(self.POLL_S)

    def read(self, size: int = -1) -> bytes:
        with self._lock:
            n = len(self._buf) if size < 0 else min(size, len(self._buf))
            data = bytes(self._buf[:n])
            del self._buf[:n]
        return data

    def in_waiting(self) -> int:
        with self._lock:
            return len(self._buf)

    def write(self, data: bytes) -> int:
        with self._lock:
            self._out += data
        return len(data)

    def close(self) -> None:
        self._stop = True
        self._thread.join(2)
        self.target.close()
        try:
            self.host.end()
        except Exception:
            pass
        close = getattr(getattr(self.host, "link", None), "close", None)
        if close:
            close()


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="python -m oep_client.rp2040_swd", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("flash", help="write an image over SWD, verify, reboot")
    f.add_argument("address")
    f.add_argument("image")
    c = sub.add_parser("console", help="print the RTT output")
    c.add_argument("address")
    c.add_argument("seconds", type=float, nargs="?", default=5.0)
    a = ap.parse_args(argv)
    if a.cmd == "flash":
        try:
            flash(a.address, a.image)
        except Exception as e:
            print(f"rp2040_swd flash: {e}", file=sys.stderr)
            return 1
        return 0
    con = Console(a.address)
    try:
        t = time.monotonic()
        while time.monotonic() - t < a.seconds:
            data = con.read()
            if data:
                sys.stdout.write(data.decode(errors="replace"))
                sys.stdout.flush()
            if con.error:
                raise con.error
            time.sleep(0.05)
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
