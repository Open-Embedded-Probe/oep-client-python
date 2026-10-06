"""USB identification and the probing rule (transports §3): no automatic identification but the project's VID:PID
(1209:4F45, registry usb), a named unit_id found by serial alone and checked by describe, and confirm-only probing that
closes a device giving no valid answer."""

import dataclasses
import struct
import time

import pytest

from oep_client import catalog, endpoint, fake, link, message as m, registry as reg


class LengthStream:
    """A vendor bulk / HID way in as a stream of length(u16) message frames, answered by a fake endpoint (or by
    `answer`, or not at all); every message the host wrote is kept in `sent`."""

    def __init__(self, ep=None, answer=None, index=1):
        self.ep, self.answer, self.index = ep, answer, index
        self.rx, self.buf, self.sent, self.timeout, self.closed = bytearray(), bytearray(), [], 0.05, False

    @property
    def in_waiting(self):
        return len(self.rx)

    def read(self, n=1):
        if not self.rx:
            time.sleep(min(self.timeout or 0, 0.01))
        out = bytes(self.rx[:n])
        del self.rx[:n]
        return out

    def write(self, data):
        self.buf += data
        while len(self.buf) >= 2:
            n = struct.unpack_from("<H", self.buf)[0]
            if len(self.buf) < 2 + n:
                break
            msg = bytes(self.buf[2:2 + n])
            del self.buf[:2 + n]
            if not n:
                continue
            self.sent.append(msg)
            if self.ep is not None:
                out = self.ep.handle(msg, self.index)
            else:
                out = self.answer(msg) if self.answer else None
            if out:
                self.rx += struct.pack("<H", len(out)) + out
        return len(data)

    def flush(self):
        pass

    def reset_input_buffer(self):
        self.rx.clear()

    def close(self):
        self.closed = True


def fake_ep(unit_id=None):
    """The fake p4-bench probe; `unit_id`: its describe says this unit_id instead."""
    probe = fake.p4_bench()
    if unit_id is not None:
        core = probe.offered[0]
        tag = reg.CORE.tlv["describe"]["unit_id"]
        probe.offered[0] = dataclasses.replace(
            core, tlvs=tuple(t for t in core.tlvs if t[0] & 0x7F != tag) + (catalog.text(tag, unit_id),))
    return endpoint.Endpoint(probe, lambda: int(time.monotonic() * 1000))


def ops(sent):
    return [msg[5] for msg in sent]


def test_only_the_project_vid_pid_identifies_a_probe():
    assert (reg.USB["project_vid"], reg.USB["project_pid"]) == (0x1209, 0x4F45)
    assert link.PROJECT_VID_PIDS == ((0x1209, 0x4F45),)
    assert (link.USB_VID, link.USB_PID) == (0x1209, 0x4F45)
    assert link.is_project_device(0x1209, 0x4F45)
    assert not link.is_project_device(0x303A, 0x0002)          # a board's default: never identified
    assert not link.is_project_device(0x1209, 0x0001)
    assert "iproduct_prefix" not in reg.USB and not hasattr(link, "temporary_clue")
    assert not any(n.startswith("TEMPORARY_") for n in dir(link))


def probes(*devices):
    """A fake usb_probes: each device is (unit_id, ways, ports)."""
    return lambda vid=0x1209, pid=0x4F45: [link.UsbProbe(u, list(w), list(p)) for u, w, p in devices]


class Opened(Exception):
    pass


def serial_link(path, timeout, baud):
    raise Opened(path)


def no_stream(kind, vid, pid, serial):
    raise FileNotFoundError(f"no USB device {vid:04x}:{pid:04x}")


def test_a_bare_usb_target_opens_the_project_vid_pid(monkeypatch):
    s, seen = LengthStream(fake_ep()), []

    def opened(kind, vid, pid, serial):
        seen.append((kind, vid, pid, serial))
        return s

    monkeypatch.setattr(link, "_open_usb_stream", opened)
    monkeypatch.setattr(link, "usb_probes", probes(("30eda0e343c6", ["vendor"], [])))
    link.open_host("usb")
    assert seen == [("vendor", 0x1209, 0x4F45, "30eda0e343c6")] and ops(s.sent)[0] == m.OP_CONFIRM


