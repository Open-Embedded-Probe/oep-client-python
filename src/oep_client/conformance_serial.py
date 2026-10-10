"""Independent serial wire inspector: OS port opening only is shared with the client."""
import binascii
import time


class WireError(ValueError):
    pass


def encode(data):
    out = bytearray(b'\0')
    mark, count = 0, 1
    for byte in data:
        if byte == 0:
            out[mark] = count
            mark, count = len(out), 1
            out.append(0)
        else:
            out.append(byte)
            count += 1
            if count == 255:
                out[mark] = count
                mark, count = len(out), 1
                out.append(0)
    if count == 1 and mark == len(out) - 1 and data and data[-1] != 0:
        out.pop()
    else:
        out[mark] = count
    return bytes(out)


def decode(data):
    out, at = bytearray(), 0
    while at < len(data):
        count = data[at]
        if not count or at + count > len(data):
            raise WireError('COBS block length')
        out.extend(data[at + 1:at + count])
        at += count
        if count != 255 and at < len(data):
            out.append(0)
    return bytes(out)


def frame(message, corrupt_crc=False):
    checksum = binascii.crc_hqx(message, 0xffff) ^ int(corrupt_crc)
    return b'\0' + encode(message + checksum.to_bytes(2, 'little')) + b'\0'


def unframe(candidate):
    data = decode(candidate)
    if len(data) < 3 or binascii.crc_hqx(data[:-2], 0xffff) != int.from_bytes(data[-2:], 'little'):
        raise WireError('serial CRC/length')
    return data[:-2]


class SerialWire:
    def __init__(self, stream, timeout=3.0, settle=0.03):
        self.stream = stream
        self.timeout, self.settle = timeout, settle
        self.max_frame = 64
        self.last_exchange = {}

    def send(self, message):
        replies = self.exchange([frame(message)], 1)
        if len(replies) != 1:
            raise WireError(f'expected one result, observed {len(replies)}')
        return replies[0]

    def exchange(self, chunks, count, pause=0, silence=0):
        record = self.last_exchange = {'writes': [], 'reads': [], 'settle_ms': self.settle * 1000}
        replies, candidate, started = [], bytearray(), False
        try:
            for chunk in chunks:
                now = time.monotonic_ns()
                record['writes'].append({'hex': chunk.hex(), 'monotonic_ns': now})
                if self.stream.write(chunk) != len(chunk):
                    raise OSError('short serial write')
                if pause:
                    time.sleep(pause)
            flush = getattr(self.stream, 'flush', None)
            if flush:
                flush()
            # Before transport kind is known, use the conservative UART wait floor (core §4.4).
            baud = getattr(self.stream, 'baudrate', 115200)
            transfer = (sum(map(len, chunks)) + self.max_frame * 3) * 10 / baud
            deadline = time.monotonic() + max(self.timeout, 1 + transfer, silence + self.settle)
            quiet = time.monotonic() + silence if count == 0 else None
            while time.monotonic() < deadline:
                self.stream.timeout = min(0.01, max(0, deadline - time.monotonic()))
                data = self.stream.read(max(1, getattr(self.stream, 'in_waiting', 0)))
                if data:
                    record['reads'].append({'hex': data.hex(), 'monotonic_ns': time.monotonic_ns()})
                    for byte in data:
                        if byte == 0:
                            if started and candidate:
                                message = unframe(bytes(candidate))
                                if len(message) > self.max_frame:
                                    raise WireError('probe emitted oversized message')
                                replies.append(message)
                            candidate.clear()
                            started = True
                        elif started:
                            candidate.append(byte)
                            if len(candidate) > 2 * self.max_frame + 32:
                                raise WireError('probe emitted unbounded serial candidate')
                    if len(replies) >= count:
                        quiet = max(quiet or 0, time.monotonic() + self.settle)
                elif quiet is not None and time.monotonic() >= quiet:
                    if candidate:
                        raise WireError('partial trailing response frame')
                    return replies
            raise TimeoutError(f'serial replies: expected {count}, observed {len(replies)}')
        except Exception as exc:
            record['error'] = f'{type(exc).__name__}: {exc}'
            raise
