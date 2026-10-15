"""Sixth review, core §5-§13 and transports: code divergences from oep-spec a09fa45 found and fixed.

- transports §5 / core §5.1: a length-framed resync whose input never went quiet sent the host's unsubscribe and end
  blind before closing the transport; the spec says "静かにできなければ経路を閉じて開き直す" and "boot_id を確認し、
  状態を読んでから次の状態変更を判断する" (tests in test_link_host.py and test_trace_transports.py).
- core §7.5: two USB devices sharing an `x-` serial were counted as one probe ("host は `x-` で始まる unit_id で経路を
  まとめず").
- core §7.2 / §2.3: the independent checkers refused a list answer with TLVs after its entries ("各 op の応答の固定部分
  （と、前に数か長さを置いた並び）の後ろは TLV の並び").
- core §6.4: the independent checker required the optional owner display ("probe は owner の保持と表示を実装しなくてよい").
- core §10.3: the subscription checker's unsupported case asked for a large batching condition that a probe may refuse
  unavailable cause 2 instead ("probe は購読用の保持量に上限を置いてよい").
"""

from __future__ import annotations

import struct

import pytest

from oep_client import conformance, link


# ---- core §7.5: an x- unit_id groups nothing ------------------------------------------------------------------------

def test_two_devices_with_the_same_x_serial_are_two_probes():
    usb = [{"serial": "x-oep", "location": "1-2", "ways": {"vendor"}},
           {"serial": "x-oep", "location": "1-3", "ways": {"vendor"}}]
    ports = [("/dev/ttyACM0", "x-oep", "1-2"), ("/dev/ttyACM1", "x-oep", "1-3")]
    got = link._merge_probes(usb, [{"serial": "x-oep", "location": None, "ways": {"hid"}}], ports)
    assert [(p.location, p.ways, p.ports) for p in got] == [
        ("1-2", ["vendor", "cdc"], ["/dev/ttyACM0"]),               # by location only
        ("1-3", ["vendor", "cdc"], ["/dev/ttyACM1"])]                # the HID entry (no location) is libusb's to show
    alone = link._merge_probes([], [{"serial": "x-oep", "location": None, "ways": {"hid"}},
                                    {"serial": "x-oep", "location": None, "ways": {"hid"}}], [])
    assert len(alone) == 2                                          # nothing ties them together


# ---- core §7.2 / §2.3: TLVs after the list entries ------------------------------------------------------------------

def _bench(monkeypatch, **kw):
    from oep_client import endpoint, virtual_bench
    now = [1000]
    monkeypatch.setattr('oep_client.conformance.time.sleep', lambda s: now.__setitem__(0, now[0] + int(s * 1000)))
    probe = virtual_bench.PROFILES['p4-bench']()
    return endpoint.Endpoint(probe, lambda: now[0], **kw), virtual_bench.unit_id_of(probe)


def _failed(report):
    return [(r['id'], r.get('error')) for r in report['checks'] if r['status'] not in ('passed', 'not_applicable')]


def test_the_core_checker_takes_tlvs_after_the_list_entries(monkeypatch):
    """"各 op の応答の固定部分（と、前に数か長さを置いた並び）の後ろは TLV の並び。後から足すものはすべてここに TLV で足す"
    (core §2.3): a list answer (§7.2) with an extension TLV after its entries, on every page and past the end."""
    from test_conformance import REG
    import copy
    ep, unit = _bench(monkeypatch, tail=b'\x7e\x01\x00x')
    report = conformance.Checks(ep.handle, copy.deepcopy(REG), unit).run()
    assert not [f for f in _failed(report) if f[0].startswith('CORE-')], _failed(report)


def test_the_discovery_and_smoke_checkers_take_tlvs_after_the_list_entries():
    import inspect
    from oep_client import conformance_core_smoke, conformance_discovery
    for module in (conformance_core_smoke, conformance_discovery):
        src = inspect.getsource(module)
        assert "at == len(p)" not in src and "tlvs(p[at:])" in src


# ---- core §6.4: owner is optional -------------------------------------------------------------------------------------