def test_a_bare_usb_target_with_one_probe_of_all_three_ways_opens_vendor(monkeypatch):
    s, seen = LengthStream(fake_ep()), []

    def opened(kind, vid, pid, serial):
        seen.append((kind, serial))
        return s

    monkeypatch.setattr(link, "_open_usb_stream", opened)
    monkeypatch.setattr(link, "usb_probes", probes(("30eda0e343c6", ["vendor", "hid", "cdc"], ["/dev/ttyACM0"])))
    link.open_host("usb")
    assert seen == [("vendor", "30eda0e343c6")]
    monkeypatch.setattr(link, "SerialLink", serial_link)
    monkeypatch.setattr(link, "_open_usb_stream", no_stream)  # vendor and HID not openable: its CDC port
    with pytest.raises(Opened, match="/dev/ttyACM0"):
        link.open_host("usb")


def test_a_bare_usb_target_with_one_cdc_only_probe_takes_its_port(monkeypatch):
    tried = []
    monkeypatch.setattr(link, "_open_usb_stream", lambda *a: tried.append(a))
    monkeypatch.setattr(link, "usb_probes", probes(("9489dd2ae0953650", ["cdc"], ["/dev/ttyACM7"])))
    monkeypatch.setattr(link, "SerialLink", serial_link)
    with pytest.raises(Opened, match="/dev/ttyACM7"):
        link.open_host("usb")
    assert not tried                                          # no vendor / HID tried on a CDC-only probe
    monkeypatch.setattr(link, "usb_probes", probes(("9489dd2ae0953650", ["cdc"], ["/dev/ttyACM7", "/dev/ttyACM8"])))
    with pytest.raises(ValueError, match="2 serial ports: name one"):
        link.open_host("usb")


def test_a_bare_usb_target_with_no_probe_says_none(monkeypatch):
    monkeypatch.setattr(link, "_open_usb_stream", no_stream)
    monkeypatch.setattr(link, "usb_probes", probes())
    with pytest.raises(FileNotFoundError, match="no way in to 1209:4f45.*cdc: no serial port on 1209:4f45"):
        link.open_host("usb")


def test_a_named_vid_pid_has_no_cdc_fallback_and_no_listing(monkeypatch):
    monkeypatch.setattr(link, "_open_usb_stream", no_stream)
    monkeypatch.setattr(link, "usb_probes", lambda *a: pytest.fail("listed"))
    with pytest.raises(FileNotFoundError):
        link.open_host("usb:1209:4f45")


@pytest.mark.parametrize("devices", [
    [("30eda0e343c6", ["vendor", "hid", "cdc"], ["/dev/ttyACM0"]), ("9489dd2ae0953650", ["cdc"], ["/dev/ttyACM1"])],
    [("9489dd2ae0953650", ["cdc"], ["/dev/ttyACM1"]), ("30eda0e343c6", ["vendor"], [])],
    [("a1", ["vendor"], []), ("a2", ["hid"], []), ("a3", ["cdc"], ["/dev/ttyACM3"]),
     ("a4", ["vendor", "hid"], [])],
])
def test_a_bare_usb_target_with_several_probes_opens_none_and_lists_them(monkeypatch, devices):
    tried = []
    monkeypatch.setattr(link, "_open_usb_stream", lambda *a: tried.append(a))
    monkeypatch.setattr(link, "SerialLink", serial_link)
    monkeypatch.setattr(link, "usb_probes", probes(*devices))
    with pytest.raises(link.SeveralProbes) as e:
        link.open_host("usb")
    assert not tried and len(e.value.probes) == len(devices)
    text = str(e.value)
    assert text.startswith(f"{len(devices)} probes on 1209:4f45 (") and "name one with usb:<unit_id> or its serial port" in text
    for unit_id, ways, ports in devices:
        assert f"{unit_id}: " in text
    if devices[0][0] == "30eda0e343c6":
        assert "(30eda0e343c6: vendor, hid, cdc /dev/ttyACM0; 9489dd2ae0953650: cdc /dev/ttyACM1)" in text


def test_the_oep_command_lists_several_usb_probes_in_one_line(monkeypatch):
    from oep_client import __main__ as cli
    monkeypatch.setattr(link, "_open_usb_stream", lambda *a: pytest.fail("opened"))
    monkeypatch.setattr(link, "usb_probes", probes(("30eda0e343c6", ["vendor", "hid", "cdc"], ["/dev/ttyACM0"]),
                                                   ("9489dd2ae0953650", ["cdc"], ["/dev/ttyACM1"])))
    with pytest.raises(SystemExit) as e:
        cli.main(["dump", "--port", "usb"])
    assert str(e.value.code) == ("oep: cannot open usb: 2 probes on 1209:4f45 (30eda0e343c6: vendor, hid, cdc "
                                 "/dev/ttyACM0; 9489dd2ae0953650: cdc /dev/ttyACM1); not choosing one: name one with "
                                 "usb:<unit_id> or its serial port.")


