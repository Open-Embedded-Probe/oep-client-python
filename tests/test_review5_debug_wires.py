"""Fifth review, oep-if-debug before §4 (the wires, attach, connections, rvswd / swio / swd) and common: code divergences
from oep-spec a09fa45 found and fixed, each test failing on the code before the fix.

- debug §1 / core §7.4: "両方を書いた場合、選んだピンの組は channel_group のどれかに一致し、かつ role_channels の候補にも
  入っていなければならない。" - the bench took role_channels alone once a wire declared them, the client's candidate
  pairs took the groups alone, and `oep pins` never looked at a swio wire's channel_group.
- debug §2: "待ちと後始末を含め要求全体のmax_op_msのうちに終える。" - the bench's wait for the DM after an attach's reset line
  let go had all of max_op_ms, the hold_ms already spent not counted.
- debug §3 / console §1 (a09fa45 registry): the attach checker required the selected SPEC's attach TLVs to be exactly
  {reset: 5} and the console mechanisms to carry 'none': 255 - neither is a09fa45 (rvswd's idle_clock, swd's
  targetsel; the mechanisms are sdi / dmdata / dmseq), so it could not run against the real registry.
- debug §1 / §3 with core §2.3 ("後続の同じtagも上の検証対象である。" - the checks above include "この probe が扱わない値は
  rejected unsupported（受け取ったままの tag）"): the bench checked only the first copy of scan's max_speed (0 is malformed,
  below min_clock_hz unsupported), of idle_clock and of attach's reset TLV; the scan checkers required a probe to
  accept a later max_speed below min_clock_hz and a later idle_clock 2, and their models accepted them.
"""

from __future__ import annotations

import random
import struct

from oep_client import catalog, endpoint, host, message as m, pins, registry as reg, riscv, virtual_bench
from oep_client.conformance_attach import AttachChecks
from test_conformance_attach import setup as attach_setup
from test_virtual_bench_spec import W, Host, att

ATTACH_HALT = reg.WIRE_RVSWD.enum["features"]["attach_halt"]


def both_declared(name="oep.wire.rvswd") -> virtual_bench.VirtualProbe:
    """A wire with one channel_group (2, 3) and role_channels (role 1: 2, 4; role 2: 3, 5)."""
    roles = (catalog.role_channels(1, [2]),) if name == "oep.wire.swio" else (
        catalog.role_channels(1, [2, 4]), catalog.role_channels(2, [3, 5]))
    group = catalog.channel_group(1, [(1, 2)] if name == "oep.wire.swio" else [(1, 2), (2, 3)])
    return virtual_bench.VirtualProbe("both", 1024, [
        virtual_bench._core("3.0.0", "rp2350", "b0b0b0b0b0b0", 30,
                            virtual_bench._transports([(virtual_bench.TRANSPORT["usb_cdc"], 0)])),
        virtual_bench.Offered(1, 0, name, (group,) + roles + (catalog.u32(catalog.FEATURES, ATTACH_HALT),
                                                             catalog.u8(virtual_bench.MAX_CONNECTIONS, 1))),
        virtual_bench.Offered(2, 0, "oep.target.riscv-dm", (catalog.u16(catalog.MAX_LENGTH, 64),)),
    ])


def test_a_pair_in_role_channels_but_in_no_channel_group_is_unsupported_on_the_bench():
    """core §7.4 (debug §1 "describe の channel_group / role_channels が候補を宣言する"): with both declared the pair must
    match a group and the role_channels candidates - (4, 5) is in role_channels only, (2, 5) matches no group."""
    ep = endpoint.Endpoint(both_declared(), lambda: 0)
    h = Host(ep)
    h.open()
    for pair in [(4, 5), (2, 5), (4, 3)]:
        r = h.raw(1, W["attach"], att(pair))
        assert (r.resolution, r.detail, r.payload) == (m.REJECTED, m.UNSUPPORTED, b"\x00"), pair
        r = h.raw(1, W["scan"], b"\x01" + struct.pack("<HH", *pair))
        assert (r.resolution, r.detail) == (m.REJECTED, m.UNSUPPORTED), pair
    assert h.raw(1, W["attach"], att((2, 3))).succeeded


def a_host(describes):
    def send(_):
        raise AssertionError("no request: the declarations are cached")
    hst = host.Host(send)
    hst._describes.update(describes)
    return hst


def wire_on(rows, name="oep.wire.rvswd"):
    w = riscv.Wire.__new__(riscv.Wire)
    w.host, w.fn, w.name, w.prefix = a_host({1: [m.split_tlvs(t)[0] for t in rows]}), 1, name, b""
    w.ONE_WIRE = name == "oep.wire.swio"
    return w


