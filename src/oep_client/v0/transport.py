"""Length-prefixed frames over a pyserial-like object (read/write/in_waiting/timeout)."""

from __future__ import annotations

import struct
import time


class FrameTransport:
    """The stream must be reliable (USB CDC, USB-Serial/JTAG, TCP). No CRC, no resync:
    when framing is lost the caller reopens the connection."""

    def __init__(self, stream, max_frame: int = 0xFFFF):
        self._stream = stream
        self._buffer = bytearray()
        self.max_frame = max_frame
        self.bytes_sent = 0
        self.bytes_received = 0

    def send(self, message: bytes) -> None:
        if not message or len(message) > self.max_frame:
            raise ValueError(f"message length {len(message)} outside 1..{self.max_frame}")
        self._stream.write(struct.pack("<H", len(message)) + message)
        self.bytes_sent += len(message) + 2

    def send_many(self, messages) -> None:
        """Several frames in one write; the peer reads a byte stream so boundaries are the length prefixes."""
        chunk = bytearray()
        for message in messages:
            if not message or len(message) > self.max_frame:
                raise ValueError(f"message length {len(message)} outside 1..{self.max_frame}")
            chunk += struct.pack("<H", len(message)) + message
        if chunk:
            self._stream.write(bytes(chunk))
            self.bytes_sent += len(chunk)

    def _fill(self, deadline: float) -> bool:
        """Read whatever is available; block for the first byte until deadline."""
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        self._stream.timeout = min(remaining, 0.05)
        data = self._stream.read(1)
        if not data:
            return False
        waiting = getattr(self._stream, "in_waiting", 0)
        if waiting:
            data += self._stream.read(waiting)
        self._buffer += data
        self.bytes_received += len(data)
        return True

    def recv(self, timeout: float) -> bytes | None:
        deadline = time.monotonic() + timeout
        while True:
            if len(self._buffer) >= 2:
                length = self._buffer[0] | (self._buffer[1] << 8)
                if length == 0:
                    del self._buffer[:2]
                    continue
                if length > self.max_frame:
                    raise ConnectionError(f"frame length {length} exceeds max_frame {self.max_frame}; framing lost (first bytes {bytes(self._buffer[:48])!r})")
                if len(self._buffer) >= 2 + length:
                    message = bytes(self._buffer[2:2 + length])
                    del self._buffer[:2 + length]
                    return message
            if not self._fill(deadline):
                if time.monotonic() >= deadline:
                    return None

    def discard_input(self) -> None:
        self._buffer.clear()
        reset = getattr(self._stream, "reset_input_buffer", None)
        if reset:
            reset()


class BulkTransport:
    """Length-prefixed frames over a USB vendor bulk endpoint pair (pyusb). The device treats
    the bulk stream as bytes and finds frame boundaries itself, so several frames may travel
    in one bulk write: send_many() is what makes pipelining fast over usbip (E160: 5 MB/s
    with 16 frames per URB against 1.3 MB/s one frame per URB)."""

    def __init__(self, device, endpoint_in, endpoint_out, max_frame: int = 0xFFFF):
        import threading
        self.device, self.ep_in, self.ep_out = device, endpoint_in, endpoint_out
        self._buffer = bytearray()
        self.max_frame = max_frame
        self.bytes_sent = 0
        self.bytes_received = 0
        # A bulk write blocks until the device accepts the data, and the device stops accepting
        # once its response FIFO is full and nobody reads it. Draining IN on a thread breaks that
        # cycle (E160's echo host did the same); recv() then only parses what the thread buffered.
        self._cond = threading.Condition()
        self._closed = False
        self._reader = threading.Thread(target=self._drain, daemon=True)
        self._reader.start()

    def _drain(self):
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
                    self.bytes_received += len(data)
                    self._cond.notify_all()

    @classmethod
    def open(cls, vid: int, pid: int, serial: str | None = None, *, timeout_s: float = 10.0):
        import usb.core
        import usb.util
        deadline = time.monotonic() + timeout_s
        device = None
        while device is None:
            for candidate in usb.core.find(find_all=True, idVendor=vid, idProduct=pid):
                if serial is None or (usb.util.get_string(candidate, candidate.iSerialNumber) or "") == serial:
                    device = candidate
                    break
            if device is None:
                if time.monotonic() >= deadline:
                    raise ConnectionError(f"USB {vid:04x}:{pid:04x} serial={serial!r} not found")
                time.sleep(0.2)
        try:
            device.get_active_configuration()
        except usb.core.USBError:
            device.set_configuration()
        intf = next(i for i in device.get_active_configuration()
                    if usb.util.find_descriptor(i, custom_match=lambda e: usb.util.endpoint_type(e.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK) is not None)
        usb.util.claim_interface(device, intf.bInterfaceNumber)
        ep_in = usb.util.find_descriptor(intf, custom_match=lambda e: usb.util.endpoint_type(e.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK
                                         and usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN)
        ep_out = usb.util.find_descriptor(intf, custom_match=lambda e: usb.util.endpoint_type(e.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK
                                          and usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT)
        return cls(device, ep_in, ep_out)

    def send(self, message: bytes) -> None:
        self.send_many([message])

    def send_many(self, messages) -> None:
        chunk = bytearray()
        for message in messages:
            if not message or len(message) > self.max_frame:
                raise ValueError(f"message length {len(message)} outside 1..{self.max_frame}")
            chunk += struct.pack("<H", len(message)) + message
        if chunk:
            self.ep_out.write(bytes(chunk), timeout=2000)
            self.bytes_sent += len(chunk)

    def recv(self, timeout: float) -> bytes | None:
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                if len(self._buffer) >= 2:
                    length = self._buffer[0] | (self._buffer[1] << 8)
                    if length == 0:
                        del self._buffer[:2]
                        continue
                    if length > self.max_frame:
                        raise ConnectionError(f"frame length {length} exceeds max_frame {self.max_frame}; framing lost (first bytes {bytes(self._buffer[:48])!r})")
                    if len(self._buffer) >= 2 + length:
                        message = bytes(self._buffer[2:2 + length])
                        del self._buffer[:2 + length]
                        return message
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cond.wait(remaining)

    def discard_input(self) -> None:
        time.sleep(0.1)
        with self._cond:
            self._buffer.clear()

    def close(self) -> None:
        import usb.util
        self._closed = True
        self._reader.join(timeout=1.0)
        usb.util.dispose_resources(self.device)
