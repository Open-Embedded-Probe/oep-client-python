"""OEP over a vendor-defined USB HID interface (oep-spec v1-core-wire-delta §1): the way in when vendor bulk is not
usable (no WinUSB / udev permission for raw USB), before a CDC port, which a probe may give to serial forwarding.

Framing: every report carries count (u16 LE) and then that many bytes of the length-prefixed frame stream, the rest of
the report zero. Report IDs are the HID transport's business, not OEP's: when the report descriptor declares one, every
report on the wire starts with it - output reports too (EspUsbDevice takes a first byte equal to its report ID as the
ID, so a report sent without it lost a byte whenever count's low byte matched).

Backends, same shape as UsbBulkStream (read with a timeout, in_waiting, write, reset_input_buffer, close):
  hidapi (`hid`, cython-hidapi): the OS HID driver - Windows, macOS, Linux hidraw. Needs no driver change on Windows.
  libusb1 (`usb1`): detaches the kernel's HID driver and talks to the endpoints (Linux, raw USB permission).
"""

from __future__ import annotations

import threading
import time

VENDOR_USAGE_PAGE_MIN = 0xFF00


def parse_report_descriptor(desc: bytes) -> list[dict]:
    """The reports of a HID report descriptor: [{usage_page, report_id (0 = none), input, output}] with input/output in
    bytes (the report ID not counted). Short items only (long items are skipped); enough for a vendor interface."""
    reports: dict[tuple[int, int], dict] = {}
    usage_page = report_id = size = count = 0
    stack = []
    i = 0
    while i < len(desc):
        prefix = desc[i]
        if prefix == 0xFE:                               # long item: size in the next byte
            i += 3 + (desc[i + 1] if i + 1 < len(desc) else 0)
            continue
        n = (0, 1, 2, 4)[prefix & 3]
        value = int.from_bytes(desc[i + 1:i + 1 + n], "little")
        tag, kind = prefix & 0xF0, prefix & 0x0C
        i += 1 + n
        if kind == 0x04:                                 # global
            if tag == 0x00:
                usage_page = value
            elif tag == 0x70:
                size = value
            elif tag == 0x80:
                report_id = value
            elif tag == 0x90:
                count = value
            elif tag == 0xA0:
                stack.append((usage_page, report_id, size, count))
            elif tag == 0xB0 and stack:
                usage_page, report_id, size, count = stack.pop()
        elif kind == 0x00 and tag in (0x80, 0x90):       # main: Input / Output
            r = reports.setdefault((usage_page, report_id),
                                   {"usage_page": usage_page, "report_id": report_id, "input": 0, "output": 0})
            r["input" if tag == 0x80 else "output"] += (size * count + 7) // 8
    return list(reports.values())


def vendor_report(desc: bytes) -> dict | None:
    """The vendor-page report carrying both directions, if the descriptor has one."""
    for r in parse_report_descriptor(desc):
        if r["usage_page"] >= VENDOR_USAGE_PAGE_MIN and r["input"] >= 3 and r["output"] >= 3:
            return r
    return None


class HidReportStream:
    """count(u16) + frame bytes per report over a backend that moves whole reports (payload without the report ID)."""

    def __init__(self, backend, input_size: int, output_size: int):
        self.backend = backend
        self.input_size, self.output_size = input_size, output_size
        self.timeout = 0.05
        self._buffer = bytearray()
        self._cond = threading.Condition()
        self._closed = False
        self._reader = threading.Thread(target=self._drain, daemon=True)
        self._reader.start()

    def _drain(self) -> None:
        while not self._closed:
            try:
                report = self.backend.read_report(0.1)
            except OSError:
                if self._closed:
                    return
                time.sleep(0.01)
                continue
            if len(report) < 2:
                continue
            n = min(report[0] | report[1] << 8, len(report) - 2)
            if n:
                with self._cond:
                    self._buffer += report[2:2 + n]
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
        room = self.output_size - 2
        for i in range(0, len(data), room):
            part = data[i:i + room]
            self.backend.write_report(len(part).to_bytes(2, "little") + part + bytes(room - len(part)))
        return len(data)

    def reset_input_buffer(self) -> None:
        with self._cond:
            self._buffer.clear()

    def close(self) -> None:
        self._closed = True
        self._reader.join(timeout=0.5)
        self.backend.close()


