"""pyserial's handler for oep://<probe>/<port>: an RP2040 peer's console over the probe's SWD (SEGGER RTT through the
MEM-AP, oep_client.rp2040_peer.Console). Baud rate, parity and the control lines mean nothing here and are taken as
given. A console that stops (the session lost, the target gone) raises on the next read."""

from __future__ import annotations

import time

from serial.serialutil import PortNotOpenError, SerialBase, SerialException

from ..rp2040_peer import Console


class Serial(SerialBase):
    def open(self):
        if self._port is None:
            raise SerialException("port must be configured before it can be used")
        if self.is_open:
            raise SerialException("port already open")
        try:
            self._console = Console(self.portstr)
        except Exception as e:
            raise SerialException(f"{self.portstr}: {e}") from e
        self.is_open = True

    def close(self):
        if self.is_open:
            self._console.close()
            self.is_open = False

    def from_url(self, url):
        return url

    def _reconfigure_port(self):
        pass

    @property
    def in_waiting(self):
        if not self.is_open:
            raise PortNotOpenError()
        return self._console.in_waiting()

    def read(self, size=1):
        if not self.is_open:
            raise PortNotOpenError()
        deadline = None if self._timeout is None else time.monotonic() + self._timeout
        out = bytearray()
        while len(out) < size:
            if self._console.error:
                raise SerialException(f"{self.portstr}: {self._console.error}")
            out += self._console.read(size - len(out))
            if len(out) >= size or (deadline is not None and time.monotonic() >= deadline):
                break
            time.sleep(0.005)
        return bytes(out)

    def write(self, data):
        if not self.is_open:
            raise PortNotOpenError()
        return self._console.write(bytes(data))

    def reset_input_buffer(self):
        if self.is_open:
            self._console.read()

    def reset_output_buffer(self):
        pass

    def _update_break_state(self):
        pass

    def _update_rts_state(self):
        pass

    def _update_dtr_state(self):
        pass

    @property
    def cts(self):
        return True

    @property
    def dsr(self):
        return True

    @property
    def ri(self):
        return False

    @property
    def cd(self):
        return True
