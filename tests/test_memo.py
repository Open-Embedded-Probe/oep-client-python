"""oep_client.memo against a small in-memory probe of oep-if-memo: parts at the route's max_frame, paged lists, the crc."""

import struct

import pytest

from oep_client import core, memo, message as m, registry as reg

OP = reg.PROBE_MEMO.op
TLV = reg.PROBE_MEMO.tlv["describe"]


class FakeProbe:
    """The ops of oep-if-memo §2 on a dict, with a small max_frame so that reads, writes and lists go in parts."""

    def __init__(self, max_frame=80):
        self.link = type("L", (), {"max_frame": max_frame})()
        self.objects, self.stage, self.calls = {}, None, []

    def call(self, fn, op, payload=b"", *, locked=True, expect_ms=0):
        self.calls.append((op, len(payload), locked))
        assert len(payload) + reg.LIMITS["request_header_bytes"] <= self.link.max_frame
        out = getattr(self, f"_{[k for k, v in OP.items() if v == op][0]}")(payload)
        assert len(out) + reg.LIMITS["result_header_bytes"] <= self.link.max_frame
        return m.Result(0, m.COMPLETED, m.SUCCESS, out)

    @staticmethod
    def _name(p, at):
        n = p[at]
        return p[at + 1:at + 1 + n], at + 1 + n

    def _list(self, p):
        first = struct.unpack_from("<H", p)[0]
        prefix, _ = self._name(p, 2)
        names = [n for n in sorted(self.objects) if n.startswith(prefix)][first:]
        out, count = b"", 0
        for n in names:
            one = bytes([len(n)]) + n + struct.pack("<II", len(self.objects[n][0]), self.objects[n][1])
            if 2 + len(out) + len(one) + reg.LIMITS["result_header_bytes"] > self.link.max_frame:
                return bytes([1, count]) + out
            out, count = out + one, count + 1
        return bytes([0, count]) + out

    def _read(self, p):
        name, at = self._name(p, 0)
        offset, most = struct.unpack_from("<IH", p, at)
        data, crc = self.objects[name]
        room = self.link.max_frame - reg.LIMITS["result_header_bytes"] - 10
        part = data[offset:offset + min(most, room)]
        return struct.pack("<IIH", len(data), crc, len(part)) + part

    def _write(self, p):
        name, at = self._name(p, 0)
        size, offset, crc, n = struct.unpack_from("<IIIH", p, at)
        data = p[at + 14:at + 14 + n]
        if offset == 0:
            self.stage = [name, size, crc, b""]
        assert self.stage[:3] == [name, size, crc] and len(self.stage[3]) == offset
        self.stage[3] += data
        if len(self.stage[3]) == size:
            assert memo.crc32(self.stage[3]) == crc
            self.objects[name] = (self.stage[3], crc)
            self.stage = None
        return b""

    def _delete(self, p):
        name, _ = self._name(p, 0)
        del self.objects[name]
        return b""


@pytest.fixture
def probe(monkeypatch):
    fake = FakeProbe()
    monkeypatch.setattr(core, "find", lambda hst, name, revision=None: 7)
    monkeypatch.setattr(core, "describe", lambda hst, fn=0: [(TLV["max_object"], struct.pack("<I", 2048)),
                                                              (TLV["max_objects"], struct.pack("<H", 64))])
    return fake


def test_the_crc_is_crc32_iso_hdlc():
    """oep-if-memo §1: "123456789" -> 0xCBF43926, no data -> 0."""
    assert memo.crc32(b"123456789") == 0xCBF43926 and memo.crc32(b"") == 0


def test_a_write_and_a_read_go_in_parts_at_max_frame(probe):
    mm = memo.Memo(probe)
    assert (mm.max_object, mm.max_objects) == (2048, 64)
    data = bytes(range(256)) * 2
    mm.write("tool/a", data)
    writes = [c for c in probe.calls if c[0] == OP["write"]]
    assert len(writes) > 1 and all(c[2] for c in writes)                 # the lock
    assert mm.read("tool/a") == data
    assert all(not c[2] for c in probe.calls if c[0] in (OP["read"], OP["list"]))   # lock-free (oep-if-memo §2)


def test_an_empty_object_is_one_write(probe):
    mm = memo.Memo(probe)
    mm.write(b"e", b"")
    assert [c[0] for c in probe.calls] == [OP["write"]] and mm.read(b"e") == b""


def test_the_list_pages_and_filters_by_prefix(probe):
    mm = memo.Memo(probe)
    for i in range(12):
        mm.write(f"t/{i:02d}", bytes([i]))
    mm.write("other", b"x")
    probe.calls.clear()
    got = mm.list("t/")
    assert [e.name for e in got] == [f"t/{i:02d}".encode() for i in range(12)]
    assert got[3].size == 1 and got[3].crc == memo.crc32(bytes([3]))
    assert len([c for c in probe.calls if c[0] == OP["list"]]) > 1


def test_a_torn_object_raises(probe):
    mm = memo.Memo(probe)
    mm.write("t", b"abc")
    probe.objects[b"t"] = (b"abd", probe.objects[b"t"][1])               # oep-if-memo §3: shown, not hidden
    with pytest.raises(memo.MemoTorn):
        mm.read("t")
    assert mm.read("t", check=False) == b"abd"


def test_a_name_past_64_bytes_is_refused_before_sending(probe):
    mm = memo.Memo(probe)
    with pytest.raises(ValueError):
        mm.write("n" * 65, b"")
    assert probe.calls == []
