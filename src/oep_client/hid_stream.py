"""OEP over a vendor-defined USB HID interface (oep-spec oep-core §3): the way in when vendor bulk is not
usable (no WinUSB / udev permission for raw USB), before a CDC port, which a probe may give to serial forwarding.

Framing: every report carries count (u16 LE), first (u16 LE: where the first frame starting in the report begins,
hid_no_frame_start when none) and then count bytes of the length-prefixed frame stream, the rest of the report zero
(transports §1). An OS HID driver drops reports read too late; a reader whose own reading puts the next frame's start
elsewhere than first has lost one, drops the frame begun and reads on from first. Report IDs are the HID transport's business, not OEP's: when the report descriptor declares one, every
report on the wire starts with it - output reports too (EspUsbDevice takes a first byte equal to its report ID as the
ID, so a report sent without it lost a byte whenever count's low byte matched).

Backends, same shape as UsbBulkStream (read with a timeout, in_waiting, write, reset_input_buffer, close):
  hidapi (`hid`, cython-hidapi): the OS HID driver - Windows, macOS, Linux hidraw. Needs no driver change on Windows.
  libusb1 (`usb1`): detaches the kernel's HID driver and talks to the endpoints (Linux, raw USB permission).
"""

from __future__ import annotations

import errno
import os
import sys
import threading
import time

from . import registry as reg

HID_USAGE_PAGE, HID_USAGE = reg.USB["hid_usage_page"], reg.USB["hid_usage"]   # OEP's HID: 0xFF4F, 0x45 (transports §3)
HEAD = reg.USB["hid_report_header_bytes"]          # count(u16), first(u16) (after the report ID, if any)
NO_START = reg.USB["hid_no_frame_start"]


class FrameTrack:
    """Where the frames of a length-prefixed stream start: the frame begun so far (`partial`, b"" at a frame's start)."""

    def __init__(self):
        self.partial = bytearray()

    def next_start(self, data: bytes) -> int:
        """The position in `data` where the next frame starts, or NO_START when the frame begun runs past it."""
        p = self.partial
        if not p:
            return 0 if data else NO_START
        if len(p) == 1:
            if not data:
                return NO_START
            need = 2 + (p[0] | data[0] << 8) - 1
        else:
            need = 2 + (p[0] | p[1] << 8) - len(p)
        return need if need < len(data) else NO_START

    def feed(self, data: bytes) -> list[bytes]:
        """`data` read on: the frames it completes (each with its length)."""
        self.partial += data
        done = []
        while len(self.partial) >= 2:
            end = 2 + (self.partial[0] | self.partial[1] << 8)
            if len(self.partial) < end:
                break
            done.append(bytes(self.partial[:end]))
            del self.partial[:end]
        return done


def parse_report_descriptor(desc: bytes) -> list[dict]:
    """The reports of a HID report descriptor: [{usage_page, usage (the top-level collection's), report_id (0 = none),
    input, output}] with input/output in bytes (the report ID not counted). Short items only (long items are skipped);
    enough for a vendor interface."""
    reports: dict[tuple[int, int], dict] = {}
    usage_page = report_id = size = count = 0
    usage = top = depth = 0
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
        elif kind == 0x08 and tag == 0x00:               # local: Usage
            usage = value
        elif kind == 0x00 and tag == 0xA0:               # main: Collection
            top = usage if depth == 0 else top
            depth += 1
        elif kind == 0x00 and tag == 0xC0:               # main: End Collection
            depth = max(0, depth - 1)
        elif kind == 0x00 and tag in (0x80, 0x90):       # main: Input / Output
            r = reports.setdefault((usage_page, report_id),
                                   {"usage_page": usage_page, "usage": top, "report_id": report_id, "input": 0,
                                    "output": 0})
            r["input" if tag == 0x80 else "output"] += (size * count + 7) // 8
    return list(reports.values())


def vendor_report(desc: bytes) -> dict | None:
    """OEP's report carrying both directions - usage page 0xFF4F, usage 0x45 (transports §3: another vendor HID is not
    taken) - if the descriptor has one."""
    for r in parse_report_descriptor(desc):
        if (r["usage_page"], r["usage"]) == (HID_USAGE_PAGE, HID_USAGE) and r["input"] >= 3 and r["output"] >= 3:
            return r
    return None


