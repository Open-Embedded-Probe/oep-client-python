"""Independent TCP length-frame inspector; no runtime client framing or retries."""
import socket
import time
from urllib.parse import urlsplit

from .conformance_serial import WireError


def address(value):
    parsed = urlsplit(value)
    if parsed.scheme != 'tcp' or not parsed.hostname or parsed.port is None:
        raise ValueError('TCP conformance requires explicit tcp://HOST:PORT')
    if parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
        raise ValueError('TCP conformance address must contain only host and port')
    return parsed.hostname, parsed.port


def frame(message):
    return len(message).to_bytes(2, 'little') + message


class TcpWire:
    def __init__(self, stream, timeout=3.0, settle=0.03):
        self.stream, self.timeout, self.settle = stream, timeout, settle
        self.max_frame, self.last_exchange = 64, {}

    @classmethod
    def open(cls, value):
        stream = socket.create_connection(address(value), timeout=3)
        stream.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return cls(stream)

    def close(self):
        self.stream.close()

    def send(self, message):
        replies = self.exchange([frame(message)], 1)
        if len(replies) != 1:
            raise WireError(f'expected one result, observed {len(replies)}')
        return replies[0]

    def exchange(self, chunks, count, pause=0, expect_close=False, silence=0):
        record = self.last_exchange = {'writes': [], 'reads': [], 'settle_ms': self.settle * 1000,
                                       'silence_ms': silence * 1000,
                                       'started_monotonic_ns': time.monotonic_ns()}
        pending, replies = bytearray(), []
        try:
            for index, chunk in enumerate(chunks):
                record['writes'].append({'hex': chunk.hex(), 'monotonic_ns': time.monotonic_ns()})
                self.stream.sendall(chunk)
                if pause and index + 1 < len(chunks):
                    time.sleep(pause)
            deadline = time.monotonic() + max(self.timeout, silence + self.settle)
            quiet = time.monotonic() + silence if count == 0 and not expect_close else None
            while time.monotonic() < deadline:
                self.stream.settimeout(min(0.01, max(0, deadline - time.monotonic())))
                try:
                    data = self.stream.recv(4096)
                except socket.timeout:
                    data = None
                except ConnectionResetError:
                    data = b''
                    record['reset'] = True
                if data == b'':
                    record['closed'] = True
                    if not expect_close or pending or replies:
                        raise WireError('unexpected TCP close, partial frame or response before close')
                    return replies
                if data:
                    record['reads'].append({'hex': data.hex(), 'monotonic_ns': time.monotonic_ns()})
                    pending.extend(data)
                    while len(pending) >= 2:
                        size = int.from_bytes(pending[:2], 'little')
                        if size > self.max_frame:
                            raise WireError('probe emitted oversized TCP message')
                        if len(pending) < size + 2:
                            break
                        if size:
                            replies.append(bytes(pending[2:2 + size]))
                        del pending[:2 + size]
                    if len(replies) >= count and not expect_close:
                        quiet = max(quiet or 0, time.monotonic() + self.settle)
                elif not expect_close and quiet is not None and time.monotonic() >= quiet:
                    if pending:
                        raise WireError('partial trailing TCP frame')
                    return replies
            raise TimeoutError('TCP close not observed' if expect_close else
                               f'TCP replies: expected {count}, observed {len(replies)}')
        except Exception as exc:
            record['error'] = f'{type(exc).__name__}: {exc}'
            raise
        finally:
            record['ended_monotonic_ns'] = time.monotonic_ns()
