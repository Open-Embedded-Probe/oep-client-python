"""A USB vendor bulk endpoint pair shaped like the part of a pyserial port the v1 link uses (read with a timeout,
in_waiting, write, reset_input_buffer), so length-prefixed frames run over it unchanged. pyusb is imported only here.

IN is drained on a thread: a bulk write blocks until the device takes the data, and the device stops taking data
once its answers fill its FIFO and nobody reads them (E160).

Each IN read asks for READ_SIZE bytes. A read completes when that much has arrived or a short packet ends the
transfer; if its timeout hits first, pyusb raises and the bytes already received are lost. Under a continuous stream
(12.5 MB/s, 2026-09-25) a 64 KiB read waited for more than its 50 ms and dropped whole answers, so reads stay small."""

from __future__ import annotations

import atexit
import collections
import threading
import time



from . import registry as reg

# the vendor bulk transport's interface: class 0xFF, subclass 'O', protocol 'E' (transports §3); one per probe
VENDOR_CLASS, VENDOR_SUBCLASS, VENDOR_PROTOCOL = (reg.USB[k] for k in ("vendor_bulk_class", "vendor_bulk_subclass",
                                                                       "vendor_bulk_protocol"))

class UsbBulkStream:
    READ_SIZE = 16384
    READER = "usb-sync"      # one synchronous read at a time: a host's pause stalls the probe's sending (link.reader)

    def __init__(self, device, endpoint_in, endpoint_out, interface: int):
        self.device, self.ep_in, self.ep_out, self.interface = device, endpoint_in, endpoint_out, interface
        self.timeout = 0.05
        self._buffer = bytearray()
        self._cond = threading.Condition()
        self._closed = False
        self._reader = threading.Thread(target=self._drain, daemon=True)
        self._reader.start()

    @classmethod
    def open(cls, vid: int, pid: int, serial: str | None = None) -> "UsbBulkStream":
        import usb.core
        import usb.util
        match = (lambda d: usb.util.get_string(d, d.iSerialNumber).lower() == serial.lower()) if serial else None
        dev = usb.core.find(idVendor=vid, idProduct=pid, custom_match=match)
        if dev is None:
            raise FileNotFoundError(f"no USB device {vid:04x}:{pid:04x}" + (f" serial {serial}" if serial else ""))
        cfg = dev.get_active_configuration()
        for intf in cfg:
            if (intf.bInterfaceClass, intf.bInterfaceSubClass, intf.bInterfaceProtocol) != \
                    (VENDOR_CLASS, VENDOR_SUBCLASS, VENDOR_PROTOCOL):   # the OEP vendor interface, not another 0xFF one
                continue
            eps = [e for e in intf if usb.util.endpoint_type(e.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK]
            ins = [e for e in eps if usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN]
            outs = [e for e in eps if usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT]
            if ins and outs:
                usb.util.claim_interface(dev, intf.bInterfaceNumber)
                return cls(dev, ins[0], outs[0], intf.bInterfaceNumber)
        raise FileNotFoundError("no OEP vendor interface (class 0xFF, subclass 0x4F, protocol 0x45) with a bulk IN/OUT pair")

    def _drain(self) -> None:
        import usb.core
        while not self._closed:
            try:
                data = self.ep_in.read(self.READ_SIZE, timeout=200)
            except usb.core.USBTimeoutError:
                continue
            except usb.core.USBError:
                if self._closed:
                    return
                time.sleep(0.01)
                continue
            if len(data):
                with self._cond:
                    self._buffer += bytes(data)
                    self._cond.notify_all()

    @property
    def in_waiting(self) -> int:
        with self._cond:
            return len(self._buffer)

    def read(self, n: int = 1) -> bytes:
        deadline = time.monotonic() + (self.timeout or 0)
        with self._cond:
            while not self._buffer:
                left = deadline - time.monotonic()
                if left <= 0:
                    return b""
                self._cond.wait(left)
            out = bytes(self._buffer[:n])
            del self._buffer[:n]
            return out

    def write(self, data: bytes) -> int:
        n = self.ep_out.write(data, timeout=2000)
        if data and len(data) % self.ep_out.wMaxPacketSize == 0:   # a zero-length packet ends the probe's transfer
            self.ep_out.write(b"", timeout=2000)
        return n

    def reset_input_buffer(self) -> None:
        with self._cond:
            self._buffer.clear()

    def close(self) -> None:
        import usb.util
        self._closed = True
        self._reader.join(timeout=0.5)
        usb.util.release_interface(self.device, self.interface)
        usb.util.dispose_resources(self.device)


class UsbAsyncStream:
    """The same shape as UsbBulkStream on python-libusb1 (`usb1`): IN runs as DEPTH asynchronous transfers of URB_SIZE
    kept queued by an event thread, so a continuous stream near the HS ceiling (wch-protocols E116: 1 MiB x 8 reaches
    ~47 MB/s over usbipd; 64 KiB x 32 carried 2 x 160 Msps here, x 128 40 MB/s for 30 s with no loss) is taken without gaps. A transfer completes when full or on a short packet; the probe ends a
    transfer whose length is a whole number of packets with a zero-length packet, so small answers never wait."""
    READER = "usb-async"     # queued asynchronous IN transfers: a host's pause is taken by the kernel (link.reader)

    # The probe ends every transfer with a short packet (a frame, ~19 KB at 40 MB/s on the P4X), so a transfer holds
    # one frame whatever its size: what the host can hold back is DEPTH frames, not DEPTH x URB_SIZE - 64 KiB x 128 kept
    # ~2.4 MB (60 ms at 40 MB/s), 256 KiB x 32 lost far more (wireskein, 2026-10-20). So transfers of a frame's most
    # (32 KiB) and as many as usbfs takes: 480 x 32 KiB is 15 MiB of usbfs_memory_mb's default 16. A submit usbfs
    # refuses (its memory, another device's transfers in this process) leaves the depth at what it took.
    URB_SIZE = 32 * 1024
    DEPTH = 480
    # The bytes waiting for read() at most: past it, a transfer that comes back is held, not sent out again, until read()
    # takes them under half - the kernel then takes no more, the probe's own buffer fills and it drops whole frames,
    # which the stream's positions show (core §10.4). Without it a host slower than the stream grew without end (a
    # P4X at 40 MB/s: the process killed out of memory, 2026-11-17). 64 MiB: 1.6 s at 40 MB/s. A host whose disk
    # stops writing for seconds (writeback) needs more: OEP_USB_BACKLOG_MB or backlog_max= chooses it (backlog_bytes).
    BACKLOG_MAX = 64 * 1024 * 1024

    def __init__(self, context, handle, endpoint_in: int, endpoint_out: int, interface: int, out_packet: int = 512,
                 urb_size: int | None = None, depth: int | None = None, backlog_max: int | None = None):
        import usb1
        self._usb1 = usb1
        self.context, self.handle, self.ep_in, self.ep_out, self.interface = context, handle, endpoint_in, endpoint_out, interface
        self.out_packet = out_packet   # the OUT endpoint's wMaxPacketSize (64 at full speed, 512 at high speed)
        self.timeout = 0.05
        self._buffer = bytearray()
        self._cond = threading.Condition()
        self._closed = False           # no more resubmits (close() started)
        self._stop = False             # the event thread ends (every transfer is back)
        self.urb_size, self.depth = urb_size or self.URB_SIZE, depth or self.DEPTH
        self.backlog_max = backlog_max or backlog_bytes(self.BACKLOG_MAX)
        self._held = []                # transfers back while the backlog was full: out again when it drains
        self._transfers = []
        self._active = 0               # transfers submitted and not yet back for good
        # what stats() reports: the transfers out at a callback's start (the fewest seen: how near the queue came to
        # empty - the kernel takes the stream only into transfers that are out), the longest between two callbacks,
        # the most bytes waiting for read()
        self._inflight = 0
        self._stats = {"callbacks": 0, "inflight_min": None, "gap_max_s": 0.0, "buffered_max": 0, "bytes": 0,
                       "holds": 0, "held_max": 0}
        self._last_cb = None
        for _ in range(self.depth):
            t = handle.getTransfer()
            t.setBulk(endpoint_in, self.urb_size, callback=self._done, timeout=0)
            try:
                t.submit()
            except usb1.USBError:
                if not self._transfers:
                    raise                  # not even one: the endpoint is unusable
                break                      # usbfs' memory: the depth is what it took
            self._transfers.append(t)
            self._active += 1
            self._inflight += 1
        self.depth = len(self._transfers)
        self._events = threading.Thread(target=self._run, daemon=True)
        self._events.start()
        # Closed at exit too: left to usb1's own finalizers, the context went while the event thread was inside
        # handleEventsTimeout with transfers out - the process hung at exit or libusb aborted (usbi_mutex_destroy).
        atexit.register(self.close)

    @classmethod
    def open(cls, vid: int, pid: int, serial: str | None = None, *, urb_size: int | None = None,
             depth: int | None = None, backlog_max: int | None = None) -> "UsbAsyncStream":
        """`urb_size` / `depth`: the IN transfers (URB_SIZE x DEPTH by default). Each transfer back is a callback that
        takes the GIL: fewer, larger ones keep up better beside a thread that holds it (a low rate's latency grows
        with the size). `backlog_max`: the bytes waiting for read() at most (backlog_bytes by default)."""
        import usb1
        context = usb1.USBContext()
        context.open()
        for dev in context.getDeviceIterator(skip_on_error=True):
            if dev.getVendorID() != vid or dev.getProductID() != pid:
                continue
            handle = dev.open()
            if serial and (handle.getSerialNumber() or "").lower() != serial.lower():
                handle.close()
                continue
            for setting in dev.iterSettings():
                if (setting.getClass(), setting.getSubClass(), setting.getProtocol()) != \
                        (VENDOR_CLASS, VENDOR_SUBCLASS, VENDOR_PROTOCOL):   # the OEP vendor interface (transports §3)
                    continue
                eps = [(e.getAddress(), e.getAttributes()) for e in setting]
                sizes = {e.getAddress(): e.getMaxPacketSize() for e in setting}
                ins = [a for a, attr in eps if attr & 3 == 2 and a & 0x80]
                outs = [a for a, attr in eps if attr & 3 == 2 and not a & 0x80]
                if ins and outs:
                    handle.claimInterface(setting.getNumber())
                    return cls(context, handle, ins[0], outs[0], setting.getNumber(), sizes[outs[0]],
                               urb_size=urb_size, depth=depth, backlog_max=backlog_max)
            handle.close()
        context.close()
        raise FileNotFoundError(f"no USB device {vid:04x}:{pid:04x}" + (f" serial {serial}" if serial else ""))

    def _done(self, transfer) -> None:
        usb1 = self._usb1
        now = time.monotonic()
        st = self._stats
        self._inflight -= 1            # the event thread alone changes it once running
        st["callbacks"] += 1
        if st["inflight_min"] is None or self._inflight < st["inflight_min"]:
            st["inflight_min"] = self._inflight
        if self._last_cb is not None and now - self._last_cb > st["gap_max_s"]:
            st["gap_max_s"] = now - self._last_cb
        self._last_cb = now
        status = transfer.getStatus()
        data = b""
        if status == usb1.TRANSFER_COMPLETED:
            n = transfer.getActualLength()
            if n:
                data = bytes(transfer.getBuffer()[:n])   # copied out: the transfer goes back out at once
        # out again before the lock: a read() holding it does not keep the transfer back - unless the backlog is full
        held = False
        if not self._closed and status in (usb1.TRANSFER_COMPLETED, usb1.TRANSFER_TIMED_OUT):
            if len(self._buffer) + len(data) > self.backlog_max:
                held = True
                resubmitted = False
            else:
                try:
                    transfer.submit()
                    self._inflight += 1
                    resubmitted = True
                except usb1.USBError:
                    resubmitted = False
        else:
            resubmitted = False
        with self._cond:
            if held:
                self._held.append(transfer)
                st["holds"] += 1
                st["held_max"] = max(st["held_max"], len(self._held))
            if data:
                self._buffer += data
                st["bytes"] += len(data)
                if len(self._buffer) > st["buffered_max"]:
                    st["buffered_max"] = len(self._buffer)
            if not resubmitted and not held:
                self._active -= 1      # back for good (cancelled, failed, or closing); a held one is still the stream's
            self._cond.notify_all()

    def stats(self) -> dict:
        """The IN side so far: callbacks, inflight_min (the fewest transfers out at a callback; near 0, the stream
        outran the host and the probe's buffer took it), gap_max_s (the longest between two callbacks), buffered_max
        (bytes waiting for read() at most), bytes, and urb_size / depth."""
        with self._cond:
            return dict(self._stats, urb_size=self.urb_size, depth=self.depth)

    def _run(self) -> None:
        while not self._stop:
            try:
                self.context.handleEventsTimeout(0.1)
            except self._usb1.USBErrorInterrupted:
                continue
            except self._usb1.USBError:
                break

    @property
    def in_waiting(self) -> int:
        with self._cond:
            return len(self._buffer)

    def read(self, n: int = 1) -> bytes:
        deadline = time.monotonic() + (self.timeout or 0)
        with self._cond:
            while not self._buffer:
                left = deadline - time.monotonic()
                if left <= 0:
                    return b""
                self._cond.wait(left)
            out = self.take_locked(n)
        self.release_held()
        return out

    def take_locked(self, n: int) -> bytes:
        """Up to n bytes off the front; the caller holds self._cond (then calls release_held() without it)."""
        out = bytes(self._buffer[:n])
        del self._buffer[:n]
        return out

    def release_held(self) -> None:
        """The held transfers out again once the backlog is under half of backlog_max (outside self._cond)."""
        with self._cond:
            if not self._held or self._closed or len(self._buffer) > self.backlog_max // 2:
                return
            held, self._held = self._held, []
        for t in held:
            try:
                t.submit()
            except self._usb1.USBError:
                with self._cond:
                    self._active -= 1          # back for good
                    self._cond.notify_all()
                continue
            with self._cond:
                self._inflight += 1

    def write(self, data: bytes) -> int:
        n = self.handle.bulkWrite(self.ep_out, data, timeout=2000)
        if data and len(data) % self.out_packet == 0:   # a zero-length packet ends the transfer (transports §1)
            self.handle.bulkWrite(self.ep_out, b"", timeout=2000)
        return n

    def reset_input_buffer(self) -> None:
        with self._cond:
            self._buffer.clear()
        self.release_held()

    def close(self) -> None:
        """Cancel the IN transfers and let the event thread take them back before the handle and the context go
        (closing with transfers still out hung the process or aborted libusb). Safe to call twice (atexit)."""
        if self._closed:
            return
        self._closed = True
        atexit.unregister(self.close)
        with self._cond:
            self._active -= len(self._held)    # not out: nothing to cancel
            self._held = []
        for t in self._transfers:
            try:
                t.cancel()
            except self._usb1.USBError:
                pass
        deadline = time.monotonic() + 1.0
        with self._cond:
            while self._active > 0 and self._events.is_alive() and time.monotonic() < deadline:
                self._cond.wait(0.05)
        self._stop = True
        self._events.join(timeout=1.0)
        try:
            self.handle.releaseInterface(self.interface)
        except self._usb1.USBError:
            pass
        finally:
            self.handle.close()
            self.context.close()


def backlog_bytes(default: int) -> int:
    """The USB stream's backlog (UsbAsyncStream.BACKLOG_MAX): OEP_USB_BACKLOG_MB (MiB, a whole number from 1) when set,
    else `default`. The user chooses the margin: a host whose disk stops for seconds holds that long's data here."""
    import os
    value = os.environ.get("OEP_USB_BACKLOG_MB", "").strip()
    if not value:
        return default
    try:
        mib = int(value)
    except ValueError:
        mib = 0
    if mib < 1:
        raise ValueError(f"OEP_USB_BACKLOG_MB={value!r}: a whole number of MiB from 1")
    return mib * 1024 * 1024


class _Split:
    """UsbProcessStream's process: the IN bytes as length-prefixed frames (transports §2), answers ahead of data. A
    result or an event goes to the host before any data frame still waiting; data frames past `limit` bytes waiting
    are dropped whole, which the stream's positions show as a loss (core §10.4) - the IN side is never held, so an
    answer (a lease renewal, the stop) never waits behind a host that fell behind (P4 at 40 MB/s beside a host under
    load: the answers queued behind 0.6 - 1.3 GB of data and the session ended, wireskein 2026-11-25).
    Bytes that do not read as a frame (an unknown role: a start in the middle of one) go on in order, unsplit, until
    the input is quiet for probe_frame_gap_ms with no frame half read - the next byte then starts one (transports §5)."""

    ROLES = frozenset(reg.ROLES.values())
    QUIET_S = reg.TIMING["probe_frame_gap_ms"] / 1000

    def __init__(self, limit: int):
        self.limit = limit
        self.cond = threading.Condition()
        self.first = collections.deque()       # answers, events and unsplit bytes: out first, in order
        self.data = collections.deque()        # data frames (with their length prefix)
        self.data_bytes = 0
        self.dropped_frames = self.dropped_bytes = 0
        self._pending = bytearray()
        self._aligned = True
        self._last = time.monotonic()

    def feed(self, chunk: bytes, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        quiet = now - self._last >= self.QUIET_S
        self._last = now
        first, data = [], []
        if quiet:
            if self._pending:                  # a frame that stopped half way: on as it is (the host's stall rule)
                first.append(bytes(self._pending))
                self._pending.clear()
            self._aligned = True
        buf = self._pending
        buf += chunk
        i = 0
        if self._aligned:
            while len(buf) - i >= 2:
                length = buf[i] | buf[i + 1] << 8
                if length and len(buf) - i >= 3 and buf[i + 2] not in self.ROLES:
                    self._aligned = False
                    break
                if len(buf) - i < 2 + length:
                    break
                frame = bytes(buf[i:i + 2 + length])
                (data if length and frame[2] == reg.ROLES["data"] else first).append(frame)
                i += 2 + length
        if not self._aligned:
            first.append(bytes(buf[i:]))
            i = len(buf)
        del buf[:i]
        if not first and not data:
            return
        with self.cond:
            self.first.extend(first)
            for frame in data:
                if self.data_bytes + len(frame) > self.limit:
                    self.dropped_frames += 1
                    self.dropped_bytes += len(frame)
                    continue
                self.data.append(frame)
                self.data_bytes += len(frame)
            self.cond.notify_all()

    def next_chunk(self, most: int, timeout: float) -> bytes:
        """What goes to the host next: every waiting answer, else data frames up to `most` bytes (one at least)."""
        with self.cond:
            if not self.first and not self.data:
                self.cond.wait(timeout)
            if self.first:
                out = b"".join(self.first)
                self.first.clear()
                return out
            parts, n = [], 0
            while self.data and (not parts or n + len(self.data[0]) <= most):
                frame = self.data.popleft()
                parts.append(frame)
                n += len(frame)
            self.data_bytes -= n
            return b"".join(parts)

    def stats(self) -> dict:
        with self.cond:
            return {"split_dropped_frames": self.dropped_frames, "split_dropped_bytes": self.dropped_bytes}


def _child(vid: int, pid: int, serial: str | None, urb_size: int | None, depth: int | None, backlog_max: int | None,
           data_conn, cmd_conn) -> None:
    """UsbProcessStream's process: a UsbAsyncStream of its own (its event thread and its GIL away from the host's), a
    thread taking what came in at once and splitting it (_Split: answers ahead, data past the backlog dropped), a
    thread sending it to the host as it can take it, and the host's writes and requests on `cmd_conn`."""
    try:
        s = UsbAsyncStream.open(vid, pid, serial, urb_size=urb_size, depth=depth, backlog_max=backlog_max)
    except Exception as e:                           # noqa: BLE001  the host raises it
        cmd_conn.send(("err", f"{type(e).__name__}: {e}"))
        return
    cmd_conn.send(("ready", s.out_packet))
    stop = threading.Event()
    split = _Split(s.backlog_max)

    def take() -> None:
        while not stop.is_set():
            with s._cond:
                if not s._buffer:
                    s._cond.wait(0.05)
                # all of it, at once: the split holds what the host has not taken, so the IN side is never held
                data = s.take_locked(len(s._buffer))
            s.release_held()
            if data:
                split.feed(data)

    def send() -> None:
        while not stop.is_set():
            data = split.next_chunk(ProcessStreamChunk, 0.05)
            try:
                if data:
                    data_conn.send_bytes(data)
            except (OSError, EOFError):
                return

    taker = threading.Thread(target=take, daemon=True)
    sender = threading.Thread(target=send, daemon=True)
    taker.start()
    sender.start()
    try:
        while True:
            try:
                kind, arg = cmd_conn.recv()
            except (EOFError, OSError):
                break
            if kind == "close":
                break
            try:
                if kind == "write":
                    cmd_conn.send(("ok", s.write(arg)))
                elif kind == "stats":
                    cmd_conn.send(("ok", {**s.stats(), **split.stats()}))
                else:
                    cmd_conn.send(("err", f"unknown request {kind!r}"))
            except Exception as e:                   # noqa: BLE001
                cmd_conn.send(("err", f"{type(e).__name__}: {e}"))
    finally:
        stop.set()
        taker.join(timeout=0.5)
        sender.join(timeout=0.5)
        s.close()
        data_conn.close()


ProcessStreamChunk = 1 << 20   # the most one data message carries to the host


class _Proc:
    """A Popen shaped as UsbProcessStream uses a process: join, is_alive, kill."""

    def __init__(self, popen):
        self.popen = popen

    def join(self, timeout=None):
        import subprocess
        try:
            self.popen.wait(timeout)
        except subprocess.TimeoutExpired:
            pass

    def is_alive(self):
        return self.popen.poll() is None

    def kill(self):
        self.popen.kill()


class UsbProcessStream:
    """UsbAsyncStream in a process of its own, shaped the same (read with a timeout, in_waiting, write,
    reset_input_buffer, close, stats). The IN transfers are taken back and sent out again there: a host thread
    holding the GIL (numpy, a large bytes join) no longer stops them - the P4X at 40 MB/s lost data whenever the event
    thread stopped longer than the probe's ring holds (26 ms; 6 s once beside a host under load, wireskein
    2026-10-20). What came in waits in that process until this one reads it."""
    READER = "usb-process"
    # The bytes waiting for read() here at most: past it this process stops taking the pipe, so the stream's process
    # holds them (_Split: data past its backlog dropped there) - memory stays bounded whatever the host keeps up with.
    # Small: an answer waits behind what is here, and the stream's process puts answers ahead only of what it holds.
    BUFFER_MAX = 4 * 1024 * 1024

    def __init__(self, process, data_conn, cmd_conn, out_packet: int):
        self.process, self._data, self._cmd, self.out_packet = process, data_conn, cmd_conn, out_packet
        self.timeout = 0.05
        self._buffer = bytearray()
        self._cond = threading.Condition()
        self._cmd_lock = threading.Lock()
        self._closed = False
        self._reader = threading.Thread(target=self._take, daemon=True)
        self._reader.start()
        atexit.register(self.close)

    @classmethod
    def open(cls, vid: int, pid: int, serial: str | None = None, *, urb_size: int | None = None,
             depth: int | None = None, backlog_max: int | None = None, start_timeout: float = 10.0) -> "UsbProcessStream":
        """The process is `python -m oep_client.usb_stream` (not multiprocessing's spawn: that runs the caller's
        __main__ again, a script without a main guard twice), the data on a pipe and the requests on a socket pair."""
        import os
        import socket
        import subprocess
        import sys
        from multiprocessing.connection import Connection
        data_r, data_w = os.pipe()
        cmd_parent, cmd_child = socket.socketpair()
        package_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env = dict(os.environ, PYTHONPATH=os.pathsep.join(filter(None, [package_root, os.environ.get("PYTHONPATH")])))
        # this process's sizes (UsbAsyncStream.URB_SIZE / DEPTH / backlog_bytes as set here), not the new process's own
        urb_size, depth = urb_size or UsbAsyncStream.URB_SIZE, depth or UsbAsyncStream.DEPTH
        backlog_max = backlog_max or backlog_bytes(UsbAsyncStream.BACKLOG_MAX)
        args = [sys.executable, "-m", "oep_client.usb_stream", str(vid), str(pid), serial or "", str(urb_size),
                str(depth), str(backlog_max), str(data_w), str(cmd_child.fileno())]
        try:
            p = subprocess.Popen(args, pass_fds=(data_w, cmd_child.fileno()), env=env, stdin=subprocess.DEVNULL)
        except OSError as e:
            for fd in (data_r, data_w):
                os.close(fd)
            cmd_parent.close()
            cmd_child.close()
            raise ChildProcessError(f"the USB stream's process: {e}") from e
        os.close(data_w)
        cmd_child.close()
        data = Connection(data_r, writable=False)
        cmd = Connection(cmd_parent.detach())
        proc = _Proc(p)
        try:
            if not cmd.poll(start_timeout):
                raise TimeoutError("the USB stream's process did not start")
            kind, arg = cmd.recv()
        except EOFError:   # it ended before it was ready (not the device's answer): ChildProcessError
            proc.join(1.0)
            data.close()
            cmd.close()
            raise ChildProcessError("the USB stream's process ended before it was ready") from None
        except Exception:
            proc.kill()
            data.close()
            cmd.close()
            raise
        if kind != "ready":
            proc.join(1.0)
            data.close()
            cmd.close()
            raise FileNotFoundError(arg)
        return cls(proc, data, cmd, arg)

    def _take(self) -> None:
        while not self._closed:
            with self._cond:
                while len(self._buffer) > self.BUFFER_MAX and not self._closed:
                    self._cond.wait(0.1)           # read() takes some, then the pipe again
            try:
                data = self._data.recv_bytes()
            except (EOFError, OSError):
                break
            with self._cond:
                self._buffer += data
                self._cond.notify_all()

    def _request(self, kind: str, arg=None):
        with self._cmd_lock:
            if self._closed:
                raise OSError("the USB stream is closed")
            try:
                self._cmd.send((kind, arg))
                status, value = self._cmd.recv()
            except (EOFError, OSError) as e:
                raise OSError("the USB stream's process is gone") from e
        if status != "ok":
            raise OSError(value)
        return value

    @property
    def in_waiting(self) -> int:
        with self._cond:
            return len(self._buffer)

    def read(self, n: int = 1) -> bytes:
        deadline = time.monotonic() + (self.timeout or 0)
        with self._cond:
            while not self._buffer:
                left = deadline - time.monotonic()
                if left <= 0:
                    return b""
                self._cond.wait(left)
            out = bytes(self._buffer[:n])
            del self._buffer[:n]
            self._cond.notify_all()                # _take may wait for room
            return out

    def write(self, data: bytes) -> int:
        return self._request("write", bytes(data))

    def reset_input_buffer(self) -> None:
        with self._cond:
            self._buffer.clear()
            self._cond.notify_all()

    def stats(self) -> dict:
        """The process's UsbAsyncStream.stats()."""
        return self._request("stats")

    def close(self) -> None:
        """The process cancels its transfers and closes the device; then it ends. Safe to call twice (atexit)."""
        if self._closed:
            return
        atexit.unregister(self.close)
        with self._cmd_lock:
            self._closed = True
            try:
                self._cmd.send(("close", None))
            except (OSError, EOFError):
                pass
        self.process.join(timeout=3.0)
        if self.process.is_alive():
            self.process.kill()
            self.process.join(timeout=1.0)
        self._reader.join(timeout=1.0)
        for c in (self._data, self._cmd):
            try:
                c.close()
            except OSError:
                pass


if __name__ == "__main__":   # UsbProcessStream's process: vid pid serial urb_size depth backlog_max data_fd cmd_fd
    import sys
    from multiprocessing.connection import Connection
    _vid, _pid, _serial, _urb, _depth, _backlog, _data_fd, _cmd_fd = sys.argv[1:9]
    _child(int(_vid), int(_pid), _serial or None, int(_urb) or None, int(_depth) or None, int(_backlog) or None,
           Connection(int(_data_fd), readable=False), Connection(int(_cmd_fd)))
