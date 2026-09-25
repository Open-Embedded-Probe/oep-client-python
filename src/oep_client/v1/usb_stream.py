"""A USB vendor bulk endpoint pair shaped like the part of a pyserial port the v1 link uses (read with a timeout,
in_waiting, write, reset_input_buffer), so length-prefixed frames run over it unchanged. pyusb is imported only here.

IN is drained on a thread: a bulk write blocks until the device takes the data, and the device stops taking data
once its answers fill its FIFO and nobody reads them (E160)."""

from __future__ import annotations

import threading
import time


class UsbBulkStream:
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
                data = self.ep_in.read(65536, timeout=50)
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
        return self.ep_out.write(data, timeout=2000)

    def reset_input_buffer(self) -> None:
        with self._cond:
            self._buffer.clear()

    def close(self) -> None:
        import usb.util
        self._closed = True
        self._reader.join(timeout=0.5)
        usb.util.release_interface(self.device, self.interface)
        usb.util.dispose_resources(self.device)
