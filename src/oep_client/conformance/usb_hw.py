"""Run the raw bulk/HID checks on a real probe, and check its USB descriptors (transports §1-§3).

The device is the one the user names: VID:PID and its serial number, the unit id (compared without case, transports §3).
No other device is opened, no interface is guessed beyond transports §3's class / usage, and nothing is retried. The
backend moves whole USB transfers (bulk: a read ends at a short packet or a zero-length packet, which reads as b'') and
whole HID reports; it never strips, repairs or reassembles bytes. pyusb (libusb) only.

    python -m oep_client.conformance.usb_hw --kind bulk --unit UNIT --spec SPEC --out NEW.json --lock LOCK
"""
import argparse
import os
import queue
import threading

from .usb import inspect

MS_OS_20_UUID = bytes.fromhex('df60ddd88945c74c9cd2659d9e648a9f')   # MS OS 2.0 platform capability (little endian)
GET_DESCRIPTOR, BOS, HID_REPORT, SET_REPORT, OUTPUT = 0x06, 0x0F, 0x22, 0x09, 0x02
READ_TIMEOUT_MS = 1000   # a blocking read's own bound, not a verdict: the checks keep their own deadlines


def find_device(vid, pid, unit):
    """The one USB device of `vid`:`pid` whose serial number is `unit` (case not compared)."""
    import usb.core
    import usb.util
    found = []
    for dev in usb.core.find(find_all=True, idVendor=vid, idProduct=pid):
        try:
            serial = usb.util.get_string(dev, dev.iSerialNumber) if dev.iSerialNumber else None
        except (usb.core.USBError, ValueError):
            continue
        if serial is not None and serial.lower() == unit.lower():
            found.append(dev)
    if len(found) != 1:
        raise FileNotFoundError(f'{len(found)} USB devices {vid:04x}:{pid:04x} with serial {unit}')
    return found[0]


def _endpoints(intf, kind):
    import usb.util
    eps = [e for e in intf if usb.util.endpoint_type(e.bmAttributes) == kind]
    ins = [e for e in eps if usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN]
    outs = [e for e in eps if usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT]
    return ins, outs


def _report_descriptor(dev, number):
    """The HID report descriptor of interface `number`, the kernel driver detached first (Linux usbfs refuses the
    request while it is bound) and the interface left claimed. -> (descriptor, detached)."""
    import usb.util
    detached = False
    try:
        if dev.is_kernel_driver_active(number):
            dev.detach_kernel_driver(number)
            detached = True
    except NotImplementedError:
        pass
    usb.util.claim_interface(dev, number)
    desc = bytes(dev.ctrl_transfer(0x81, GET_DESCRIPTOR, HID_REPORT << 8, number, 4096, timeout=1000))
    return desc, detached


def _release(dev, number, detached):
    import usb.util
    usb.util.release_interface(dev, number)
    if detached:
        try:
            dev.attach_kernel_driver(number)
        except Exception:
            pass


def _winusb(dev):
    """MS OS 2.0: the BOS platform capability's vendor code, then the descriptor set holding compatible ID WINUSB.
    -> (found, detail)."""
    try:
        head = bytes(dev.ctrl_transfer(0x80, GET_DESCRIPTOR, BOS << 8, 0, 5, timeout=1000))
        bos = bytes(dev.ctrl_transfer(0x80, GET_DESCRIPTOR, BOS << 8, 0, int.from_bytes(head[2:4], 'little'),
                                      timeout=1000))
    except Exception as exc:
        return False, f'no BOS descriptor ({type(exc).__name__})'
    at = bos[0]
    while at + 3 <= len(bos):
        size, kind, cap = bos[at], bos[at + 1], bos[at + 2]
        if size == 0:
            break
        if kind == 0x10 and cap == 0x05 and bos[at + 4:at + 20] == MS_OS_20_UUID and size >= 28:
            total, code = int.from_bytes(bos[at + 24:at + 26], 'little'), bos[at + 26]
            try:
                desc = bytes(dev.ctrl_transfer(0xC0, code, 0, 7, total, timeout=1000))
            except Exception as exc:
                return False, f'MS OS 2.0 descriptor set not read ({type(exc).__name__})'
            return b'WINUSB' in desc, 'compatible ID WINUSB' if b'WINUSB' in desc else 'no compatible ID WINUSB'
        at += size
    return False, 'no MS OS 2.0 platform capability'