def test_usb_probes_counts_each_device_once_whatever_its_ways_in():
    usb = [{"serial": "30EDA0E343C6", "location": "1-2", "ways": {"vendor", "hid", "cdc"}},
           {"serial": None, "location": "1-3", "ways": {"cdc"}},
           {"serial": None, "location": "1-4", "ways": {"vendor"}}]
    hid = [{"serial": "30eda0e343c6", "location": None, "ways": {"hid"}},
           {"serial": None, "location": None, "ways": {"hid"}}]          # no serial, libusb listed: left out
    ports = [("/dev/ttyACM0", "30eda0e343c6", "1-2"), ("/dev/ttyACM1", "9489dd2ae0953650", "1-3"),
             ("/dev/ttyACM2", "abc", "1-5")]
    got = link._merge_probes(usb, hid, ports)
    assert [(p.unit_id, p.ways, p.ports) for p in got] == [
        ("30EDA0E343C6", ["vendor", "hid", "cdc"], ["/dev/ttyACM0"]),
        ("9489dd2ae0953650", ["cdc"], ["/dev/ttyACM1"]),               # merged by location
        (None, ["vendor"], []),
        ("abc", ["cdc"], ["/dev/ttyACM2"])]
    assert str(got[2]) == "no USB serial (at 1-4): vendor"
    assert link._merge_probes([], [], []) == []
    one = link._merge_probes([], [{"serial": None, "location": None, "ways": {"hid"}}], [])
    assert [(p.unit_id, p.ways) for p in one] == [(None, ["hid"])]


@pytest.mark.parametrize("command", [["dump", "--port", "usb"], ["config", "show", "usb"], ["linktest", "usb"],
                                     ["pins", "usb"], ["speed", "usb"]])
def test_the_oep_command_says_in_one_line_that_no_usb_probe_is_there(monkeypatch, capsys, command):
    """No device on the project's VID:PID and no hid module: one line, no traceback, with the hint (extras, or name
    the port)."""
    from oep_client import __main__ as cli

    def none(kind, vid, pid, serial):
        if kind == "hid":
            raise ImportError("No module named 'hid'")
        raise FileNotFoundError(f"no USB device {vid:04x}:{pid:04x}")

    monkeypatch.setattr(link, "_open_usb_stream", none)
    monkeypatch.setattr(link, "usb_probes", probes())
    with pytest.raises(SystemExit) as e:
        cli.main(command)
    text = str(e.value.code)
    assert "\n" not in text and text.startswith("oep: cannot open usb: no way in to 1209:4f45")
    assert "No module named 'hid'" in text and "Hint: install the usb / hid extras" in text and "name the probe's port" in text


def test_the_oep_command_says_in_one_line_that_a_port_is_not_there(capsys):
    from oep_client import __main__ as cli
    with pytest.raises(SystemExit) as e:
        cli.main(["dump", "--port", "/dev/oep-no-such-port"])
    text = str(e.value.code)
    assert "\n" not in text and text.startswith("oep: cannot open /dev/oep-no-such-port:") and "Hint" not in text


def test_project_serial_ports_lists_the_project_vid_pid_only(monkeypatch):
    import serial.tools.list_ports as lp

    P = dataclasses.make_dataclass("P", ["device", "vid", "pid", "serial_number"])
    ports = [P("/dev/ttyACM0", 0x1209, 0x4F45, "abc"), P("/dev/ttyUSB0", 0x1A86, 0x7523, None),
             P("/dev/ttyS0", None, None, None), P("/dev/ttyACM1", 0x303A, 0x0002, "def")]
    monkeypatch.setattr(lp, "comports", lambda: ports)
    assert link.project_serial_ports() == [("/dev/ttyACM0", "abc")]


def test_open_usb_host_probes_with_confirm_first(monkeypatch):
    s = LengthStream(fake_ep())
    monkeypatch.setattr(link, "_open_usb_stream", lambda kind, vid, pid, serial: s)
    hst = link.open_usb_host()
    assert ops(s.sent) == [m.OP_CONFIRM] and hst.limits["max_frame"] >= 64 and not s.closed


