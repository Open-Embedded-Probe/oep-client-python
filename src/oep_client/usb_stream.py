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
    ~47 MB/s over usbipd; 64 KiB x 32 carried 2 x 160 Msps here) is taken without gaps. A transfer completes when full or on a short packet; the probe ends a
    transfer whose length is a whole number of packets with a zero-length packet, so small answers never wait."""

    # 64 KiB x 32: a probe streaming whole-packet frames (16 KiB) completes a read every four frames, so the latency at
    # a low rate stays short (1 MiB reads waited 0.8 s at 1.25 MB/s), and 2 MiB stay queued for the host's hiccups
    URB_SIZE = 64 * 1024
    DEPTH = 32

    def __init__(self, context, handle, endpoint_in: int, endpoint_out: int, interface: int):
        import usb1
        self._usb1 = usb1
        self.context, self.handle, self.ep_in, self.ep_out, self.interface = context, handle, endpoint_in, endpoint_out, interface
        self.timeout = 0.05
        self._buffer = bytearray()
        self._cond = threading.Condition()
        self._closed = False           # no more resubmits (close() started)
        self._stop = False             # the event thread ends (every transfer is back)
        self._transfers = []
        self._active = 0               # transfers submitted and not yet back for good
        for _ in range(self.DEPTH):
            t = handle.getTransfer()
            t.setBulk(endpoint_in, self.URB_SIZE, callback=self._done, timeout=0)
            t.submit()
            self._transfers.append(t)
            self._active += 1
        self._events = threading.Thread(target=self._run, daemon=True)
        self._events.start()
        # Closed at exit too: left to usb1's own finalizers, the context went while the event thread was inside
        # handleEventsTimeout with transfers out - the process hung at exit or libusb aborted (usbi_mutex_destroy).
        atexit.register(self.close)

    @classmethod
    def open(cls, vid: int, pid: int, serial: str | None = None) -> "UsbAsyncStream":
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
                ins = [a for a, attr in eps if attr & 3 == 2 and a & 0x80]
                outs = [a for a, attr in eps if attr & 3 == 2 and not a & 0x80]
                if ins and outs:
                    handle.claimInterface(setting.getNumber())
                    return cls(context, handle, ins[0], outs[0], setting.getNumber())
            handle.close()
        context.close()
        raise FileNotFoundError(f"no USB device {vid:04x}:{pid:04x}" + (f" serial {serial}" if serial else ""))

    def _done(self, transfer) -> None:
        usb1 = self._usb1
        status = transfer.getStatus()
        if status == usb1.TRANSFER_COMPLETED:
            n = transfer.getActualLength()
            if n:
                data = transfer.getBuffer()[:n]
                with self._cond:
                    self._buffer += data
                    self._cond.notify_all()
        if not self._closed and status in (usb1.TRANSFER_COMPLETED, usb1.TRANSFER_TIMED_OUT):
            try:
                transfer.submit()
                return
            except usb1.USBError:
                pass
        with self._cond:
            self._active -= 1          # back for good (cancelled, failed, or closing)
            self._cond.notify_all()

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
        if data and len(data) % 512 == 0:   # end the probe's receive transfer (a probe reading large transfers needs it)
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