def test_the_client_candidate_pairs_meet_both_declarations():
    """core §7.4: the groups' pairs are kept only where role_channels (listing the role) has the channel; a role a
    group does not list comes from role_channels alone."""
    group = catalog.channel_group(1, [(1, 2), (2, 3)])
    assert wire_on([group]).candidate_pairs() == [(2, 3)]
    assert wire_on([group, catalog.role_channels(1, [2, 4]), catalog.role_channels(2, [3, 5])]).candidate_pairs() == [(2, 3)]
    assert wire_on([group, catalog.role_channels(1, [4]), catalog.role_channels(2, [3, 5])]).candidate_pairs() == []
    only_dio = catalog.channel_group(1, [(1, 2)])                     # role 2 from role_channels
    assert wire_on([only_dio, catalog.role_channels(2, [3, 5])]).candidate_pairs() == [(2, 3), (2, 5)]
    assert wire_on([catalog.channel_group(1, [(1, 7)]), catalog.role_channels(1, [8])], "oep.wire.swio").candidate_pairs() == []


def test_oep_pins_scans_a_swio_wire_declared_by_channel_group():
    """debug §1: "describe の channel_group / role_channels が候補を宣言する" - a swio wire whose SWIO pin is a
    channel_group (as the esp32-v003 profile) is scanned on that pin, not skipped for having no role_channels."""
    probe = virtual_bench.VirtualProbe("swio-group", 1024, [
        virtual_bench._core("3.0.0", "esp32", "c0c0c0c0c0c0", 30,
                            virtual_bench._transports([(virtual_bench.TRANSPORT["usb_cdc"], 0)])),
        virtual_bench.Offered(1, 0, "oep.wire.swio", (catalog.channel_group(1, [(1, 19)]),
                                                     catalog.u8(virtual_bench.MAX_CONNECTIONS, 1))),
        virtual_bench.Offered(2, 0, "oep.target.riscv-dm", (catalog.u16(catalog.MAX_LENGTH, 64),)),
        virtual_bench._gpio(4, list(range(30))),
    ])
    ep = endpoint.Endpoint(probe, lambda: 0)
    hst = host.Host(ep.handle, rng=random.Random(1))
    hst.open(lease_ms=60000)
    f = pins.PinFinder(hst, say=lambda _: None, sleep=lambda _: None, clock=lambda: 0.0, probe="P", wire="swio")
    f.kinds = {c: pins.Channel(c, pins.FLOATING, "1", "0") for c in range(30)}
    seen = []
    f.wire.scan = lambda pairs, *a, **kw: seen.append(list(pairs)) or []
    f.scan()
    assert seen and seen[0] == [(19, 0xFFFF)] and f.report.scanned == [19]


def test_the_wait_after_an_attach_reset_shares_max_op_ms_with_the_hold():
    """debug §2: "待ちと後始末を含め要求全体のmax_op_msのうちに終える。" - a DM silent for max_op_ms - 10 after a 20 ms hold
    is past the request's budget: status line, no connection; the wait recorded is what was left."""
    ep = endpoint.Endpoint(virtual_bench.esp32_v003(), lambda: 0)
    h = Host(ep)
    h.open()
    fn = ep.fns["oep.wire.swio"]
    pair = ep.pairs[fn][0]
    reset = m.tlv(reg.WIRE_SWIO.tlv["attach"]["reset"], struct.pack("<HH", 23, 20), critical=True)
    ep.targets[(fn, pair)].restart_ms = ep.max_op_ms - 10
    r = h.raw(fn, W["attach"], att(pair, speed=1_000_000) + reset)
    assert (r.resolution, r.detail, r.payload) == (m.COMPLETED, m.FAILED, bytes([reg.STATUS["line"]]))
    assert not ep.conns and ep.settle_log[-1] == ep.max_op_ms - 20
    ep.targets[(fn, pair)].restart_ms = ep.max_op_ms - 20                # just within: the hold and the wait
    assert h.raw(fn, W["attach"], att(pair, speed=1_000_000) + reset).succeeded


def test_the_attach_checker_runs_on_the_selected_registry():
    """The selected SPEC's attach TLVs (by name: the wire family's one space) and its console mechanisms (a named list):
    the context check must accept them."""
    _, c, a = attach_setup()
    assert {"idle_clock", "reset"} <= set(c.reg["interface"][0]["tlv"]["attach"])
    assert list(c.reg["interface"][3]["names"]["mechanism"]) == ["sdi", "dmdata", "dmseq"]
    r = AttachChecks(c, a).run()
    assert r["checks"][0]["status"] == "passed", r["checks"][0].get("error")
    assert r["status"] == "passed", [(x["id"], x.get("error")) for x in r["checks"] if x["status"] != "passed"]


def later(tag, value, critical=True):
    return m.tlv(tag, value, critical=critical)


