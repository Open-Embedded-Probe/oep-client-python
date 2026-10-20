"""UsbAsyncStream closes cleanly: the IN transfers are cancelled and taken back by the event thread before the handle and
the context go (closing with transfers out hung the process at exit or aborted libusb), and close is hooked to exit."""

import atexit
import sys
import threading
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
