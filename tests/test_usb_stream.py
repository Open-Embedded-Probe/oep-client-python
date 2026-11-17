"""UsbAsyncStream closes cleanly: the IN transfers are cancelled and taken back by the event thread before the handle and
the context go (closing with transfers out hung the process at exit or aborted libusb), and close is hooked to exit."""

import atexit
import sys
import threading
import time
import types

import pytest


class FakeUsb1(types.ModuleType):
    TRANSFER_COMPLETED, TRANSFER_TIMED_OUT, TRANSFER_CANCELLED = 0, 2, 3

    class USBError(Exception):
        pass

    class USBErrorInterrupted(USBError):
        pass


usb1 = FakeUsb1("usb1")


class Transfer:
    def __init__(self, ctx):
        self.ctx, self.status, self.out = ctx, None, False

    def setBulk(self, ep, size, callback, timeout):
        self.callback = callback

    def submit(self):
        self.out = True

    def cancel(self):
        self.ctx.pending.append(self)                  # comes back as cancelled on a later event pass, like libusb

    def getStatus(self):
        return self.status


class Context:
    def __init__(self):
        self.pending, self.lock, self.closed = [], threading.Lock(), False

    def handleEventsTimeout(self, t):
        import time
        time.sleep(0.005)
        while self.pending:
            tr = self.pending.pop(0)
            tr.out, tr.status = False, usb1.TRANSFER_CANCELLED
            tr.callback(tr)

    def close(self):
        self.closed = True


class Handle:
    def __init__(self, ctx):
        self.ctx, self.transfers, self.closed_with_out = ctx, [], None

    def getTransfer(self):
        t = Transfer(self.ctx)
        self.transfers.append(t)
        return t

    def releaseInterface(self, n):
        pass

    def close(self):
        self.closed_with_out = sum(t.out for t in self.transfers)


@pytest.fixture
def stream(monkeypatch):
    monkeypatch.setitem(sys.modules, "usb1", usb1)
    from oep_client import usb_stream
    ctx = Context()
    h = Handle(ctx)
    s = usb_stream.UsbAsyncStream(ctx, h, 0x81, 0x01, 0)
    return s, ctx, h


def test_close_takes_every_transfer_back_before_the_handle_goes(stream):
    s, ctx, h = stream
    s.close()
    assert h.closed_with_out == 0 and ctx.closed and not s._events.is_alive()
    s.close()                                          # twice (atexit after an explicit close): nothing more


def test_close_is_hooked_to_exit(stream, monkeypatch):
    s, ctx, h = stream
    calls = []
    monkeypatch.setattr(atexit, "unregister", lambda f: calls.append(f))
    s.close()
    assert calls == [s.close]                          # it was registered, and an explicit close takes it off


def test_a_completed_transfer_goes_out_again_and_is_counted(stream):
    """A transfer back with data: copied out and submitted again; stats() counts it, the fewest out at a callback and
    the bytes waiting."""
    s, ctx, h = stream
    t = h.transfers[0]
    t.status, t.out = usb1.TRANSFER_COMPLETED, False
    t.getActualLength = lambda: 3
    t.getBuffer = lambda: bytearray(b"abcdef")
    t.callback(t)
    st = s.stats()
    assert t.out and st["callbacks"] == 1 and st["inflight_min"] == s.depth - 1 and st["bytes"] == 3
    assert st["buffered_max"] == 3 and s.read(10) == b"abc"
    s.close()
    assert h.closed_with_out == 0


def test_the_transfers_size_and_depth_are_the_callers(monkeypatch):
    monkeypatch.setitem(sys.modules, "usb1", usb1)
    from oep_client import usb_stream
    ctx = Context()
    h = Handle(ctx)
    s = usb_stream.UsbAsyncStream(ctx, h, 0x81, 0x01, 0, urb_size=256 * 1024, depth=32)
    assert len(h.transfers) == 32 and s.stats()["urb_size"] == 256 * 1024
    s.close()


class FakeProcess:
    def __init__(self, thread):
        self.thread = thread

    def join(self, timeout=None):
        self.thread.join(timeout)

    def is_alive(self):
        return self.thread.is_alive()

    def kill(self):
        pass


