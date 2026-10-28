"""Round 8, debug."""
import struct

from oep_client.conformance_arm_adi import ArmAdiChecks
from oep_client.conformance_attach import AttachChecks
from oep_client.conformance_reset import ResetChecks
from oep_client.virtual_attach_model import AttachModel, Attachments
from oep_client.virtual_wire_options_model import WireOptionsModel
from test_conformance_arm_adi import setup as arm_setup
from test_conformance_attach import setup as attach_setup
from test_conformance_reset import setup as reset_setup


def _request(corr, fn, op, sid, payload=b""):
    return b"\1" + struct.pack("<HHBI", corr, fn, op, sid) + payload


def _open(model, sid=0x1234):
    reply = model.handle(_request(1, 0, 16, sid, struct.pack("<IB", 3000, 0)))
    assert reply[3:5] == b"\1\0"
    return sid


def _failed(result, name):
    return next(row for row in result['checks'] if row['id'] == name)['status'] == 'failed'


def test_attach_model_offers_method_1_only_with_attach_halt():
    """debug §3: method 1はdescribeのfeatures bit0（attach_halt）を宣言するときだけ提供する。提供しないmethodと未知の
    methodはunsupported（固定部分の印0）. The attach fixture declares no features: method 1 is refused on rvswd and
    swio; the wire-options fixture, which attaches halting, declares bit0."""
    model = AttachModel(lambda: 0, boot_id=17)
    sid = _open(model)
    for corr, fn, pair in ((2, 1, (0, 1)), (3, 2, (0, 0xFFFF))):
        assert not any(row[0] == 5 for row in model.extension.describe(fn))
        reply = model.handle(_request(corr, fn, 17, sid, struct.pack("<BHHI", 1, *pair, 2000)))
        assert reply[3:6] == b"\0\x0b\0", reply.hex()
        assert not model.extension.connections and not model.extension.touch
    options = WireOptionsModel(lambda: 0, boot_id=17)
    for fn in (1, 2):
        assert (5, struct.pack("<I", 1)) in [(row[0], row[3:]) for row in options.extension.describe(fn)]
    sid = _open(options)
    reply = options.handle(_request(2, 1, 17, sid, struct.pack("<BHHI", 1, 0, 1, 2000)))
    assert reply[3:5] == b"\1\0", reply.hex()


def test_attach_checker_refuses_method_1_offered_without_attach_halt(monkeypatch):
    """debug §3 (as above): a fixture that takes method 1 without features bit0 fails OEP-ATTACH-WIRE-METHOD."""
    monkeypatch.setattr(Attachments, 'methods', lambda self, fn: (0, 1) if fn in (1, 2) else (0,))
    _, c, a = attach_setup()
    assert _failed(AttachChecks(c, a).run(), 'OEP-ATTACH-WIRE-METHOD')


def test_arm_checker_lets_the_probe_retry_an_unanswered_transfer():
    """debug §2: probe は 1 つの要求の中で線を再試行してよい（どれだけ続けるかは probe が決め...）. A probe that sends an
    unanswered transfer (no ACK) again within the request - and still gets no answer - is conforming; only FAULT stops
    at once and WAIT has the fixture's retries."""
    model, c, a = arm_setup()
    install = model.install

    def retrying():
        install()
        e = model.extension
        packet = e.packet

        def again(parent, req, value=None):
            status, ack, result = packet(parent, req, value)
            if ack == 0:                                   # one more try of the same packet on the silent line
                status, ack, result = packet(parent, req, value)
            return status, ack, result
        e.packet = again
    model.install = retrying
    result = ArmAdiChecks(c, a).run()
    bad = [(row['id'], row.get('error')) for row in result['checks'] if row['status'] != 'passed']
    assert result['status'] == 'passed', bad


def test_reset_checker_lets_a_silent_dm_close_the_connection():
    """debug §4.3: ndmresetを解いた後の待ちは§2。DMが答えなければstatus lineのcompleted failed。必要な後始末が済まなければ
    connectionを閉じる（§4）. A probe that cannot count mode 2's haltreq as lowered on a DM that never answered closes the
    connection and its streams after the answer: DM-LINE and CONSOLE-DM-LINE accept that (the kept connection too)."""
    model, c, a = reset_setup()
    install = model.install

    def closing():
        install()
        e = model.extension
        dispatch = e.dispatch_request

        def close_on_silence(fn, op, payload, tags):
            value = dispatch(fn, op, payload, tags)
            if fn == 5 and op == 19 and e.reset_result == 'dm-line':
                parent, mode = struct.unpack('<IB', payload)
                if mode == 2:
                    e.close_connection(parent)
            return value
        e.dispatch_request = close_on_silence
    model.install = closing
    result = ResetChecks(c, a).run()
    bad = [(row['id'], row.get('error')) for row in result['checks'] if row['status'] != 'passed']
    assert result['status'] == 'passed', bad