class HidReportStream:
    """count(u16) + first(u16) + frame bytes per report over a backend that moves whole reports (payload without the
    report ID). What read() gives is whole frames only: a report found missing (rule 6) drops the frame begun here, so
    the link's reading never loses a boundary - the frames lost show as an answer that does not come or a push's seq
    (core §10.2). `report_losses`: reports found missing or broken."""

    def __init__(self, backend, input_size: int, output_size: int):
        self.backend = backend
        self.input_size, self.output_size = input_size, output_size
        self.timeout = 0.05
        self.report_losses = 0
        self._out = FrameTrack()                         # the frames written, for each report's first
        self._buffer = bytearray()
        self._cond = threading.Condition()
        self._closed = False
        self._reader = threading.Thread(target=self._drain, daemon=True)
        self._reader.start()

    def _drain(self) -> None:
        track, synced = FrameTrack(), False              # just opened: read from the first report with a first
        while not self._closed:
            try:
                report = self.backend.read_report(0.1)
            except OSError:
                if self._closed:
                    return
                time.sleep(0.01)
                continue
            if len(report) < HEAD:
                continue
            count, first = report[0] | report[1] << 8, report[2] | report[3] << 8
            if count > len(report) - HEAD or (first != NO_START and first >= count):
                track, synced = FrameTrack(), False      # rule 7: dropped, the stream broken till the next first
                self.report_losses += 1
                continue
            if not count:
                continue
            data = report[HEAD:HEAD + count]
            if synced and track.next_start(data) != first:
                track, synced = FrameTrack(), False      # rule 6: a report is missing before this one
                self.report_losses += 1
            if not synced:
                if first == NO_START:
                    continue
                data, synced = data[first:], True
            frames = track.feed(data)
            if frames:
                with self._cond:
                    for f in frames:
                        self._buffer += f
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
        """`data`: length-prefixed frames, as the link writes them; each report says where its first frame starts."""
        room = self.output_size - HEAD
        for i in range(0, len(data), room):
            part = data[i:i + room]
            first = self._out.next_start(part)
            self._out.feed(part)
            self.backend.write_report(len(part).to_bytes(2, "little") + first.to_bytes(2, "little") + part
                                      + bytes(room - len(part)))
        return len(data)

    def reset_input_buffer(self) -> None:
        with self._cond:
            self._buffer.clear()

    def close(self) -> None:
        self._closed = True
        self._reader.join(timeout=0.5)
        self.backend.close()


class HidBusy(OSError):
    """The probe's HID node is held by another program (transports §3: a host opens the HID exclusively where the OS
    allows it) - not taken over through libusb either."""


def _lock_node(path) -> int | None:
    """transports §3: the HID opened exclusively where the OS allows it - on Linux an flock (LOCK_EX | LOCK_NB) on the
    hidraw node hidapi opened, as pyserial's exclusive=True on a serial port; macOS's hidapi opens the device exclusively
    by itself. -> the locked fd (None: nothing to lock); HidBusy when another program holds it."""
    name = path.decode(errors="replace") if isinstance(path, bytes) else str(path)
    if not sys.platform.startswith("linux") or not os.path.exists(name):
        return None
    import fcntl
    fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        os.close(fd)
        if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
            raise HidBusy(errno.EBUSY, f"{name} is open in another program") from e
        raise
    return fd


class HidapiBackend:
    def __init__(self, device, report_id: int, lock_fd: int | None = None):
        self.device, self.report_id, self.lock_fd = device, report_id, lock_fd

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
        try:
            self.device.close()
        finally:
            if self.lock_fd is not None:
                os.close(self.lock_fd)                   # the flock goes with it
                self.lock_fd = None


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
        if info.get("usage_page", 0) and info["usage_page"] != HID_USAGE_PAGE:
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
        try:
            lock = _lock_node(info["path"])                  # exclusive (transports §3)
        except OSError:
            dev.close()
            raise
        return HidReportStream(HidapiBackend(dev, r["report_id"], lock), r["input"], r["output"])
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
            # the interface ours before anything goes to it: with the kernel's HID driver bound, Linux usbfs refuses
            # even its GET_DESCRIPTOR (LIBUSB_ERROR_IO; an RP2's HID at interface 3, 2026-10-22), and gives it back
            # when the descriptor is not OEP's
            detached = bool(handle.kernelDriverActive(number))
            if detached:
                handle.detachKernelDriver(number)
            handle.claimInterface(number)
            # GET_DESCRIPTOR (report) on the interface
            desc = bytes(handle.controlRead(0x81, 0x06, 0x2200, number, 4096, timeout=1000))
            r = vendor_report(desc)
            if r is None:
                handle.releaseInterface(number)
                if detached:
                    handle.attachKernelDriver(number)
                continue
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
        except HidBusy:
            raise                                        # held by another host: libusb would take it from that host
        except ImportError as e:
            errors.append(f"{opener.__name__}: {e}")
        except (OSError, FileNotFoundError) as e:
            errors.append(f"{opener.__name__}: {e}")
        except Exception as e:                           # usb1.USBError (access denied, busy) is not an OSError
            errors.append(f"{opener.__name__}: {type(e).__name__}: {e}")
    raise FileNotFoundError("; ".join(errors))
