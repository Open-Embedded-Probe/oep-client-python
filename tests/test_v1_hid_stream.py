"""The vendor HID way in: report descriptor parsing, count(u16) report framing, and open_usb_host's vendor -> HID order
(no USB device)."""

import queue
import time

import pytest

from oep_client.v1 import hid_stream, link


def espusbdevice_vendor_descriptor(size: int) -> bytes:
    """EspUsbDeviceHidVendor::buildReportDescriptor (report ID 6, input / output / feature of `size` bytes)."""
    count = bytes([0x96, size & 0xFF, size >> 8]) if size > 0xFF else bytes([0x95, size])
    return (bytes([0x06, 0x00, 0xFF, 0x09, 0x01, 0xA1, 0x01, 0x85, 0x06, 0x15, 0x00, 0x26, 0xFF, 0x00, 0x75, 0x08])
            + count + bytes([0x09, 0x01, 0x81, 0x02, 0x09, 0x01, 0x91, 0x02, 0x09, 0x01, 0xB1, 0x02, 0xC0]))


KEYBOARD = bytes([0x05, 0x01, 0x09, 0x06, 0xA1, 0x01, 0x05, 0x07, 0x19, 0xE0, 0x29, 0xE7, 0x15, 0x00, 0x25, 0x01,
                  0x75, 0x01, 0x95, 0x08, 0x81, 0x02, 0x95, 0x01, 0x75, 0x08, 0x81, 0x01, 0x95, 0x05, 0x75, 0x01,
                  0x05, 0x08, 0x19, 0x01, 0x29, 0x05, 0x91, 0x02, 0x95, 0x01, 0x75, 0x03, 0x91, 0x01, 0xC0])


@pytest.mark.parametrize("size", [63, 511])
def test_the_espusbdevice_vendor_report_is_found_with_its_id_and_sizes(size):
    r = hid_stream.vendor_report(espusbdevice_vendor_descriptor(size))
    assert r == {"usage_page": 0xFF00, "report_id": 6, "input": size, "output": size}


def test_a_keyboard_is_not_a_vendor_report():
    assert hid_stream.parse_report_descriptor(KEYBOARD) == [{"usage_page": 7, "report_id": 0, "input": 8, "output": 0}] \
        or hid_stream.vendor_report(KEYBOARD) is None
    assert hid_stream.vendor_report(KEYBOARD) is None


class Backend:
    def __init__(self):
        self.inbox: queue.Queue[bytes] = queue.Queue()
        self.sent: list[bytes] = []
        self.closed = False

    def read_report(self, timeout):
        try:
            return self.inbox.get(timeout=timeout)
        except queue.Empty:
            return b""

    def write_report(self, payload):
        self.sent.append(payload)

    def close(self):
        self.closed = True


def report(data: bytes, size: int = 511) -> bytes:
    return len(data).to_bytes(2, "little") + data + bytes(size - 2 - len(data))


def test_writes_are_cut_into_whole_reports_with_their_count():
    b = Backend()
    s = hid_stream.HidReportStream(b, 511, 511)
    try:
        data = bytes(range(256)) * 4          # 1024 bytes: 509 + 509 + 6
        s.write(data)
        assert [len(r) for r in b.sent] == [511, 511, 511]
        assert [r[0] | r[1] << 8 for r in b.sent] == [509, 509, 6]
        assert b"".join(r[2:2 + (r[0] | r[1] << 8)] for r in b.sent) == data
        assert b.sent[2][8:] == bytes(511 - 8)
    finally:
        s.close()
    assert b.closed


def test_reads_join_the_counted_bytes_and_drop_the_padding():
    b = Backend()
    s = hid_stream.HidReportStream(b, 511, 511)
    try:
        b.inbox.put(report(b"\x03\x00abc"))
        b.inbox.put(report(b""))
        b.inbox.put(report(b"de"))
        deadline = time.monotonic() + 1
        while s.in_waiting < 7 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert s.read(100) == b"\x03\x00abcde"
    finally:
        s.close()


def test_a_count_larger_than_the_report_is_cut_to_the_report():
    b = Backend()
    s = hid_stream.HidReportStream(b, 8, 8)
    try:
        b.inbox.put(b"\xff\x00abcdef")
        deadline = time.monotonic() + 1
        while s.in_waiting < 6 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert s.read(100) == b"abcdef"
    finally:
        s.close()


class Id6Device:
    """A hidapi device that answers with report ID 6 and records what was written."""

    def __init__(self):
        self.written, self.reads = [], [bytes([6]) + report(b"xy"), bytes([5]) + report(b"no")]

    def read(self, n, timeout_ms):
        return list(self.reads.pop(0)) if self.reads else []

    def write(self, data):
        self.written.append(bytes(data))
        return len(data)

    def close(self):
        pass


def test_hidapi_reports_carry_the_report_id_both_ways():
    dev = Id6Device()
    b = hid_stream.HidapiBackend(dev, 6)
    assert b.read_report(0.1)[:4] == b"\x02\x00xy"
    assert b.read_report(0.1) == b""          # another report ID is not ours
    b.write_report(report(b"q"))
    assert dev.written[0][0] == 6 and dev.written[0][1:4] == b"\x01\x00q"


def test_open_usb_host_falls_back_from_vendor_to_hid(monkeypatch):
    tried = []

    def fake_open(kind, vid, pid, serial):
        tried.append(kind)
        if kind == "vendor":
            raise PermissionError("LIBUSB_ERROR_ACCESS")
        raise FileNotFoundError("stop here")

    monkeypatch.setattr(link, "_open_usb_stream", fake_open)
    with pytest.raises(FileNotFoundError) as e:
        link.open_usb_host()
    assert tried == ["vendor", "hid"]
    assert "vendor: LIBUSB_ERROR_ACCESS" in str(e.value) and "hid: stop here" in str(e.value)
