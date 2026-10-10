"""Inspect raw USB transfers/reports, without the runtime USB/HID stream parsers.

Backend: read(timeout) returns a complete transfer/report or None on timeout;
write(bytes, method='out'|'set-report') returns the byte count, and close().
HID sizes include count and an optional report ID. This checks probe output,
so invalid counts, IDs and nonzero sender padding are violations, not filtered.
"""
import time

from .conformance_serial import WireError


def frame(message):
    return len(message).to_bytes(2, 'little') + message


class UsbWire:
    def __init__(self, backend, kind, *, input_size=None, output_size=None, report_id=0,
                 out_packet_size=None, timeout=3, settle=0.03, clock=time.monotonic, sleep=time.sleep):
        if kind not in ('bulk', 'hid'):
            raise ValueError('raw USB kind must be bulk or hid')
        if not 0 <= report_id <= 255:
            raise ValueError('report ID must be u8')
        header = 2 + bool(report_id)
        if kind == 'hid' and (input_size is None or output_size is None or
                              not header < input_size <= 65535 or not header < output_size <= 65535):
            raise ValueError('explicit HID report sizes must leave room after count/ID')
        if kind == 'bulk' and (out_packet_size is None or out_packet_size < 1):
            raise ValueError('explicit bulk OUT packet size required')
        self.backend, self.kind = backend, kind
        self.input_size, self.output_size, self.report_id = input_size, output_size, report_id
        self.out_packet_size, self.timeout, self.settle = out_packet_size, timeout, settle
        self.clock, self.sleep = clock, sleep
        self.max_frame, self.last_exchange = 64, {}

    def close(self):
        self.backend.close()

    def pack(self, data, *, padding=0, report_id=None):
        room = self.output_size - 2 - bool(self.report_id)
        if len(data) > room:
            raise ValueError('stimulus exceeds HID report capacity')
        identity = self.report_id if report_id is None else report_id
        prefix = bytes((identity,)) if self.report_id else b''
        return prefix + len(data).to_bytes(2, 'little') + data + bytes((padding,)) * (room - len(data))

    def reports(self, data):
        room = self.output_size - 2 - bool(self.report_id)
        return [self.pack(data[i:i + room]) for i in range(0, len(data), room)] or [self.pack(b'')]

    def send(self, message):
        replies = self.exchange([frame(message)], 1)
        if len(replies) != 1:
            raise WireError(f'expected one result, observed {len(replies)}')
        return replies[0]

    def exchange(self, chunks, count, *, raw=False, method='out', pause=0, silence=0):
        record = self.last_exchange = {'kind': self.kind, 'writes': [], 'reads': [],
            'settle_ms': self.settle * 1000, 'silence_ms': silence * 1000,
            'started_monotonic_ns': int(self.clock() * 1e9)}
        pending, replies = bytearray(), []
        try:
            for index, chunk in enumerate(chunks):
                transfers = self.reports(chunk) if self.kind == 'hid' and not raw else [chunk]
                for transfer in transfers:
                    self.write(transfer, method, record)
                    if self.kind == 'bulk' and transfer and len(transfer) % self.out_packet_size == 0:
                        self.write(b'', method, record)  # required host ZLP, not a length-zero message
                if pause and index + 1 < len(chunks):
                    self.sleep(pause)
            deadline = self.clock() + max(self.timeout, silence + self.settle)
            quiet = self.clock() + silence if count == 0 else None
            while self.clock() < deadline:
                transfer = self.backend.read(min(0.01, max(0, deadline - self.clock())))
                if transfer is not None:
                    record['reads'].append({'hex': transfer.hex(), 'monotonic_ns': int(self.clock() * 1e9)})
                    data = bytes(transfer)
                    if self.kind == 'hid':
                        if len(data) != self.input_size:
                            raise WireError('probe emitted HID report with wrong size')
                        at = int(bool(self.report_id))
                        if at and data[0] != self.report_id:
                            raise WireError('probe emitted undeclared HID report ID')
                        size = int.from_bytes(data[at:at + 2], 'little')
                        if size > len(data) - at - 2:
                            raise WireError('probe emitted oversized HID count')
                        if any(data[at + 2 + size:]):
                            raise WireError('probe emitted nonzero HID padding')
                        data = data[at + 2:at + 2 + size]
                    pending.extend(data)
                    while len(pending) >= 2:
                        size = int.from_bytes(pending[:2], 'little')
                        if size > self.max_frame:
                            raise WireError('probe emitted oversized USB message')
                        if len(pending) < size + 2:
                            break
                        if size:
                            replies.append(bytes(pending[2:2 + size]))
                        del pending[:size + 2]
                    if data and len(replies) >= count:
                        quiet = max(quiet or 0, self.clock() + self.settle)
                elif quiet is not None and self.clock() >= quiet:
                    if pending:
                        raise WireError('partial trailing USB frame')
                    return replies
            raise TimeoutError(f'USB replies: expected {count}, observed {len(replies)}')
        except Exception as exc:
            record['error'] = f'{type(exc).__name__}: {exc}'
            raise
        finally:
            record['ended_monotonic_ns'] = int(self.clock() * 1e9)

    def write(self, transfer, method, record):
        record['writes'].append({'hex': transfer.hex(), 'method': method,
                                'monotonic_ns': int(self.clock() * 1e9)})
        if self.backend.write(transfer, method=method) != len(transfer):
            raise OSError('short raw USB write')



def inspect(factory, *, kind, unit, spec, out, lock, adapter, input_size=None, output_size=None,
            report_id=0, out_packet_size=None):
    """Reserve new evidence and lock equipment before opening the supplied raw adapter.

The factory is responsible for explicitly selecting the raw endpoint and declaring
its shape. No USB enumeration, retry, interface choice or transport fallback is
provided here. The current virtual software adapter is one such implementation.
"""
    from datetime import datetime, timezone
    import json
    from pathlib import Path
    from . import __version__
    from .conformance import Checks, checker_sources_sha256, spec_identity
    from .hardware import equipment_lock

    registry, identity = spec_identity(spec)
    report = {'status': 'failed', 'spec': identity, 'client_version': __version__,
              'checker_sources_sha256': checker_sources_sha256(),
              'expected_unit': unit, 'transport_adapter': adapter,
              'usb_shape': dict(kind=kind, input_size=input_size, output_size=output_size,
                                report_id=report_id, out_packet_size=out_packet_size),
              'full_conformance': False, 'started_at': datetime.now(timezone.utc).isoformat()}
    with Path(out).open('x') as artifact:
        try:
            with equipment_lock(lock):
                wire = UsbWire(None, kind, input_size=input_size, output_size=output_size,
                               report_id=report_id, out_packet_size=out_packet_size)
                backend = factory()
                wire.backend = backend
                try:
                    checks = Checks(wire.send, registry, unit)
                    checks.usb_wire = wire
                    report.update(checks.run())
                finally:
                    backend.close()
        except Exception as exc:
            report.update(status='failed', error=f'{type(exc).__name__}: {exc}')
        report['finished_at'] = datetime.now(timezone.utc).isoformat()
        json.dump(report, artifact, ensure_ascii=False, indent=2)
        artifact.write('\n')
    return report