def _without_owner(handle):
    """A probe that does not implement owner (core §6.4): owner is an unknown TLV to it - a critical one is refused
    unsupported with the tag as received, a non-critical one ignored (§2.3)."""
    def send(req):
        role, corr, fn, op, sid = struct.unpack_from('<BHHBI', req)
        if fn == 0 and op == 16 and len(req) > 15:
            kept, at = b'', 15
            while at + 3 <= len(req):
                tag, n = struct.unpack_from('<BH', req, at)
                if tag == 0x81:
                    return struct.pack('<BHBB', 2, corr, 0, 11) + bytes((tag,))
                if tag != 0x01:
                    kept += req[at:at + 3 + n]
                at += 3 + n
            req = req[:15] + kept
        return handle(req)
    return send


def test_the_core_checker_takes_a_probe_without_the_owner_display(monkeypatch):
    """"probe は owner の保持と表示を実装しなくてよい。実装しない probe は、owner を知らない TLV として §2.3 に従って扱い、
    非 critical の owner は無視する" (core §6.4): CORE-OWNER is not applicable there, not failed."""
    from test_conformance import REG
    import copy
    ep, unit = _bench(monkeypatch)
    report = conformance.Checks(_without_owner(ep.handle), copy.deepcopy(REG), unit).run()
    rows = {r['id']: r for r in report['checks']}
    assert rows['CORE-OWNER']['status'] == 'not_applicable', rows['CORE-OWNER']
    assert report['status'] == 'passed', _failed(report)


# ---- core §10.3: a probe that bounds what it holds for subscriptions ------------------------------------------------

def test_the_subscription_checker_asks_the_unsupported_case_under_a_condition_any_probe_holds():
    """"probe は購読用の保持量に上限を置いてよい。要求したデータの送信条件を満たすための保持量が足りない場合は、何も変えずに
    rejected unavailable（cause 2）で断る" (core §10.3), and a request with several faults is refused with any one of them
    (core §4.3 order 4): IF-SUB-INVALID's unknown critical TLV goes with a condition that such a probe holds."""
    from test_conformance_subscriptions import build
    model, c, adapter, runner = build()

    def bounded(req):
        role, corr, fn, op, sid = struct.unpack_from('<BHHBI', req)
        if fn and op == 1 and len(req) >= 16 and struct.unpack_from('<H', req, 10)[0] > 4096:
            return struct.pack('<BHBB', 2, corr, 0, 4) + b'\x01\x01\x00\x02'   # unavailable, cause 2: nothing changes
        return model.handle(req)
    c.send = bounded
    report = runner.run()
    rows = {x['id']: x for x in report['checks']}
    assert rows['IF-SUB-INVALID']['status'] == 'passed', rows['IF-SUB-INVALID'].get('error')
    assert report['status'] == 'passed', [(x['id'], x.get('error')) for x in report['checks'] if x['status'] != 'passed']


# ---- core §5.1: a request given up on TCP -----------------------------------------------------------------------------

def test_a_tcp_request_given_up_without_a_resend_reopens_the_connection_before_the_next():
    """"再送を諦めたら、その経路の未解決の要求は結果不明となる。host は TCP では接続を閉じて開き直し ... boot_id を確認し"
    (core §5.1): a TCP link with its resend off (open_host's default there) gives up at the first missing answer; the next
    request goes on a new connection, after a confirm there."""
    from oep_client import message as m
    from test_trace_transports import TcpStream, confirming, req
    old = TcpStream(lambda msg: [])                                  # never answers
    new = TcpStream(confirming())
    lk = link.SerialLink.on_stream(old, "length", 0.2)
    lk.resend = False
    lk.reopener = lambda: new
    boots = []
    lk.on_boot_id = boots.append
    with pytest.raises(TimeoutError):
        lk.send(req(7))
    assert m.Result.unpack(lk.send(req(8))).corr == 8
    assert old.closed and boots == [0x11]
    assert [m.Request.unpack(w[2:]).op for w in old.writes] == [m.OP_CLOCK]
    assert [(r.op, r.corr) for r in (m.Request.unpack(w[2:]) for w in new.writes)][-1] == (m.OP_CLOCK, 8)
    assert m.Request.unpack(new.writes[0][2:]).op == m.OP_CONFIRM