def descriptor_checks(dev, unit, usb_reg):
    """transports §3's descriptor rules, each {id, clause, status passed / failed / warning, detail}."""
    import usb.util
    from ..hid_stream import parse_report_descriptor
    out = []

    def add(name, clause, ok, detail, should=False):
        out.append({'id': name, 'clause': clause, 'status': 'passed' if ok else ('warning' if should else 'failed'),
                    'detail': detail})
    serial = usb.util.get_string(dev, dev.iSerialNumber) if dev.iSerialNumber else ''
    add('USB-DESC-SERIAL', 'transports §3; core §7.5', serial.lower() == unit.lower(), f'serial {serial!r}')
    project = (dev.idVendor, dev.idProduct) == (usb_reg['project_vid'], usb_reg['project_pid'])
    out.append({'id': 'USB-DESC-VID-PID', 'clause': 'transports §3', 'status': 'passed',
                'detail': f'{dev.idVendor:04x}:{dev.idProduct:04x}' + (' (the project VID:PID)' if project
                                                                     else ' (named by the user)')})
    vendor, hid, oep_hid = [], [], []
    cfg = dev.get_active_configuration()
    for intf in cfg:
        triple = (intf.bInterfaceClass, intf.bInterfaceSubClass, intf.bInterfaceProtocol)
        if triple == (usb_reg['vendor_bulk_class'], usb_reg['vendor_bulk_subclass'], usb_reg['vendor_bulk_protocol']):
            ins, outs = _endpoints(intf, usb.util.ENDPOINT_TYPE_BULK)
            vendor.append((intf.bInterfaceNumber, len(ins), len(outs)))
        if intf.bInterfaceClass == 3:
            hid.append(intf.bInterfaceNumber)
    add('USB-DESC-VENDOR', 'transports §3',
        len(vendor) <= 1 and all(i == 1 and o == 1 for _, i, o in vendor),
        f'OEP vendor interfaces (number, bulk IN, bulk OUT): {vendor}')
    for number in hid:
        desc, detached = _report_descriptor(dev, number)
        try:
            mine = [r for r in parse_report_descriptor(desc) if r['usage_page'] == usb_reg['hid_usage_page']]
            if any(r['usage'] == usb_reg['hid_usage'] for r in mine):
                oep_hid.append((number, sorted({r['report_id'] for r in mine})))
        finally:
            _release(dev, number, detached)
    add('USB-DESC-HID', 'transports §1 rule 4, §3',
        len(oep_hid) <= 1 and all(len(ids) == 1 for _, ids in oep_hid),
        f'OEP HID interfaces (number, report IDs): {oep_hid}')
    if vendor:
        ok, detail = _winusb(dev)
        add('USB-DESC-WINUSB', 'transports §3 (SHOULD)', ok, detail, should=True)
    return out


class _Reader:
    """Blocking reads in a thread: one item per completed transfer / report (a zero-length transfer is b'')."""
    def __init__(self, read):
        self._read, self._items, self._stop = read, queue.Queue(), threading.Event()
        self._error = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        import usb.core
        while not self._stop.is_set():
            try:
                self._items.put(bytes(self._read()))
            except usb.core.USBTimeoutError:
                continue
            except Exception as exc:
                if not self._stop.is_set():
                    self._error = exc
                    self._items.put(None)
                return

    def read(self, timeout):
        try:
            item = self._items.get(timeout=max(0, timeout))
        except queue.Empty:
            return None
        if item is None:
            raise OSError(f'USB read failed: {self._error}')
        return item

    def stop(self):
        self._stop.set()
        self._thread.join(READ_TIMEOUT_MS / 1000 + 1)


class BulkBackend:
    """The vendor interface's bulk IN / OUT pair, claimed."""
    def __init__(self, dev, usb_reg):
        import usb.util
        for intf in dev.get_active_configuration():
            if (intf.bInterfaceClass, intf.bInterfaceSubClass, intf.bInterfaceProtocol) == (
                    usb_reg['vendor_bulk_class'], usb_reg['vendor_bulk_subclass'], usb_reg['vendor_bulk_protocol']):
                ins, outs = _endpoints(intf, usb.util.ENDPOINT_TYPE_BULK)
                if len(ins) == 1 and len(outs) == 1:
                    break
        else:
            raise FileNotFoundError('no OEP vendor interface with one bulk IN / OUT pair')
        self.dev, self.number = dev, intf.bInterfaceNumber
        self.ep_in, self.ep_out = ins[0], outs[0]
        self.shape = dict(out_packet_size=outs[0].wMaxPacketSize, in_packet_size=ins[0].wMaxPacketSize)
        usb.util.claim_interface(dev, self.number)
        self._reader = _Reader(lambda: dev.read(self.ep_in.bEndpointAddress, 16384, timeout=READ_TIMEOUT_MS))

    def read(self, timeout):
        return self._reader.read(timeout)

    def write(self, data, method='out'):
        if method != 'out':
            raise ValueError('bulk has no SET_REPORT')
        return self.dev.write(self.ep_out.bEndpointAddress, data, timeout=1000)

    def close(self):
        import usb.util
        self._reader.stop()
        usb.util.release_interface(self.dev, self.number)
        usb.util.dispose_resources(self.dev)


