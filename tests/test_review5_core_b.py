"""Fifth review, core §6 to §13 (docs/trace/review5/core-b.json): each test fails on the code before its fix."""

import struct

import pytest

from oep_client import message as m
from test_link_host import Stream, answering, frame, make_link, result, sent


def test_a_result_for_a_corr_not_waited_for_is_read_past_on_a_length_framed_link():
    """core §10.1: "知らない role のフレームと、待っていない corr の応答は捨てる。" - on vendor bulk / HID (length frames that
    are not TCP) too: a well-formed result for another corr is dropped, no resync, no resend."""
    s = Stream(answering({0x13: b"\x00" + bytes(4)}))
    lk = make_link(s)
    s.rx += frame(result(6, b"old"))                              # 6 answered late
    reply = lk.send(m.Request(7, 0, m.OP_LOCK_STATE, b"").pack())
    assert m.Result.unpack(reply).corr == 7
    assert [op for _, _, op in sent(s)] == [m.OP_LOCK_STATE]      # no confirm, nothing sent again
    assert lk.stale == 1 and lk.resyncs == 0 and lk.retries == 0


def _revision_zero(page: bytes) -> bytes:
    """A list page whose fn 5 entry (revision 2 in the declaration model) says revision 0 instead."""
    out, at = bytearray(page), 3
    for _ in range(page[2]):
        fn, n = struct.unpack_from("<H", out, at)[0], out[at + 5]
        if fn == 5:
            out[at + 4] = 0
        at += 6 + n
    return bytes(out)


def test_a_listed_interface_at_revision_0_is_not_refused_by_the_discovery_checks():
    """core §7.2: "entry: fn(u16)、instance(u16)、revision(u8)、name_len(u8)、name" - the interface revision is any u8;
    nothing in core (§2.8, §7.2) or versioning sets a lower bound, so the checker (conformance.Checks.list_snapshot and
    DiscoveryChecks.lists) must not require revision >= 1."""
    from test_conformance_discovery import setup
    model, c, runner = setup()
    plain = model.extension.list_page
    model.extension.list_page = lambda first, limit: _revision_zero(plain(first, limit))
    report = runner.run()
    assert report['status'] == 'passed', report['checks']
    assert any(e['fn'] == 5 and e['revision'] == 0 for e in c.list_snapshot())


def test_the_core_smoke_list_takes_an_interface_at_revision_0():
    """core §7.2 (as above): CoreSmokeChecks' list entries take revision 0."""
    from oep_client.conformance_core_smoke import CoreSmokeChecks
    from test_conformance_core_smoke import REG, SPEC, SmallCore
    probe = SmallCore(1, named=True)
    plain = probe.send

    def send(request):
        reply = plain(request)
        if request[5] == 2 and reply[3] == 1:
            reply = reply[:12] + b"\x00" + reply[13:]               # the one entry's revision byte
        return reply
    report = CoreSmokeChecks(send, REG, 'minimal', spec=SPEC, observe_effects=probe.effects).run()
    assert report['status'] == 'passed', report['checks']


def test_the_equipment_config_takes_any_spec_unit_id_for_usb():
    """core §7.5: "unit_id | 個体の ID。必須。text で 1〜32 byte、使える文字は `a-z 0-9 -` だけ（チップの固有の番号を小文字の
    16 進にしたもの、など）" - hex of 12 or more is an example, not the rule; an x- one still names no unit."""
    import pytest
    from oep_client import hardware
    from test_hardware import physical_equipment
    config = physical_equipment()
    node = config.data['probes'][0]
    for unit in ('fafe', 'bench-probe-a'):
        node['identity'] = {'method': 'oep-unit-id', 'value': unit}
        node['transport'] = {'kind': 'usb', 'unit_id': unit}
        hardware.validate(config.data)
    node['identity'] = {'method': 'oep-unit-id', 'value': 'x-1'}
    node['transport'] = {'kind': 'usb', 'unit_id': 'x-1'}
    with pytest.raises(hardware.ConfigurationError, match='complete'):
        hardware.validate(config.data)


