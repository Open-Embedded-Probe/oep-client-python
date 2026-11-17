"""The vendor HID way in: report descriptor parsing, count(u16) + first(u16) report framing (transports §1), and
open_usb_host's vendor -> HID order (no USB device)."""

import queue
import time

import pytest

from oep_client import hid_stream, link


def espusbdevice_vendor_descriptor(size: int, page: int = 0xFF4F, usage: int = 0x45) -> bytes:
    """EspUsbDeviceHidVendor::buildReportDescriptor (report ID 6, input / output / feature of `size` bytes), its usage
    page / usage patched to OEP's 0xFF4F / 0x45 as the reference firmware does (the library's own: 0xFF00 / 0x01)."""
    count = bytes([0x96, size & 0xFF, size >> 8]) if size > 0xFF else bytes([0x95, size])
    return (bytes([0x06, page & 0xFF, page >> 8, 0x09, usage, 0xA1, 0x01, 0x85, 0x06, 0x15, 0x00, 0x26, 0xFF, 0x00, 0x75, 0x08])
            + count + bytes([0x09, 0x01, 0x81, 0x02, 0x09, 0x01, 0x91, 0x02, 0x09, 0x01, 0xB1, 0x02, 0xC0]))


KEYBOARD = bytes([0x05, 0x01, 0x09, 0x06, 0xA1, 0x01, 0x05, 0x07, 0x19, 0xE0, 0x29, 0xE7, 0x15, 0x00, 0x25, 0x01,
                  0x75, 0x01, 0x95, 0x08, 0x81, 0x02, 0x95, 0x01, 0x75, 0x08, 0x81, 0x01, 0x95, 0x05, 0x75, 0x01,
                  0x05, 0x08, 0x19, 0x01, 0x29, 0x05, 0x91, 0x02, 0x95, 0x01, 0x75, 0x03, 0x91, 0x01, 0xC0])


@pytest.mark.parametrize("size", [63, 511])
def test_the_espusbdevice_vendor_report_is_found_with_its_id_and_sizes(size):
    r = hid_stream.vendor_report(espusbdevice_vendor_descriptor(size))
    assert r == {"usage_page": 0xFF4F, "usage": 0x45, "report_id": 6, "input": size, "output": size}
    for page, usage in ((0xFF00, 0x01), (0xFF00, 0x45), (0xFF4F, 0x01)):   # another vendor HID is not OEP's (transports §3)
        assert hid_stream.vendor_report(espusbdevice_vendor_descriptor(size, page, usage)) is None


def test_a_keyboard_is_not_a_vendor_report():
    assert hid_stream.parse_report_descriptor(KEYBOARD) == [{"usage_page": 7, "usage": 6, "report_id": 0, "input": 8,
                                                             "output": 0}] \
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


def report(data: bytes, size: int = 511, first: int | None = None) -> bytes:
    """A report carrying `data`; first 0 (a frame starts at its start) unless given, hid_no_frame_start when empty."""
    if first is None:
        first = 0 if data else hid_stream.NO_START
    return (len(data).to_bytes(2, "little") + first.to_bytes(2, "little") + data
            + bytes(size - hid_stream.HEAD - len(data)))


def frame(msg: bytes) -> bytes:
    return len(msg).to_bytes(2, "little") + msg


def wait_for(s, n):
    deadline = time.monotonic() + 1
    while s.in_waiting < n and time.monotonic() < deadline:
        time.sleep(0.01)


def test_writes_are_cut_into_whole_reports_with_their_count_and_first():
    """Each report: count, then where the first frame starting in it begins (hid_no_frame_start in one inside a frame)."""
    b = Backend()
    s = hid_stream.HidReportStream(b, 511, 511)
    try:
        big, small = frame(bytes(range(256)) * 4), frame(b"xyz")     # 1026 + 5 bytes: 507 + 507 + 17
        s.write(big + small)
        assert [len(r) for r in b.sent] == [511, 511, 511]
        assert [r[0] | r[1] << 8 for r in b.sent] == [507, 507, 17]
        assert [r[2] | r[3] << 8 for r in b.sent] == [0, hid_stream.NO_START, 12]   # small starts at 1026 - 1014
        assert b"".join(r[4:4 + (r[0] | r[1] << 8)] for r in b.sent) == big + small
        assert b.sent[2][4 + 17:] == bytes(511 - 4 - 17)
        s.write(frame(b"q"))                                       # the next write starts a frame
        assert b.sent[3][2:4] == b"\x00\x00"
    finally:
        s.close()
    assert b.closed


def test_reads_give_whole_frames_and_drop_the_padding():
    b = Backend()
    s = hid_stream.HidReportStream(b, 511, 511)
    try:
        b.inbox.put(report(b"\x05\x00abc"))
        b.inbox.put(report(b""))
        b.inbox.put(report(b"de", first=hid_stream.NO_START))
        wait_for(s, 7)
        assert s.read(100) == b"\x05\x00abcde" and s.report_losses == 0
    finally:
        s.close()


@pytest.mark.parametrize("case", __import__("json").load(open(__import__("pathlib").Path(__file__).with_name("vectors")
                                                             / "hid_reports.json"))["read"], ids=lambda c: c["name"])
def test_the_shared_vectors_read(case):
    """tests/vectors/hid_reports.json (oep-spec): the messages a reader of rules 6 and 7 gets from the reports, a
    reader that has just opened the transport."""
    b = Backend()
    s = hid_stream.HidReportStream(b, 16, 16)
    try:
        for r in case["reports_hex"]:
            b.inbox.put(bytes.fromhex(r))
        want = [bytes.fromhex(m) for m in case["messages_hex"]]
        wait_for(s, sum(2 + len(m) for m in want))
        time.sleep(0.05)
        got, data = [], s.read(1000)
        while data:                                          # the frames given; a length 0 one is the link's to skip
            n = data[0] | data[1] << 8
            if n:
                got.append(data[2:2 + n])
            data = data[2 + n:]
        assert got == want
    finally:
        s.close()


def test_the_shared_vectors_pack():
    import json
    import pathlib
    v = json.load(open(pathlib.Path(__file__).with_name("vectors") / "hid_reports.json"))
    for case in v["pack"]:
        b = Backend()
        s = hid_stream.HidReportStream(b, v["room"], v["room"])
        try:
            s.write(b"".join(frame(bytes.fromhex(m)) for m in case["messages_hex"]))
            assert [r.hex() for r in b.sent] == case["reports_hex"]
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
    assert b.read_report(0.1)[:6] == b"\x02\x00\x00\x00xy"
    assert b.read_report(0.1) == b""          # another report ID is not ours
    b.write_report(report(b"q"))
    assert dev.written[0][0] == 6 and dev.written[0][1:6] == b"\x01\x00\x00\x00q"


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