class HidapiBackend:
    def __init__(self, device, report_id: int):
        self.device, self.report_id = device, report_id

    def read_report(self, timeout: float) -> bytes:
        data = bytes(self.device.read(65536, int(timeout * 1000)))
        if data and self.report_id:
            if data[0] != self.report_id:
                return b""
            data = data[1:]
        return data

    def write_report(self, payload: bytes) -> None:
        # hidapi always takes the report ID first; 0 when the descriptor declares none (not sent on the wire)
        if self.device.write(bytes([self.report_id]) + payload) < 0:
            raise OSError("HID write failed")

    def close(self) -> None:
        self.device.close()


class Usb1HidBackend:
    def __init__(self, context, handle, interface: int, ep_in: int, ep_out: int | None, report_id: int, input_size: int):
        import usb1
        self._usb1 = usb1
        self.context, self.handle, self.interface = context, handle, interface
        self.ep_in, self.ep_out, self.report_id = ep_in, ep_out, report_id
        self.in_len = input_size + (1 if report_id else 0)

    def read_report(self, timeout: float) -> bytes:
        try:
            data = bytes(self.handle.interruptRead(self.ep_in, self.in_len, timeout=int(timeout * 1000)))
        except self._usb1.USBErrorTimeout:
            return b""
        except self._usb1.USBError as e:
            raise OSError(str(e)) from e
        if data and self.report_id:
            if data[0] != self.report_id:
                return b""
            data = data[1:]
        return data

    def write_report(self, payload: bytes) -> None:
        wire = (bytes([self.report_id]) if self.report_id else b"") + payload
        if self.ep_out is not None:
            self.handle.interruptWrite(self.ep_out, wire, timeout=1000)
        else:                                            # SET_REPORT (output) on the control pipe
            self.handle.controlWrite(0x21, 0x09, 0x0200 | self.report_id, self.interface, wire, timeout=1000)

    def close(self) -> None:
        try:
            self.handle.releaseInterface(self.interface)
        finally:
            self.handle.close()
            self.context.close()


def _open_hidapi(vid: int, pid: int, serial: str | None) -> HidReportStream:
    import hid
    for info in hid.enumerate(vid, pid):
        if info.get("usage_page", 0) and info["usage_page"] < VENDOR_USAGE_PAGE_MIN:
            continue
        if serial and (info.get("serial_number") or "").lower() != serial.lower():
            continue
        dev = hid.device()
        dev.open_path(info["path"])
        try:
            desc = bytes(dev.get_report_descriptor())
        except (AttributeError, OSError):
            dev.close()
            continue
        r = vendor_report(desc)
        if r is None:
            dev.close()
            continue
        return HidReportStream(HidapiBackend(dev, r["report_id"]), r["input"], r["output"])
    raise FileNotFoundError(f"no vendor HID interface on {vid:04x}:{pid:04x}" + (f" serial {serial}" if serial else ""))


def _open_usb1(vid: int, pid: int, serial: str | None) -> HidReportStream:
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
            if setting.getClass() != 3:
                continue
            number = setting.getNumber()
            eps = [(e.getAddress(), e.getAttributes()) for e in setting]
            ins = [a for a, attr in eps if attr & 3 == 3 and a & 0x80]
            outs = [a for a, attr in eps if attr & 3 == 3 and not a & 0x80]
            if not ins:
                continue
            # GET_DESCRIPTOR (report) on the interface
            desc = bytes(handle.controlRead(0x81, 0x06, 0x2200, number, 4096, timeout=1000))
            r = vendor_report(desc)
            if r is None:
                continue
            if handle.kernelDriverActive(number):
                handle.detachKernelDriver(number)
            handle.claimInterface(number)
            backend = Usb1HidBackend(context, handle, number, ins[0], outs[0] if outs else None, r["report_id"], r["input"])
            return HidReportStream(backend, r["input"], r["output"])
        handle.close()
    context.close()
    raise FileNotFoundError(f"no vendor HID interface on {vid:04x}:{pid:04x}" + (f" serial {serial}" if serial else ""))


def open_hid(vid: int, pid: int, serial: str | None = None) -> HidReportStream:
    """The probe's vendor HID interface: hidapi first (the OS driver, no permission beyond the HID node), then
    libusb1."""
    errors = []
    for opener in (_open_hidapi, _open_usb1):
        try:
            return opener(vid, pid, serial)
        except ImportError as e:
            errors.append(f"{opener.__name__}: {e}")
        except (OSError, FileNotFoundError) as e:
            errors.append(f"{opener.__name__}: {e}")
        except Exception as e:                           # usb1.USBError (access denied, busy) is not an OSError
            errors.append(f"{opener.__name__}: {type(e).__name__}: {e}")
    raise FileNotFoundError("; ".join(errors))