@pytest.mark.parametrize('refuse_replace', [True, False])
def test_the_subscription_checks_take_a_probe_that_bounds_what_it_holds(refuse_replace):
    """core §10.3: "probe は購読用の保持量に上限を置いてよい。要求したデータの送信条件を満たすための保持量が足りない場合は、何も変えずに
    rejected unavailable（cause 2）で断る。" - a probe that cannot hold min_bytes 65535 refuses that subscribe (the
    old subscription, its destination and seq kept); the checker must not require it to succeed."""
    from oep_client import virtual_core
    from test_conformance_subscriptions import build
    model, c, adapter, runner = build()
    extension = model.extension
    dispatch = extension.dispatch

    def bounded(fn, op, payload):
        if op == 1 and struct.unpack('<HI', payload)[0] > 4096:
            raise virtual_core.Reject(m.UNAVAILABLE, m.tlv(1, b'\x02'))   # cause 2: nothing changes
        return dispatch(fn, op, payload)
    extension.dispatch = bounded
    if not refuse_replace:                                          # a wrong refusal still fails: the cause must be 2
        extension.dispatch = lambda fn, op, payload: (_ for _ in ()).throw(virtual_core.Reject(
            m.UNAVAILABLE, m.tlv(1, b'\x05'))) if op == 1 and payload[:2] == b'\xff\xff' else dispatch(fn, op, payload)
    report = runner.run()
    rows = {x['id']: x['status'] for x in report['checks']}
    expected = 'passed' if refuse_replace else 'failed'
    assert rows['IF-SUB-REPLACE'] == rows['IF-SUB-EVENT-THRESHOLD'] == expected, report['checks']


def test_the_owner_shown_is_the_first_copy_even_when_a_later_copy_is_broken():
    """core §6.4 owner, read as core §2.3 says: "繰り返すと定義が言わない tag が 2 つ以上あれば、読む側は最初のものを使う。" and a
    response TLV whose value has not the defined length "は壊れている: host はそれを使わない" - the broken later copy is not
    used; it does not hide the first one (lock_state and rejected locked alike)."""
    from oep_client import host as h
    tail = m.tlv(h.OWNER, b"flash-tool") + m.tlv(h.OWNER, b"x" * 33)
    assert h.owner_of(tail) == "flash-tool"
    locked = h.rejection(m.Result(1, m.REJECTED, m.LOCKED, struct.pack("<I", 7) + tail))
    assert locked.owner == "flash-tool"
    assert h.owner_of(m.tlv(h.OWNER, b"x" * 33) + m.tlv(h.OWNER, b"ok")) is None   # a broken first copy: none used


def test_an_interface_client_uses_the_revision_it_speaks_when_the_probe_offers_two():
    """core §7.2: "複数の revision を提供するときは各 revision の組を明示的に選び、保存した設定を別の revision へ自動で移さない。" (and
    §2.8: "host は知らない revision のインターフェースを使わない。") - with oep.fixture.gpio listed at revision 2 first and at
    revision 1 after it, a revision-1 client takes the revision-1 fn instead of refusing the probe; with no revision 1 at
    all it still refuses (UnsupportedRevision)."""
    from oep_client import catalog, core, host as h
    name = b"oep.fixture.gpio"

    def probe(revisions):
        def send(raw):
            req = m.Request.unpack(raw)
            if req.op == m.OP_LIST:
                rows = b"".join(struct.pack("<HHBB", fn, 0, rev, len(name)) + name for fn, rev in revisions)
                return m.Result(req.corr, m.COMPLETED, m.SUCCESS, struct.pack("<HB", len(revisions), len(revisions))
                                + rows).pack()
            if req.op == m.OP_DESCRIBE:
                return m.Result(req.corr, m.COMPLETED, m.SUCCESS, b"\x00" + catalog.ops_tlv({0x10})).pack()
            raise AssertionError(req)
        return h.Host(send)

    class Gpio(core.Interface):
        NAME, REVISION = name.decode(), 1
    assert Gpio(probe([(3, 2), (5, 1)])).fn == 5
    with pytest.raises(core.UnsupportedRevision):
        Gpio(probe([(3, 2)]))


def test_an_optional_probe_interface_in_a_revision_the_client_does_not_speak_is_not_used():
    """core §2.8: "host は知らない revision のインターフェースを使わない。" with §7.2's explicit (name, revision) choice - an
    oep.probe.restart listed at revision 2 only is not used (the same as not offered: restart_max_ms None); listed at
    revision 2 and 1, the revision-1 fn is the one used."""
    from oep_client import catalog, core, host as h
    name = b"oep.probe.restart"

    def probe(revisions):
        def send(raw):
            req = m.Request.unpack(raw)
            if req.op == m.OP_LIST:
                rows = b"".join(struct.pack("<HHBB", fn, 0, rev, len(name)) + name for fn, rev in revisions)
                return m.Result(req.corr, m.COMPLETED, m.SUCCESS, struct.pack("<HB", len(revisions), len(revisions))
                                + rows).pack()
            if req.op == m.OP_DESCRIBE:
                fn = struct.unpack_from("<H", req.payload)[0]
                return m.Result(req.corr, m.COMPLETED, m.SUCCESS,
                                b"\x00" + catalog.tlv(core.TAG_RESTART_MAX_MS, struct.pack("<I", 100 * fn))).pack()
            raise AssertionError(req)
        return h.Host(send)
    assert core.restart_max_ms(probe([(3, 2)])) is None
    assert core.restart_max_ms(probe([(3, 2), (5, 1)])) == 500