def test_the_bench_checks_every_copy_of_a_wire_tlv():
    """core §2.3: a later copy of a known TLV is checked like the first (the first is used): scan's max_speed 0 is
    malformed, below min_clock_hz unsupported; idle_clock 2 and an attach reset copy naming an undeclared channel or a
    hold past max_op_ms are unsupported, each with the tag as that copy came."""
    from test_virtual_bench_spec import bench as p4
    ep, h = p4()
    pair = ep.pairs[1][0]
    scan = b"\x01" + struct.pack("<HH", *pair)
    SC, AT = reg.WIRE_RVSWD.tlv["scan"], reg.WIRE_RVSWD.tlv["attach"]
    first = later(SC["max_speed"], struct.pack("<I", 1500))
    r = h.raw(1, W["scan"], scan + first + later(SC["max_speed"], bytes(4)))
    assert (r.resolution, r.detail) == (m.REJECTED, m.MALFORMED)
    ep.decl[1].append((catalog.MIN_CLOCK_HZ, struct.pack("<I", 1000)))
    r = h.raw(1, W["scan"], scan + first + later(SC["max_speed"], struct.pack("<I", 999), critical=False))
    assert (r.resolution, r.detail, r.payload) == (m.REJECTED, m.UNSUPPORTED, bytes([SC["max_speed"]]))
    bad_idle = bytes([max(reg.WIRE_RVSWD.enum["idle_clock"].values()) + 1])
    for op, body, idle in ((W["scan"], scan, SC["idle_clock"]), (W["attach"], att(pair), AT["idle_clock"])):
        r = h.raw(1, op, body + later(idle, b"\x01") + later(idle, bad_idle, critical=False))
        assert (r.resolution, r.detail, r.payload) == (m.REJECTED, m.UNSUPPORTED, bytes([idle]))
    v = virtual_bench.esp32_v003()
    ep = endpoint.Endpoint(v, lambda: 0)
    h = Host(ep)
    h.open()
    fn = ep.fns["oep.wire.swio"]
    RESET = reg.WIRE_SWIO.tlv["attach"]["reset"]
    body = att(ep.pairs[fn][0], speed=1_000_000) + later(RESET, struct.pack("<HH", 23, 20))
    for copy in (struct.pack("<HH", 5, 20), struct.pack("<HH", 23, ep.max_op_ms + 1)):
        r = h.raw(fn, W["attach"], body + later(RESET, copy))
        assert (r.resolution, r.detail, r.payload) == (m.REJECTED, m.UNSUPPORTED, bytes([RESET | 0x80]))
        assert not ep.conns


def strict_later_copies(model_cls, monkeypatch):
    """A scan model that refuses a later max_speed below 1000 Hz or a later idle_clock other than 0 / 1 (core §2.3),
    whatever the model itself does."""
    install = model_cls.install

    def wrapped(self):
        install(self)
        parse = self.extension.request_tags

        def tags(fn, op, data):
            if fn <= 3 and op == 16:
                seen, rest = set(), data
                while len(rest) >= 3:
                    raw, n = struct.unpack_from("<BH", rest)
                    value, rest = rest[3:3 + n], rest[3 + n:]
                    tag = raw & 0x7F
                    if tag in seen and (tag == 1 and len(value) == 4 and struct.unpack("<I", value)[0] < 1000
                                        or tag == 4 and value[:1] not in (b"\x00", b"\x01")):
                        from oep_client.virtual_core import Reject
                        raise Reject(11, bytes((raw,)))
                    seen.add(tag)
            return parse(fn, op, data)
        self.extension.request_tags = tags
    monkeypatch.setattr(model_cls, "install", wrapped)


def test_the_scan_checkers_pass_a_probe_that_checks_later_copies(monkeypatch):
    """core §2.3: a probe that refuses an unhandled later copy follows the rule - the checkers' first-wins cases send
    valid later copies, and a later copy below min_clock_hz must be refused."""
    from oep_client.conformance_scan import ScanChecks
    from oep_client.conformance_scan_options import ScanOptionsChecks
    from oep_client.virtual_scan_model import ScanModel
    from oep_client.virtual_scan_options_model import ScanOptionsModel
    from test_conformance_scan import setup as scan_setup
    from test_conformance_scan_options import setup as options_setup
    strict_later_copies(ScanModel, monkeypatch)
    strict_later_copies(ScanOptionsModel, monkeypatch)
    for setup_, checks in ((scan_setup, ScanChecks), (options_setup, ScanOptionsChecks)):
        _, c, a = setup_()
        r = checks(c, a).run()
        assert r["status"] == "passed", [(x["id"], x.get("error")) for x in r["checks"] if x["status"] != "passed"]


def test_the_scan_checker_refuses_a_probe_that_takes_a_later_copy_below_min_clock():
    """core §2.3 with debug §1 (min_clock_hz): a model that ignores later max_speed copies fails OEP-SCAN-LATER-LOW."""
    from oep_client.conformance_scan import ScanChecks
    from test_conformance_scan import setup as scan_setup
    model, c, a = scan_setup()
    restart = model.restart_fixture

    def lax():
        out = restart()
        parse = model.extension.request_tags

        def tags(fn, op, data):
            if fn <= 3 and op == 16:
                keep, rest, seen = b"", data, set()
                while len(rest) >= 3:
                    raw, n = struct.unpack_from("<BH", rest)
                    item, rest = rest[:3 + n], rest[3 + n:]
                    if raw & 0x7F not in seen:
                        keep += item
                    seen.add(raw & 0x7F)
                data = keep + rest
            return parse(fn, op, data)
        model.extension.request_tags = tags
        return out
    model.restart_fixture = lax
    r = ScanChecks(c, a).run()
    failed = {x["id"] for x in r["checks"] if x["status"] != "passed"}
    assert "OEP-SCAN-LATER-LOW" in failed