def test_the_process_stream_reads_what_its_process_sends_and_writes_through_it():
    """UsbProcessStream: the data the process sends is read as one stream; a write is the process's (its answer is
    write's); stats are the process's; close ends it."""
    import multiprocessing as mp
    from oep_client import usb_stream
    data_parent, data_child = mp.Pipe(duplex=False)
    cmd_parent, cmd_child = mp.Pipe()
    written = []

    def child():
        data_child.send_bytes(b"abc")
        data_child.send_bytes(b"def")
        while True:
            kind, arg = cmd_child.recv()
            if kind == "close":
                data_child.close()
                return
            if kind == "write":
                written.append(arg)
                cmd_child.send(("ok", len(arg)))
            elif kind == "stats":
                cmd_child.send(("ok", {"callbacks": 7}))

    t = threading.Thread(target=child, daemon=True)
    t.start()
    s = usb_stream.UsbProcessStream(FakeProcess(t), data_parent, cmd_parent, 512)
    s.timeout = 1.0
    got = b""
    while len(got) < 6:
        got += s.read(10)
    assert got == b"abcdef" and s.write(b"xy") == 2 and written == [b"xy"] and s.stats() == {"callbacks": 7}
    s.close()
    assert not t.is_alive()
    with pytest.raises(OSError):
        s.write(b"z")
    s.close()


def test_the_process_side_sends_what_came_in_and_answers_requests(monkeypatch):
    """_child (the process's body) on a fake UsbAsyncStream: ready with the OUT packet size, the IN bytes sent in
    order however they came, a write answered with its count, an error as ("err", ...), close closes the device."""
    import multiprocessing as mp
    from oep_client import usb_stream

    class Fake:
        out_packet = 64

        def __init__(self):
            self._cond, self._buffer, self.closed = threading.Condition(), bytearray(), False

        def write(self, data):
            if data == b"bad":
                raise ValueError("no")
            return len(data)

        def stats(self):
            return {"callbacks": 1}

        def take_locked(self, n):
            out = bytes(self._buffer[:n])
            del self._buffer[:n]
            return out

        def release_held(self):
            pass

        def close(self):
            self.closed = True

    fake = Fake()
    monkeypatch.setattr(usb_stream.UsbAsyncStream, "open", classmethod(lambda cls, *a, **k: fake))
    data_parent, data_child = mp.Pipe(duplex=False)
    cmd_parent, cmd_child = mp.Pipe()
    t = threading.Thread(target=usb_stream._child, args=(1, 2, None, None, None, data_child, cmd_child), daemon=True)
    t.start()
    assert cmd_parent.recv() == ("ready", 64)
    for part in (b"ab", b"cd", b"e"):
        with fake._cond:
            fake._buffer += part
            fake._cond.notify_all()
    got = b""
    while len(got) < 5:
        got += data_parent.recv_bytes()
    assert got == b"abcde"
    cmd_parent.send(("write", b"xyz"))
    assert cmd_parent.recv() == ("ok", 3)
    cmd_parent.send(("write", b"bad"))
    assert cmd_parent.recv()[0] == "err"
    cmd_parent.send(("stats", None))
    assert cmd_parent.recv() == ("ok", {"callbacks": 1})
    cmd_parent.send(("close", None))
    t.join(2.0)
    assert not t.is_alive() and fake.closed


def test_a_submit_usbfs_refuses_leaves_the_depth_at_what_it_took(monkeypatch):
    """usbfs' memory (usbfs_memory_mb) refusing the 11th transfer: the stream runs on 10; refusing the first: raised."""
    monkeypatch.setitem(sys.modules, "usb1", usb1)
    from oep_client import usb_stream

    for room, ok in ((10, True), (0, False)):
        ctx = Context()
        h = Handle(ctx)
        taken = []

        def submit(self, taken=taken, room=room):
            if len(taken) >= room:
                raise usb1.USBError("no memory")
            taken.append(self)
            self.out = True

        monkeypatch.setattr(Transfer, "submit", submit)
        if ok:
            s = usb_stream.UsbAsyncStream(ctx, h, 0x81, 0x01, 0)
            assert s.depth == 10 and s.stats()["depth"] == 10
            s.close()
        else:
            with pytest.raises(usb1.USBError):
                usb_stream.UsbAsyncStream(ctx, h, 0x81, 0x01, 0)


