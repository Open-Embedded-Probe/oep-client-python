"""A USB vendor bulk endpoint pair shaped like the part of a pyserial port the v1 link uses (read with a timeout,
in_waiting, write, reset_input_buffer), so length-prefixed frames run over it unchanged. pyusb is imported only here.

IN is drained on a thread: a bulk write blocks until the device takes the data, and the device stops taking data
once its answers fill its FIFO and nobody reads them (E160).

Each IN read asks for READ_SIZE bytes. A read completes when that much has arrived or a short packet ends the
transfer; if its timeout hits first, pyusb raises and the bytes already received are lost. Under a continuous stream
(12.5 MB/s, 2026-09-25) a 64 KiB read waited for more than its 50 ms and dropped whole answers, so reads stay small."""

from __future__ import annotations

import atexit
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

    def __init__(self, context, handle, endpoint_in: int, endpoint_out: int, interface: int, out_packet: int = 512,
                 urb_size: int | None = None, depth: int | None = None):
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
        self._transfers = []
        self._active = 0               # transfers submitted and not yet back for good
        # what stats() reports: the transfers out at a callback's start (the fewest seen: how near the queue came to
        # empty - the kernel takes the stream only into transfers that are out), the longest between two callbacks,
        # the most bytes waiting for read()
        self._inflight = 0
        self._stats = {"callbacks": 0, "inflight_min": None, "gap_max_s": 0.0, "buffered_max": 0, "bytes": 0}
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
             depth: int | None = None) -> "UsbAsyncStream":
        """`urb_size` / `depth`: the IN transfers (URB_SIZE x DEPTH by default). Each transfer back is a callback that
        takes the GIL: fewer, larger ones keep up better beside a thread that holds it (a low rate's latency grows
        with the size)."""
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
                               urb_size=urb_size, depth=depth)
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
        # out again before the lock: a read() holding it does not keep the transfer back
        if not self._closed and status in (usb1.TRANSFER_COMPLETED, usb1.TRANSFER_TIMED_OUT):
            try:
                transfer.submit()
                self._inflight += 1
                resubmitted = True
            except usb1.USBError:
                resubmitted = False
        else:
            resubmitted = False
        with self._cond:
            if data:
                self._buffer += data
                st["bytes"] += len(data)
                if len(self._buffer) > st["buffered_max"]:
                    st["buffered_max"] = len(self._buffer)
            if not resubmitted:
                self._active -= 1      # back for good (cancelled, failed, or closing)
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
            out = bytes(self._buffer[:n])
            del self._buffer[:n]
            return out

    def write(self, data: bytes) -> int:
        n = self.handle.bulkWrite(self.ep_out, data, timeout=2000)
        if data and len(data) % self.out_packet == 0:   # a zero-length packet ends the transfer (transports §1)
            self.handle.bulkWrite(self.ep_out, b"", timeout=2000)
        return n

    def reset_input_buffer(self) -> None:
        with self._cond:
            self._buffer.clear()

    def close(self) -> None:
        """Cancel the IN transfers and let the event thread take them back before the handle and the context go
        (closing with transfers still out hung the process or aborted libusb). Safe to call twice (atexit)."""
        if self._closed:
            return
        self._closed = True
        atexit.unregister(self.close)
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


def _child(vid: int, pid: int, serial: str | None, urb_size: int | None, depth: int | None, data_conn, cmd_conn) -> None:
    """UsbProcessStream's process: a UsbAsyncStream of its own (its event thread and its GIL away from the host's), a
    thread sending what came in to the host as it can take it, and the host's writes and requests on `cmd_conn`."""
    try:
        s = UsbAsyncStream.open(vid, pid, serial, urb_size=urb_size, depth=depth)
    except Exception as e:                           # noqa: BLE001  the host raises it
        cmd_conn.send(("err", f"{type(e).__name__}: {e}"))
        return
    cmd_conn.send(("ready", s.out_packet))
    stop = threading.Event()

    def send() -> None:
        while not stop.is_set():
            with s._cond:
                if not s._buffer:
                    s._cond.wait(0.05)
                data = bytes(s._buffer)              # all of it: no memmove of a backlog at every send
                s._buffer.clear()
            view = memoryview(data)
            try:
                for at in range(0, len(data), ProcessStreamChunk):
                    data_conn.send_bytes(view[at:at + ProcessStreamChunk])
            except (OSError, EOFError):
                return

    sender = threading.Thread(target=send, daemon=True)
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
                    cmd_conn.send(("ok", s.stats()))
                else:
                    cmd_conn.send(("err", f"unknown request {kind!r}"))
            except Exception as e:                   # noqa: BLE001
                cmd_conn.send(("err", f"{type(e).__name__}: {e}"))
    finally:
        stop.set()
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
             depth: int | None = None, start_timeout: float = 10.0) -> "UsbProcessStream":
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
        # this process's sizes (UsbAsyncStream.URB_SIZE / DEPTH as set here), not the new process's own defaults
        urb_size, depth = urb_size or UsbAsyncStream.URB_SIZE, depth or UsbAsyncStream.DEPTH
        args = [sys.executable, "-m", "oep_client.usb_stream", str(vid), str(pid), serial or "", str(urb_size),
                str(depth), str(data_w), str(cmd_child.fileno())]
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
            return out

    def write(self, data: bytes) -> int:
        return self._request("write", bytes(data))

    def reset_input_buffer(self) -> None:
        with self._cond:
            self._buffer.clear()

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


if __name__ == "__main__":   # UsbProcessStream's process: vid pid serial urb_size depth data_fd cmd_fd
    import sys
    from multiprocessing.connection import Connection
    _vid, _pid, _serial, _urb, _depth, _data_fd, _cmd_fd = sys.argv[1:8]
    _child(int(_vid), int(_pid), _serial or None, int(_urb) or None, int(_depth) or None,
           Connection(int(_data_fd), readable=False), Connection(int(_cmd_fd)))
