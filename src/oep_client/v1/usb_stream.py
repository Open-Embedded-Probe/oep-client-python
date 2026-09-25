"""A USB vendor bulk endpoint pair shaped like the part of a pyserial port the v1 link uses (read with a timeout,
in_waiting, write, reset_input_buffer), so length-prefixed frames run over it unchanged. pyusb is imported only here.

IN is drained on a thread: a bulk write blocks until the device takes the data, and the device stops taking data
once its answers fill its FIFO and nobody reads them (E160).

Each IN read asks for READ_SIZE bytes. A read completes when that much has arrived or a short packet ends the
transfer; if its timeout hits first, pyusb raises and the bytes already received are lost. Under a continuous stream
(12.5 MB/s, 2026-09-25) a 64 KiB read waited for more than its 50 ms and dropped whole answers, so reads stay small."""

from __future__ import annotations

import threading
import time


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
            eps = [e for e in intf if usb.util.endpoint_type(e.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK]
            ins = [e for e in eps if usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN]
            outs = [e for e in eps if usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT]
            if ins and outs:
                usb.util.claim_interface(dev, intf.bInterfaceNumber)
                return cls(dev, ins[0], outs[0], intf.bInterfaceNumber)
        raise FileNotFoundError("no bulk IN/OUT pair on the device")

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
        self._closed = False
        self._transfers = []
        for _ in range(self.DEPTH):
            t = handle.getTransfer()
            t.setBulk(endpoint_in, self.URB_SIZE, callback=self._done, timeout=0)
            t.submit()
            self._transfers.append(t)
        self._events = threading.Thread(target=self._run, daemon=True)
        self._events.start()

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
            transfer.submit()

    def _run(self) -> None:
        while not self._closed:
            try:
                self.context.handleEventsTimeout(0.1)
            except self._usb1.USBErrorInterrupted:
                continue

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
        self._closed = True
        for t in self._transfers:
            try:
                t.cancel()
            except self._usb1.USBError:
                pass
        self._events.join(timeout=0.5)
        try:
            self.handle.releaseInterface(self.interface)
        finally:
            self.handle.close()
            self.context.close()