def test_the_usb_stream_is_a_process_by_default_and_a_thread_when_asked(monkeypatch):
    """_open_usb_stream (POSIX): UsbProcessStream by default; OEP_USB_STREAM=thread, or a process that does not run
    (ChildProcessError), UsbAsyncStream. A device that is not there is not retried on the thread."""
    import os
    from oep_client import link, usb_stream
    monkeypatch.setitem(sys.modules, "usb1", usb1)
    monkeypatch.setattr(os, "name", "posix")
    opened = []
    monkeypatch.setattr(usb_stream.UsbAsyncStream, "open", classmethod(lambda cls, *a, **k: opened.append("thread") or "t"))
    process = {"raise": None}

    def proc_open(cls, *a, **k):
        opened.append("process")
        if process["raise"]:
            raise process["raise"]
        return "p"

    monkeypatch.setattr(usb_stream.UsbProcessStream, "open", classmethod(proc_open))
    monkeypatch.delenv("OEP_USB_STREAM", raising=False)
    assert link._open_usb_stream("vendor", 1, 2, None) == "p" and opened == ["process"]
    monkeypatch.setenv("OEP_USB_STREAM", "thread")
    opened.clear()
    assert link._open_usb_stream("vendor", 1, 2, None) == "t" and opened == ["thread"]
    monkeypatch.delenv("OEP_USB_STREAM")
    opened.clear()
    process["raise"] = ChildProcessError("no")
    with pytest.warns(RuntimeWarning):
        assert link._open_usb_stream("vendor", 1, 2, None) == "t"
    assert opened == ["process", "thread"]
    opened.clear()
    process["raise"] = FileNotFoundError("no USB device")
    with pytest.raises(FileNotFoundError):
        link._open_usb_stream("vendor", 1, 2, None)
    assert opened == ["process"]



def test_a_full_backlog_holds_the_transfers_until_read_takes_it_under_half(monkeypatch):
    """Past backlog_max a transfer back with data is held, not sent out again (the kernel then takes no more and the
    probe drops whole frames, core §10.4); read() taking the backlog under half sends the held ones out again. Close
    with some held still ends with nothing out."""
    monkeypatch.setitem(sys.modules, "usb1", usb1)
    from oep_client import usb_stream
    ctx = Context()
    h = Handle(ctx)
    s = usb_stream.UsbAsyncStream(ctx, h, 0x81, 0x01, 0, depth=4, backlog_max=8)

    def back(t, data):
        t.status, t.out = usb1.TRANSFER_COMPLETED, False
        t.getActualLength = lambda: len(data)
        t.getBuffer = lambda: bytearray(data)
        t.callback(t)
    a, b, c, d = h.transfers
    back(a, b"12345")                                  # 5 waiting: out again
    back(b, b"6789")                                   # 9 > 8: held
    back(c, b"x")                                      # still full: held
    assert a.out and not b.out and not c.out and s.stats()["holds"] == 2 and s.stats()["held_max"] == 2
    s.timeout = 0.1
    assert s.read(4) == b"1234"                         # 6 waiting: not under half yet
    assert not b.out and not c.out
    assert s.read(4) == b"5678"                         # 2 waiting: out again
    assert b.out and c.out and s._held == []
    back(b, b"y")
    back(c, b"z")                                       # 4 waiting
    back(d, b"123456")                                  # 10: held
    assert not d.out
    s.close()
    assert h.closed_with_out == 0 and not s._events.is_alive()


def test_the_process_stream_stops_taking_the_pipe_while_its_buffer_is_full(monkeypatch):
    """UsbProcessStream holds BUFFER_MAX at most: past it the pipe waits (the process then holds the bytes, then its
    transfers), and read() taking some lets it go on."""
    import multiprocessing as mp
    from oep_client import usb_stream
    data_parent, data_child = mp.Pipe(duplex=False)
    cmd_parent, cmd_child = mp.Pipe()

    def child():
        for _ in range(4):
            data_child.send_bytes(b"x" * 10)
        while True:
            kind, _ = cmd_child.recv()
            if kind == "close":
                data_child.close()
                return
    monkeypatch.setattr(usb_stream.UsbProcessStream, "BUFFER_MAX", 15)   # before its reader starts
    t = threading.Thread(target=child, daemon=True)
    t.start()
    s = usb_stream.UsbProcessStream(FakeProcess(t), data_parent, cmd_parent, 512)
    time.sleep(0.3)
    assert s.in_waiting == 20                           # two taken (10, then 20 > 15): the rest waits in the pipe
    s.timeout = 1.0
    assert s.read(15) == b"x" * 15
    deadline = time.monotonic() + 2
    while s.in_waiting < 25 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert s.in_waiting == 25                           # 5 left, under 15: two more taken
    s.close()
