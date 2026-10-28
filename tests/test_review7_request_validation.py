"""Round 7, request validation."""
import struct

from oep_client.registry import WIRE_RVSWD as _WIRE, REJECT_REASONS as _R
from oep_client.conformance_capture_group import CaptureGroupChecks
from oep_client.conformance_capture_group_sample import SampleCaptureGroupAdapter
from oep_client.virtual_attach_model import AttachModel
from oep_client.virtual_capture_group_model import CaptureGroup
from oep_client.virtual_core import Reject
from test_conformance_capture_group import setup as group_setup


def _request(corr, fn, op, sid, payload=b""):
    return b"\1" + struct.pack("<HHBI", corr, fn, op, sid) + payload


def _open(model, sid=0x1234):
    reply = model.handle(_request(1, 0, 16, sid, struct.pack("<IB", 3000, 0)))
    assert reply[3:5] == b"\1\0"
    return sid


def test_attach_model_refuses_a_reset_hold_past_max_op_ms():
    """debug §3: hold_ms は core の max_op_ms の対象 / core §7.5: 明示的な待ちの引数がこれを超える要求は rejected
    unsupported - the reset TLV's tag as received (0x85), the first copy and a later one alike (core §2.3)."""
    model = AttachModel(lambda: 0, boot_id=17)
    sid = _open(model)
    limit = model.ep.max_op_ms
    RESET = _WIRE.tlv["attach"]["reset"]
    reset = lambda hold, raw=RESET | 0x80: bytes((raw,)) + struct.pack("<HHH", 4, model.extension.reset_channel, hold)
    attach = struct.pack("<BHHI", 0, 0, 1, 2000)
    for corr, tail, tag in ((2, reset(limit + 1), RESET | 0x80), (3, reset(10) + reset(limit + 1, RESET), RESET)):
        reply = model.handle(_request(corr, 1, _WIRE.op["attach"], sid, attach + tail))
        assert reply[3:6] == bytes((0, _R["unsupported"], tag)), reply.hex()
        assert not model.extension.connections and not model.extension.touch
    reply = model.handle(_request(4, 1, _WIRE.op["attach"], sid, attach + reset(limit)))   # max_op_ms itself is taken
    assert reply[3:5] == b"\1\0", reply.hex()


def test_group_checker_takes_unknown_function_for_a_bind_naming_no_fn(monkeypatch):
    """core §4.3: unknown_function - その fn は無い（見出しの fn、または payload で指す fn）; 順 4: 当たった理由のどれか
    1 つで断る. capture §4.1 makes an fn not in tracks unsupported: for an fn the probe does not have, both apply."""
    model, checks = group_setup()
    real = CaptureGroup.bind

    def bind(self, payload, tags, gid=4):
        fns = struct.unpack("<" + "H" * payload[0], payload[1:1 + 2 * payload[0]])
        if any(fn not in self.functions for fn in fns):
            raise Reject(1)                                        # the other reason that applies
        return real(self, payload, tags, gid)
    monkeypatch.setattr(CaptureGroup, 'bind', bind)
    checker = CaptureGroupChecks(checks, SampleCaptureGroupAdapter(model))
    checker.cases = ('BIND-UNKNOWN',)
    result = checker.run()
    assert result['status'] == 'passed', [(r['id'], r.get('error')) for r in result['checks'] if r['status'] != 'passed']
