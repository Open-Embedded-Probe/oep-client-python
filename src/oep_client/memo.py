"""The memo area (oep-spec interfaces/oep-if-memo): named objects a host keeps on the probe.

    mm = memo.Memo(hst)
    mm.write("oep-bench/port/x035", b"...")           # the lock: in parts at the route's max_frame
    data = mm.read("oep-bench/port/x035")             # MemoTorn when its CRC-32 does not match its crc
    [(name, size, crc)] = mm.list("oep-bench/")

The probe never reads the objects; names are byte strings compared as they are (1..64 bytes of text).
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass

from . import core, message as m, registry as reg

_R = reg.PROBE_MEMO
OP = _R.op
REQUEST_HEADER = reg.LIMITS["request_header_bytes"]
RESULT_HEADER = reg.LIMITS["result_header_bytes"]


def crc32(data: bytes) -> int:
    """CRC-32/ISO-HDLC (oep-if-memo §1): zlib's; 0 for no data."""
    return zlib.crc32(data) & 0xFFFFFFFF


def _text(value: str | bytes) -> bytes:
    b = value.encode() if isinstance(value, str) else bytes(value)
    if len(b) > 64:
        raise ValueError(f"memo name/prefix of {len(b)} bytes: at most 64")
    return bytes([len(b)]) + b


class MemoTorn(Exception):
    """An object whose data does not match its crc (oep-if-memo §3): write it again or delete it."""


@dataclass
class Entry:
    name: bytes
    size: int
    crc: int


class Memo:
    def __init__(self, hst, fn: int | None = None):
        self.h = hst
        self.fn = core.find(hst, _R.name) if fn is None else fn
        found = dict(core.describe(hst, self.fn))
        self.max_object = struct.unpack("<I", found[_R.tlv["describe"]["max_object"]])[0]
        self.max_objects = struct.unpack("<H", found[_R.tlv["describe"]["max_objects"]])[0]

    def _frame(self) -> int:
        return getattr(self.h.link, "max_frame", reg.MIN_MAX_FRAME)

    def list(self, prefix: str | bytes = b"") -> list[Entry]:
        out, first = [], 0
        while True:
            p = self.h.call(self.fn, OP["list"], struct.pack("<H", first) + _text(prefix), locked=False).payload
            rd = m.Reader(p)
            more, count = rd.take("BB")
            for _ in range(count):
                n = rd.u8()
                name = rd.bytes(n)
                size, crc = rd.take("II")
                out.append(Entry(name, size, crc))
            first += count
            if not more or not count:
                return out

    def read(self, name: str | bytes, *, check: bool = True) -> bytes:
        room = self._frame() - RESULT_HEADER - 10
        data, size, crc = b"", None, None
        while size is None or len(data) < size:
            p = self.h.call(self.fn, OP["read"], _text(name) + struct.pack("<IH", len(data), min(room, 0xFFFF)),
                            locked=False).payload
            size, crc, n = struct.unpack_from("<IIH", p)
            if n == 0 and len(data) < size:
                raise m.ProtocolError(f"memo read gave no data at offset {len(data)} of {size}")
            data += p[10:10 + n]
        if check and crc32(data) != crc:
            raise MemoTorn(f"memo object {name!r}: CRC-32 {crc32(data):#010x}, crc {crc:#010x}")
        return data

    def write(self, name: str | bytes, data: bytes) -> None:
        head = _text(name)
        room = self._frame() - REQUEST_HEADER - len(head) - 14
        crc = crc32(data)
        at = 0
        while True:
            part = data[at:at + room]
            self.h.call(self.fn, OP["write"], head + struct.pack("<IIIH", len(data), at, crc, len(part)) + part)
            at += len(part)
            if at >= len(data):
                return

    def delete(self, name: str | bytes) -> None:
        self.h.call(self.fn, OP["delete"], _text(name))