class HidBackend:
    """The OEP HID interface: interrupt IN, and interrupt OUT or SET_REPORT (Output) as asked; reports with their ID."""
    def __init__(self, dev, usb_reg):
        import usb.util
        from ..hid_stream import parse_report_descriptor
        for intf in dev.get_active_configuration():
            if intf.bInterfaceClass != 3:
                continue
            desc, detached = _report_descriptor(dev, intf.bInterfaceNumber)
            reports = [r for r in parse_report_descriptor(desc)
                       if (r['usage_page'], r['usage']) == (usb_reg['hid_usage_page'], usb_reg['hid_usage'])]
            if reports:
                break
            _release(dev, intf.bInterfaceNumber, detached)
        else:
            raise FileNotFoundError('no OEP HID interface')
        r = reports[0]
        self.dev, self.number, self.detached = dev, intf.bInterfaceNumber, detached
        self.report_id = r['report_id']
        self.input_size = r['input'] + bool(self.report_id)     # sizes with the ID (conformance.usb)
        self.output_size = r['output'] + bool(self.report_id)
        ins, outs = _endpoints(intf, usb.util.ENDPOINT_TYPE_INTR)
        self.ep_in, self.ep_out = ins[0], (outs[0] if outs else None)
        self.shape = dict(input_size=self.input_size, output_size=self.output_size, report_id=self.report_id)
        self._reader = _Reader(lambda: dev.read(self.ep_in.bEndpointAddress, self.input_size, timeout=READ_TIMEOUT_MS))

    def read(self, timeout):
        return self._reader.read(timeout)

    def write(self, data, method='out'):
        if method == 'out' and self.ep_out is not None:
            return self.dev.write(self.ep_out.bEndpointAddress, data, timeout=1000)
        return self.dev.ctrl_transfer(0x21, SET_REPORT, OUTPUT << 8 | self.report_id, self.number, data, timeout=1000)

    def close(self):
        import usb.util
        self._reader.stop()
        _release(self.dev, self.number, self.detached)
        usb.util.dispose_resources(self.dev)


def main(argv=None):
    from . import spec_identity
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kind', choices=('bulk', 'hid'), required=True)
    for name, env in [('unit', 'OEP_CONFORMANCE_UNIT_ID'), ('spec', 'OEP_CONFORMANCE_SPEC'),
                      ('out', 'OEP_CONFORMANCE_OUT'), ('lock', 'OEP_HW_LOCK')]:
        parser.add_argument('--' + name, default=os.environ.get(env))
    parser.add_argument('--vid-pid', help='VID:PID in hex (default: the project VID:PID of the SPEC registry)')
    args = parser.parse_args(argv)
    if not all(getattr(args, name) for name in ('unit', 'spec', 'out', 'lock')):
        parser.error('explicit unit, SPEC checkout, new output file and existing shared lock required')
    registry, _ = spec_identity(args.spec)
    usb_reg = registry['usb']
    vid, pid = ((int(x, 16) for x in args.vid_pid.split(':')) if args.vid_pid
                else (usb_reg['project_vid'], usb_reg['project_pid']))

    def factory():                                    # under the equipment lock (conformance.usb.inspect)
        dev = find_device(vid, pid, args.unit)
        descriptors = descriptor_checks(dev, args.unit, usb_reg)
        probe = BulkBackend(dev, usb_reg) if args.kind == 'bulk' else HidBackend(dev, usb_reg)
        probe.descriptors = descriptors
        return probe
    report = inspect(factory, kind=args.kind, unit=args.unit, spec=args.spec, out=args.out, lock=args.lock,
                     adapter=f'pyusb {vid:04x}:{pid:04x} serial {args.unit}', shape_from_backend=True)
    failed = [c['id'] for c in report.get('usb_descriptors', []) if c['status'] == 'failed']
    print(f"{report['status']}{' (descriptors failed: ' + ', '.join(failed) + ')' if failed else ''}: {args.out}")
    return int(report['status'] != 'passed')

if __name__ == '__main__':
    raise SystemExit(main())