def test_a_silent_device_gets_one_confirm_and_its_resend_then_is_closed(monkeypatch):
    streams = []

    def opened(kind, vid, pid, serial):
        streams.append(LengthStream())
        return streams[-1]

    monkeypatch.setattr(link, "_open_usb_stream", opened)
    monkeypatch.setattr(link, "PROBE_WAIT_S", 0.1)
    with pytest.raises(link.NotOepProbe):
        link.open_usb_host(timeout=5.0)
    assert len(streams) == 2                                  # vendor, then HID: each probed alone
    for s in streams:
        assert ops(s.sent) == [m.OP_CONFIRM, m.OP_CONFIRM] and s.sent[0] == s.sent[1] and s.closed


def test_an_answer_that_is_not_a_valid_confirm_closes_the_device(monkeypatch):
    def not_oep(msg):                                         # completed, the same corr, but no OEP! payload
        return m.Result(m.Request.unpack(msg).corr, m.COMPLETED, m.SUCCESS, b"HELLO" + bytes(12)).pack()

    s = LengthStream(answer=not_oep)
    monkeypatch.setattr(link, "_open_usb_stream", lambda kind, vid, pid, serial: s)
    with pytest.raises(link.NotOepProbe):
        link.open_usb_host(transports=("vendor",))
    assert ops(s.sent) == [m.OP_CONFIRM] and s.closed


def test_usb_unit_id_matches_the_serial_and_checks_describe(monkeypatch):
    s = LengthStream(fake_ep())
    monkeypatch.setattr(link, "find_usb", lambda unit_id: (0x1234, 0x5678))
    seen = []

    def opened(kind, vid, pid, serial):
        seen.append((vid, pid, serial))
        return s

    monkeypatch.setattr(link, "_open_usb_stream", opened)
    hst = link.open_host("usb:30eda0e3b001")
    assert seen == [(0x1234, 0x5678, "30eda0e3b001")]
    assert ops(s.sent)[0] == m.OP_CONFIRM and set(ops(s.sent)[1:]) == {m.OP_DESCRIBE} and not s.closed
    assert hst.limits is not None


def test_usb_unit_id_whose_describe_says_another_unit_is_closed(monkeypatch):
    s = LengthStream(fake_ep(unit_id="ffffffffffff"))
    monkeypatch.setattr(link, "find_usb", lambda unit_id: (0x1234, 0x5678))
    monkeypatch.setattr(link, "_open_usb_stream", lambda kind, vid, pid, serial: s)
    with pytest.raises(link.UnitIdMismatch):
        link.open_host("usb:30eda0e3b001")
    assert s.closed and set(ops(s.sent)) <= {m.OP_CONFIRM, m.OP_DESCRIBE}


def test_find_usb_matches_the_serial_alone(monkeypatch):
    import sys
    import types

    class Dev:
        def __init__(self, vid, pid, serial, product):
            self.v, self.p, self.serial, self.product = vid, pid, serial, product

        def getVendorID(self):
            return self.v

        def getProductID(self):
            return self.p

        def open(self):
            dev = self

            class H:
                def getSerialNumber(self):
                    return dev.serial

                def getProduct(self):
                    return dev.product

                def close(self):
                    pass
            return H()

    devices = [Dev(1, 2, "other", "OEP probe"), Dev(0x1209, 0x0001, "ABC123", "Some other name")]

    class Ctx:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def getDeviceIterator(self, skip_on_error=True):
            return iter(devices)

    monkeypatch.setitem(sys.modules, "usb1", types.SimpleNamespace(USBContext=Ctx))
    assert link.find_usb("abc123") == (0x1209, 0x0001)        # no iProduct check
    with pytest.raises(FileNotFoundError):
        link.find_usb("nobody")


def test_a_silent_serial_port_gets_only_confirms_then_is_closed(monkeypatch):
    from oep_client import cobs

    class Silent:
        def __init__(self):
            self.tx, self.timeout, self.closed, self.baudrate, self.name = bytearray(), 0.05, False, 115200, "/dev/fake"

        in_waiting = 0

        def read(self, n=1):
            time.sleep(0.005)
            return b""

        def write(self, data):
            self.tx += data
            return len(data)

        def reset_input_buffer(self):
            pass

        def fileno(self):
            raise OSError("no fd")

        def close(self):
            self.closed = True

    monkeypatch.setattr(link, "OPEN_RETRY_S", 0.2)
    s = Silent()
    lk = link.SerialLink.on_stream(s, "cobs", 0.1)
    lk.transport = "serial"
    from oep_client import host
    with pytest.raises(link.NotOepProbe):
        lk.attach_host(host.Host(lk.send))
    sent = [m.Request.unpack(cobs.unframe(f)) for f in bytes(s.tx).split(b"\x00") if f]
    assert sent and all(r.op == m.OP_CONFIRM for r in sent) and s.closed
