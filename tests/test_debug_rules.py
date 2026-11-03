"""Debug rules (oep-if-debug: the wires, attach, connections, riscv-dm, swd, arm-adi; oep-if-console, oep-if-plan,
oep-if-restart) in the host, the virtual bench and the checkers, each test pinned to the spec sentence it checks."""
from __future__ import annotations
import struct
import pytest
from oep_client import endpoint, message as m, virtual_bench
from oep_client.conformance.attach import AttachChecks
from test_conformance_attach import setup as attach_setup
from test_virtual_bench_spec import RV, W, Clock, Host, att, bench
import random
from oep_client import catalog, endpoint, host, message as m, pins, registry as reg, riscv, virtual_bench
from test_virtual_bench_spec import W, Host, att
from oep_client import riscv, virtual_bench
from oep_client.conformance.debug import DebugChecks
from oep_client.conformance.run import RunChecks
from oep_client.virtual_core import tlv
from test_conformance_debug import setup as debug_setup
from test_conformance_run import setup as run_setup
from test_host_rules_bounds_options import in_process
from oep_client import catalog, endpoint, host, message as m, pins, registry as reg, virtual_bench
from oep_client.conformance.oep_declarations import OepDeclarationChecks
from oep_client.conformance.retry import RetryChecks
from test_conformance_oep_declarations import SELECTION, setup as decl_setup
from test_conformance_retry import setup as retry_setup
from test_virtual_bench_spec import W, Clock, Host, att
from oep_client import arm, catalog, console, endpoint, host, message as m, registry as reg, riscv, uiapduino, \
    virtual_bench
from test_target_parts import FNS, ScriptedHost, ok
from test_virtual_bench_spec import Clock, Host
from oep_client.conformance.arm_adi import ArmAdiChecks
from oep_client.conformance.reset import ResetChecks
from oep_client.conformance.attach_model import AttachModel, Attachments
from oep_client.conformance.wire_options_model import WireOptionsModel
from test_conformance_arm_adi import setup as arm_setup
from test_conformance_reset import setup as reset_setup


def _attached(ep, h, pair):
    return struct.unpack_from("<I", h.ok(1, W["attach"], att(pair)))[0]


def test_a_read_block_whose_answer_would_not_fit_the_route_is_unsupported_before_the_bus():
    """debug §4.5: "必要な結果がframeへ収まらない要求は実行前にunsupportedで断る（core §4.4）" - also when the declared
    max_length allows the count (max_length is the function's limit, the frame the route's)."""
    ep, h = bench()
    pair = ep.pairs[1][0]
    conn = _attached(ep, h, pair)
    tg = ep.targets[(1, pair)]
    tg.halted = True
    ep.block_max[2] = 8192                                        # a function limit past this route's frame
    fits = (ep.probe.max_frame - 8) // 4
    before = tg.dmi_accesses
    r = h.raw(2, RV["read_block"], struct.pack("<IIH", conn, 0x20000000, fits + 1))
    assert r.resolution == m.REJECTED and r.detail == m.UNSUPPORTED and r.payload == b"\x00"
    assert tg.dmi_accesses == before
    r = h.raw(2, RV["read_block"], struct.pack("<IIH", conn, 0x20000000, fits))
    assert r.succeeded and len(r.payload) == 3 + 4 * fits


def test_a_count_0_block_is_success_done_0_even_on_a_running_hart():
    """debug §4.5: "count = 0 は success、done 0。" - nothing is moved, so the running hart (status state for a
    count > 0) does not stop it; an unknown connection is still no_resource."""
    ep, h = bench()
    pair = ep.pairs[1][0]
    conn = _attached(ep, h, pair)
    assert not ep.targets[(1, pair)].halted
    for op in (RV["read_block"], RV["write_block"]):
        r = h.raw(2, op, struct.pack("<IIH", conn, 0x20000000, 0))
        assert r.succeeded and r.payload == struct.pack("<HB", 0, 0)
        assert h.raw(2, op, struct.pack("<IIH", conn + 99, 0x20000000, 0)).detail == m.NO_RESOURCE
    r = h.raw(2, RV["read_block"], struct.pack("<IIH", conn, 0x20000000, 1))
    assert r.detail == m.FAILED and r.payload == struct.pack("<HB", 0, 5)


def test_an_attach_whose_reset_line_is_one_of_its_own_pins_is_refused_before_the_reset():
    """debug §3: "使用中のピンとの競合は reset 前に断る。" - the attach's own swdio / swclk are in use by it: a reset TLV
    naming one is unavailable cause 1 with that channel, nothing held or made."""
    ep = endpoint.Endpoint(virtual_bench.rp2350_pins(), Clock())
    h = Host(ep)
    h.open()
    for channel in (0, 1):
        r = h.raw(1, W["attach"], att((0, 1)) + m.tlv(0x04, struct.pack("<HH", channel, 10), critical=True))
        assert r.resolution == m.REJECTED and r.detail == m.UNAVAILABLE
        assert dict(m.split_tlvs(r.payload)).get(0x01) == bytes([1])
        assert not ep.conns
    r = h.raw(1, W["attach"], att((0, 1)) + m.tlv(0x04, struct.pack("<HH", 2, 10), critical=True))
    assert r.succeeded


def test_the_attach_checker_requires_swd_connections_to_carry_the_dpidr_as_tid():
    """debug §2.1: "swdはtid_scheme 2、tid_len 4、tidはDPIDR。" - the attach checker took an swd entry with scheme 0 and
    no tid (the RISC-V wires' "none") and the logical fixture listed one: both now follow the rule, and the old listing
    fails the checker."""
    model, c, a = attach_setup()
    r = AttachChecks(c, a).run()
    assert r["status"] == "passed", [(x["id"], x.get("error")) for x in r["checks"] if x["status"] != "passed"]
    model, c, a = attach_setup()
    restart = model.restart_fixture

    def old_listing():
        result = restart()
        e = model.extension
        e.connection_page = lambda fn, first: e.page(
            [struct.pack("<IHHIBB", rid, *s["pins"], s["speed"], 0, 0) for rid, s in e.connections.items()
             if s["fn"] == fn], first)
        return result
    model.restart_fixture = old_listing
    r = AttachChecks(c, a).run()
    assert next(x for x in r["checks"] if x["id"] == "OEP-ATTACH-CONSOLE-RESOURCE")["status"] == "failed"


def test_the_attach_checker_requires_the_reset_line_to_be_declared_as_role_3():
    """debug §3: "probe が reset に使ってよい channel は describe の role_channels の role 3（reset）で宣言する。宣言していない
    channel は、何も実行せずに rejected unsupported" - the logical fixture took reset on channel 8 without declaring it
    and the checker passed it; the fixture now declares it and a fixture that does not fails the checker's context."""
    model, c, a = attach_setup()
    assert (1, struct.pack("<BH", 3, 8) + b"\x01") in c.describe(1)
    describe = model.extension.describe
    model.extension.describe = lambda fn: tuple(row for row in describe(fn) if row[0] != 1)
    r = AttachChecks(c, a).run()
    assert r["checks"][0]["id"] == "OEP-ATTACH-CONTEXT" and r["checks"][0]["status"] == "failed"
    assert "role 3" in r["checks"][0]["error"]


def _old_scan_tags(e, fn, op, data):
    """The scan fixture's TLV reading before review 4: a later copy skipped unchecked, max_speed 0 not malformed."""
    from oep_client.virtual_core import Reject
    if fn > 3 or op != 16:
        return type(e).__mro__[1].request_tags(e, fn, op, data)
    result = {}
    while data:
        raw, size = struct.unpack_from("<BH", data)
        value = data[3:3 + size]
        data = data[3 + size:]
        tag = raw & 127
        if tag in result:
            continue
        if tag != 1:
            if raw & 128:
                raise Reject(11, bytes((raw,)))
            continue
        if size != 4:
            raise Reject(3)
        result[tag] = (raw, value)
    return result


@pytest.mark.parametrize("case", ["ZERO-SPEED"])
def test_the_scan_checker_requires_max_speed_0_to_be_malformed(case):
    """debug §1: "max_speed（u32 Hz）: ... 0はmalformed。" (scan: TLV 0x01) - a scan fixture that answers max_speed 0
    unsupported fails the scan checker."""
    from oep_client.conformance.scan import ScanChecks
    from test_conformance_scan import setup as scan_setup
    model, c, a = scan_setup()
    r = ScanChecks(c, a).run()
    assert next(x for x in r["checks"] if x["id"] == "OEP-SCAN-" + case)["status"] == "passed"
    model, c, a = scan_setup()
    install = model.install

    def old_install():
        install()
        e = model.extension
        e.request_tags = lambda fn, op, data: _old_scan_tags(e, fn, op, data)
    model.install = old_install
    r = ScanChecks(c, a).run()
    assert next(x for x in r["checks"] if x["id"] == "OEP-SCAN-" + case)["status"] == "failed"


def test_a_run_whose_answer_would_not_fit_the_route_is_unsupported_before_the_hart_runs():
    """core §4.4 (debug §4.4's run answer: status stopped dpc elapsed_us nvals values): "要求の引数から必要となる固定結果が
    その経路のmax_frameへ収まらない場合は、probeは実行前にrejected unsupported（固定部分の印0）で断る。" """
    ep = endpoint.Endpoint(virtual_bench.esp32_v003(64), Clock())
    h = Host(ep)
    h.open()
    pair = ep.pairs[1][0]
    conn = _attached(ep, h, pair)
    tg = ep.targets[(1, pair)]
    tg.halted = True
    fits = (ep.probe.max_frame - 5 - 11) // 4

    def run(n):
        return h.raw(2, RV["run"], struct.pack("<IIIB", conn, 0x20000000, 1, 0) + bytes([n])
                     + struct.pack(f"<{n}H", *([0x100A] * n)))
    r = run(fits + 1)
    assert r.resolution == m.REJECTED and r.detail == m.UNSUPPORTED and r.payload == b"\x00"
    assert tg.halted and tg.dpc == 0x100
    r = run(fits)
    assert r.succeeded and len(r.payload) == 11 + 4 * fits


def test_a_scan_tries_no_more_pairs_than_its_answer_can_report_in_one_frame():
    """debug §1: "応答の tried は要求の先頭から試した数。候補が残る間は 1 以上。host は未試行の候補を次の要求に載せる。" with
    core §4.4 (the answer within the route's max_frame): with every pair answering, one scan reports what fits and the
    host's next request carries the rest."""
    from oep_client import host as hh, riscv as target
    ep = endpoint.Endpoint(virtual_bench.rp2350_pins(), Clock())
    h = Host(ep)
    h.open()
    pairs = ep._role_pairs(1)[:200]
    for p in pairs:
        ep._target(1, p).present = True
    body = bytes([len(pairs)]) + b"".join(struct.pack("<HH", *p) for p in pairs)
    r = h.raw(1, W["scan"], body)
    assert r.succeeded and 5 + len(r.payload) <= ep.probe.max_frame
    tried, count = r.payload[:2]
    assert 1 <= tried < len(pairs) and count == tried
    ep = endpoint.Endpoint(virtual_bench.rp2350_pins(), Clock())
    for p in pairs:
        ep._target(1, p).present = True
    hst = hh.Host(lambda b: ep.handle(b, 0))
    hst.open(3000)
    assert [f.pins for f in target.Wire(hst).scan(pairs)] == pairs
    assert len([q for q in ep.requests if (q.fn, q.op) == (1, W["scan"])]) >= 2


@pytest.mark.parametrize("fn", [7, 8])
def test_the_metadata_checker_takes_a_block_max_length_past_the_route_frame(fn):
    """debug §4.5 (riscv-dm, fn 7) and §6 (arm-adi, fn 8: "1回の長さと読みの意味は riscv-dm（§4.5）と同じ"): the host takes
    min(max_length, max_frame - 8 / - 20), "大きい経路を小さい経路の転送量へ一律に制限しない" - the selected-metadata checker
    required 18 + max_length and 8 + max_length to fit max_frame (an old rule, with an old 18-byte write header)."""
    from oep_client.conformance.selected_metadata import SelectedMetadataChecks
    from oep_client.virtual_core import tlv
    from test_conformance_selected_metadata import setup as metadata_setup
    model, c = metadata_setup()
    model.extension.metadata[fn] = [tlv(3, struct.pack("<H", 4096))]
    r = SelectedMetadataChecks(c, model.metadata_selection).run()
    assert next(x for x in r["checks"] if x["id"] == f"OEP-METADATA-{fn}")["status"] == "passed", r["checks"]


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


def test_the_bench_refuses_every_wire_tlv_twice():
    """core §2.3: a known non-repeating TLV twice is malformed, whatever the later copy holds: scan's max_speed and
    idle_clock, attach's idle_clock and reset - and nothing runs."""
    from test_virtual_bench_spec import bench as p4
    ep, h = p4()
    pair = ep.pairs[1][0]
    scan = b"\x01" + struct.pack("<HH", *pair)
    SC, AT = reg.WIRE_RVSWD.tlv["scan"], reg.WIRE_RVSWD.tlv["attach"]
    first = later(SC["max_speed"], struct.pack("<I", 1500))
    for copy in (bytes(4), struct.pack("<I", 999), struct.pack("<I", 1500)):
        r = h.raw(1, W["scan"], scan + first + later(SC["max_speed"], copy))
        assert (r.resolution, r.detail) == (m.REJECTED, m.MALFORMED)
    for op, body, idle in ((W["scan"], scan, SC["idle_clock"]), (W["attach"], att(pair), AT["idle_clock"])):
        r = h.raw(1, op, body + later(idle, b"\x01") + later(idle, b"\x00", critical=False))
        assert (r.resolution, r.detail) == (m.REJECTED, m.MALFORMED)
    v = virtual_bench.esp32_v003()
    ep = endpoint.Endpoint(v, lambda: 0)
    h = Host(ep)
    h.open()
    fn = ep.fns["oep.wire.swio"]
    RESET = reg.WIRE_SWIO.tlv["attach"]["reset"]
    body = att(ep.pairs[fn][0], speed=1_000_000) + later(RESET, struct.pack("<HH", 23, 20))
    r = h.raw(fn, W["attach"], body + later(RESET, struct.pack("<HH", 23, 20)))
    assert (r.resolution, r.detail) == (m.REJECTED, m.MALFORMED)
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
    from oep_client.conformance.scan import ScanChecks
    from oep_client.conformance.scan_options import ScanOptionsChecks
    from oep_client.conformance.scan_model import ScanModel
    from oep_client.conformance.scan_options_model import ScanOptionsModel
    from test_conformance_scan import setup as scan_setup
    from test_conformance_scan_options import setup as options_setup
    strict_later_copies(ScanModel, monkeypatch)
    strict_later_copies(ScanOptionsModel, monkeypatch)
    for setup_, checks in ((scan_setup, ScanChecks), (options_setup, ScanOptionsChecks)):
        _, c, a = setup_()
        r = checks(c, a).run()
        assert r["status"] == "passed", [(x["id"], x.get("error")) for x in r["checks"] if x["status"] != "passed"]


def test_the_scan_checker_refuses_a_probe_that_takes_the_first_of_two_copies():
    """core §2.3: a model that drops the later max_speed copy and scans with the first fails OEP-SCAN-TWICE."""
    from oep_client.conformance.scan import ScanChecks
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
    assert "OEP-SCAN-TWICE" in failed


def test_riscv_dm_declared_names_halt_and_resume_as_optional():
    """The client listed halt / resume as always there; a probe offering dmi only declares neither."""
    _, hst, _ = in_process(virtual_bench.esp32_v003())
    hst.open(3000)
    conn, _ = riscv.Wire(hst, "oep.wire.swio").attach(halt=True)
    dm = riscv.RiscvDm(hst, conn)
    assert {"halt", "resume"} <= dm.declared()
    dm.ops = lambda: {riscv.RiscvDm.DMI}
    assert dm.declared() == set()
    dm.ops = lambda: {riscv.RiscvDm.DMI, riscv.RiscvDm.HALT}
    assert dm.declared() == {"halt"}


def test_debug_checker_takes_a_dmi_only_fixture():
    """The checker required halt and resume (17, 18) as mandatory; a dmi-only fixture is conforming and its halt /
    resume cases are not run."""
    model, c, a = debug_setup()
    install = model.install

    def dmi_only():
        install()
        model.extension.describe = lambda fn, d=model.extension.describe: (tlv(7, b'\x10\x01'),) if fn == 5 else d(fn)
    model.install = dmi_only
    model.install()
    r = DebugChecks(c, a).run()
    assert r['status'] == 'passed', [(row['id'], row.get('error')) for row in r['checks'] if row['status'] != 'passed']
    ids = {row['id'] for row in r['checks']}
    assert 'OEP-DEBUG-CONTEXT' in ids and 'OEP-DEBUG-MIXED' in ids and 'OEP-DEBUG-HARTSEL-RAW' in ids
    assert not ids & {'OEP-DEBUG-HALT-RESUME', 'OEP-DEBUG-HALT-FAILURE', 'OEP-DEBUG-HARTSEL-HALT'}


# ---- console §1: "最小はopen/read/close/streamsで" (marks / clear / mark / write 任意) --------------------------

def test_attach_checker_takes_a_minimal_console():
    """The attach checker required every console op; marks, clear, mark and write are optional."""
    model, c, a = attach_setup()
    install = model.install

    def minimal():
        install()
        d = model.extension.describe
        # open 0x10, read 0x11, close 0x16, streams 0x17
        model.extension.describe = lambda fn: (tlv(7, b'\x10\xc3'),) + d(fn)[1:] if fn == 4 else d(fn)
    model.install = minimal
    model.install()
    r = AttachChecks(c, a).run()
    assert r['status'] == 'passed', [(row['id'], row.get('error')) for row in r['checks'] if row['status'] != 'passed']


# ---- debug §4 run row: "abstractauto と haltreq は戻す" (no order between DATA1 and DATA0) ----------------------

def test_run_checker_takes_data0_restored_before_data1():
    """The run checker required DATA1 to be written back before DATA0; the run row names no order."""
    model, c, a = run_setup()
    install = model.install

    def reversed_restore():
        install()
        e = model.extension
        dispatch = e.dispatch_request

        def run(fn, op, payload, tags):
            value = dispatch(fn, op, payload, tags)
            if fn == 5 and op == 22:
                rows = [i for i, row in enumerate(e.restore) if row[0] == 'run-restore' and row[2] in (4, 5)][-2:]
                e.restore[rows[0]], e.restore[rows[1]] = e.restore[rows[1]], e.restore[rows[0]]
            return value
        e.dispatch_request = run
    model.install = reversed_restore
    r = RunChecks(c, a).run()
    assert r['status'] == 'passed', [(row['id'], row.get('error')) for row in r['checks'] if row['status'] != 'passed']


# ---- debug §4: "host は、dmi の要求で…DATA0 / DATA1 を変えたら、hart を走らせる前に、変える前の値を書き戻す" ------

def test_reset_run_writes_data0_back_before_the_hart_runs():
    """reset mode 0 runs the hart: the DATA0 that read_register changed is written back first (it was left changed, and
    a later resume wrote the pre-reset value over the restarted firmware's mailbox)."""
    ep, hst, _ = in_process(virtual_bench.esp32_v003())
    hst.open(3000)
    conn, _ = riscv.Wire(hst, "oep.wire.swio").attach(halt=True)
    tg = ep._target_of(conn)
    tg.dmi[0x04] = 0x0000CAFE                      # the console's mailbox word
    tg.regs[0x100A] = 0x12345678
    dm = riscv.RiscvDm(hst, conn)
    assert dm.read_register(0x100A) == 0x12345678 and tg.dmi[0x04] == 0x12345678
    dm.reset()
    assert tg.dmi[0x04] == 0x0000CAFE and dm.data_saved is None


RV = reg.TARGET_RISCV_DM.op
CON = reg.TARGET_CONSOLE.op
FLAGS = reg.WIRE_RVSWD.enum["attach_flags"]


def test_a_console_poll_that_goes_unanswered_releases_the_lines_until_one_succeeds():
    """debug §2: a console read is an exchange too - once one goes unanswered the connection's lines rest released;
    an answered one brings the rest state back."""
    clock = Clock()
    ep = endpoint.Endpoint(virtual_bench.p4_bench(), clock)
    h = Host(ep)
    h.open()
    pair = ep.pairs[1][0]
    cid = struct.unpack_from("<I", h.raw(1, W["attach"], att(pair)).payload)[0]
    con = ep.fns["oep.target.console"]
    assert h.raw(con, CON["open"], struct.pack("<IB", cid, 2)).succeeded
    assert {ep.pin_state(ch) for ch in pair} == {"wire"}
    ep.targets[(1, pair)].present = False                       # the target lost power: the polls go unanswered
    clock.t += 5
    ep.tick()
    assert {ep.pin_state(ch) for ch in pair} == {"wire-free"}
    ep.targets[(1, pair)].present = True
    clock.t += 5
    ep.tick()
    assert {ep.pin_state(ch) for ch in pair} == {"wire"}


def test_an_attach_under_reset_acknowledges_the_havereset_its_reset_left():
    """debug §3 (attach flags bit0) / §4.6: the reset line held and let go resets the hart - havereset is pending, and
    the attach acknowledges it first, also when an earlier attach had acknowledged the power-on one."""
    ep = endpoint.Endpoint(virtual_bench.esp32_v003(), lambda: 0)
    h = Host(ep)
    h.open()
    fn = ep.fns["oep.wire.swio"]
    pair = ep.pairs[fn][0]
    reset = m.tlv(reg.WIRE_SWIO.tlv["attach"]["reset"], struct.pack("<HH", 23, 10), critical=True)
    for _ in range(2):
        r = h.raw(fn, W["attach"], att(pair, speed=1_000_000) + reset)
        cid, _, flags, _ = struct.unpack_from("<IIBI", r.payload)
        assert flags & FLAGS["havereset_acked"] and not ep.targets[(fn, pair)].havereset
        assert h.raw(fn, W["detach"], struct.pack("<I", cid)).succeeded
    r = h.raw(fn, W["attach"], att(pair, speed=1_000_000))                 # no reset: nothing pending
    assert not struct.unpack_from("<IIBI", r.payload)[2] & FLAGS["havereset_acked"]


def test_the_declarations_checker_takes_a_group_for_swdio_and_role_channels_for_swclk():
    """core §7.4 / debug §1: a group fixing role 1 and role_channels giving role 2 declare usable pairs; a group whose
    role 2 is nowhere does not."""
    for role2, ok in ((catalog.role_channels(2, [1]), True), (None, False)):
        model, c = decl_setup()
        describe = model.extension.describe

        def split(fn, role2=role2):
            rows = list(describe(fn))
            if fn == 1:
                rows = [r for r in rows if r[0] not in (1, 6)] + [catalog.channel_group(1, [(1, 0)])]
                if role2 is not None:
                    rows.append(role2)
            return tuple(rows)
        model.extension.describe = split
        r = OepDeclarationChecks(c, SELECTION).run()
        pins_row = next(x for x in r["checks"] if x["id"] == "OEP-DECLARATION-1-PINS")
        assert (pins_row["status"] == "passed") == ok, pins_row.get("error")


def split_wire() -> virtual_bench.VirtualProbe:
    """An rvswd wire whose channel_group fixes SWDIO (19) and whose role_channels give SWCLK (20, 21)."""
    return virtual_bench.VirtualProbe("rvswd-split", 1024, [
        virtual_bench._core("3.0.0", "esp32", "c0c0c0c0c0c1", 30,
                            virtual_bench._transports([(virtual_bench.TRANSPORT["usb_cdc"], 0)])),
        virtual_bench.Offered(1, 0, "oep.wire.rvswd", (catalog.channel_group(1, [(1, 19)]),
                                                      catalog.role_channels(2, [20, 21]),
                                                      catalog.u8(virtual_bench.MAX_CONNECTIONS, 1))),
        virtual_bench.Offered(2, 0, "oep.target.riscv-dm", (catalog.u16(catalog.MAX_LENGTH, 64),)),
        virtual_bench._gpio(4, list(range(30))),
    ])


def test_the_bench_serves_a_wire_whose_group_and_role_channels_split_the_roles():
    """core §7.4 / debug §1: the bench's allowed pairs for that wire are (19, 20) and (19, 21) - not (19, 0xFFFF) -
    and an attach there makes a connection (the bench could not even boot it before)."""
    ep = endpoint.Endpoint(split_wire(), lambda: 0)
    assert ep.pairs[1] == [(19, 20), (19, 21)]
    h = Host(ep)
    h.open()
    assert h.raw(1, W["attach"], att((19, 20))).succeeded
    r = h.raw(1, W["attach"], att((20, 21)))
    assert (r.detail, r.payload) == (m.UNSUPPORTED, b"\x00")


def test_oep_pins_scans_the_pairs_of_a_group_and_role_channels_together():
    """core §7.4: an rvswd wire whose group fixes SWDIO (19) and whose role_channels give SWCLK (20, 21) is scanned
    on (19, 20) and (19, 21)."""
    ep = endpoint.Endpoint(split_wire(), lambda: 0)
    hst = host.Host(ep.handle, rng=random.Random(1))
    hst.open(lease_ms=60000)
    f = pins.PinFinder(hst, say=lambda _: None, sleep=lambda _: None, clock=lambda: 0.0, probe="P", wire="rvswd")
    f.kinds = {c: pins.Channel(c, pins.FLOATING, "1", "0") for c in range(30)}
    seen = []
    f.wire.scan = lambda pairs, *a, **kw: seen.append(list(pairs)) or []
    f.scan()
    assert seen and sorted(seen[0]) == [(19, 20), (19, 21)]


def test_the_retry_checker_takes_a_probe_owned_retry_at_the_same_speed():
    """debug §2: retrying at a slower speed is the probe's choice - a fixture retrying at the connection's speed passes
    the probe-owned read cases (the speed_hz kept is still checked)."""
    model, c, a = retry_setup()
    install = model.install

    def same_speed():
        install()
        e = model.extension
        packet = e.packet
        e.packet = lambda parent, kind, address, value, speed: packet(parent, kind, address, value,
                                                                      e.connections[parent]["speed"])
    model.install = same_speed
    r = RetryChecks(c, a).run()
    owned = [x for x in r["checks"] if x["id"].startswith("OEP-RETRY-OWNED")]
    assert owned and all(x["status"] == "passed" for x in owned), [(x["id"], x.get("error")) for x in owned]


W = reg.WIRE_RVSWD.op
CONSOLE = reg.TARGET_CONSOLE


# ---- debug §4: DATA0 back before the hart runs (uiapduino.run_payload) -----------------------------------------------

def test_run_payload_writes_data0_back_before_it_resumes_the_hart():
    ep = endpoint.Endpoint(virtual_bench.p4_x035(), Clock())
    hst = host.Host(ep.handle, rng=random.Random(1))
    hst.open(lease_ms=10000)
    tg = ep.target
    tg.dmi[riscv.DATA0] = 0x5A5A0001                              # the console's mailbox word, say
    writes = []
    write = tg.write_dmi

    def logged(address, value):
        writes.append((address, value))
        write(address, value)
    tg.write_dmi = logged
    uiapduino.run_payload(hst, riscv.Wire(hst), [0x0000006F])
    resume = next(i for i, (a, v) in enumerate(writes) if a == riscv.DMCONTROL and v & (1 << 30))
    last_command = max(i for i, (a, _) in enumerate(writes[:resume]) if a == 0x17)
    assert (riscv.DATA0, 0x5A5A0001) in writes[last_command:resume]
    assert tg.dmi[riscv.DATA0] == 0x5A5A0001 and not tg.halted


# ---- debug §4.6: the console's read acknowledges a pending havereset ------------------------------------------------

def _console_bench():
    ep = endpoint.Endpoint(virtual_bench.p4_bench(), Clock())
    hv = Host(ep)
    assert hv.open().succeeded
    pair = ep.pairs[1][0]
    r = hv.raw(1, W["attach"], struct.pack("<BHHI", 0, *pair, 4_000_000))
    assert r.succeeded
    cid = struct.unpack_from("<I", r.payload)[0]
    sid = struct.unpack_from("<I", hv.ok(3, CONSOLE.op["open"], struct.pack("<IB", cid, 3)))[0]
    return ep, hv, ep.targets[(1, pair)], cid, sid


def test_the_console_read_acknowledges_a_pending_havereset_with_a_restart_mark():
    ep, hv, tg, cid, sid = _console_bench()
    tg.havereset = True                                            # the target reset itself, and runs
    read = struct.pack("<IHBB", cid, 1, 2, 0x11)
    dmstatus = struct.unpack("<HBHI", hv.ok(2, RV["dmi"], read))[3]
    assert not tg.havereset and not dmstatus & 0xC0000              # the console's read before it acknowledged it
    assert [(mk[2], mk[4]) for mk in ep.streams[sid].marks] == [
        (CONSOLE.enum["mark_kind"]["restart"], CONSOLE.enum["mark_detail_restart"]["havereset"])]


def test_the_console_does_not_read_a_halted_hart():
    ep, hv, tg, cid, sid = _console_bench()
    tg.halted, tg.havereset = True, True
    hv.ok(2, RV["dmi"], struct.pack("<IHBB", cid, 1, 2, 0x11))
    assert tg.havereset and ep.streams[sid].marks == []           # console §3: no reading while it is halted


# ---- debug §6: MemAp over raw transfers when the probe offers transfer only ------------------------------------------

class RawAdi:
    """An ADIv6 MEM-AP at 0x2000 behind transfer alone: CSW / TAR / DRW (D00 / D04 / D0C), posted AP reads, RDBUFF.
    A DRW access past the 1 KB TAR was last written in is an error (ADI guarantees the auto-increment within it)."""

    def __init__(self):
        self.select, self.csw, self.tar, self.tar_set, self.posted, self.mem = 0, 0x43800052, 0, 0, 0, {}
        self.sizes = []

    def transfer(self, p):
        self.sizes.append(m.REQUEST_HEADER + len(p))
        n = struct.unpack_from("<H", p, 4)[0]
        p, at, out = p[6:], 0, []
        for _ in range(n):
            req = p[at]; at += 1
            ap, read, a = req & 1, req >> 1 & 1, (req >> 2 & 3) << 2
            value = None
            if not read:
                value = struct.unpack_from("<I", p, at)[0]; at += 4
            if not ap:
                if not read and a == 0x8:
                    self.select = value
                elif read and a == 0xC:
                    out.append(self.posted)
                continue
            addr = (self.select & ~0xF) | a
            if addr == 0x2D04:
                if read:
                    out.append(self.posted); self.posted = self.tar
                else:
                    self.tar = self.tar_set = value
                continue
            if addr == 0x2D0C:
                assert self.tar >> 10 == self.tar_set >> 10, "DRW past TAR's guaranteed auto-increment"
                if read:
                    out.append(self.posted); self.posted = self.mem.get(self.tar, self.tar)
                else:
                    self.mem[self.tar] = value
                self.tar += 4
                continue
            if addr == 0x2D00:
                if read:
                    out.append(self.posted); self.posted = self.csw
                else:
                    self.csw = value
        body = struct.pack("<HBBH", n, 0, 1, len(out)) + b"".join(struct.pack("<I", v) for v in out)
        assert m.RESULT_HEADER + len(body) <= 64, "an answer past the route's max_frame"
        return ok(body)


def test_mem_ap_reads_and_writes_with_raw_transfers_when_the_probe_has_no_block_ops():
    adi_fn = FNS["oep.target.arm-adi"]
    model = RawAdi()

    def describe(p):
        fn = struct.unpack_from("<H", p)[0]
        return ok(b"\x00" + (catalog.ops_tlv([reg.TARGET_ARM_ADI.op["transfer"]]) if fn == adi_fn else b""))
    hst = ScriptedHost({(0, m.OP_CONFIRM): lambda p: ok(struct.pack("<4sBHIBI", b"OEP!", 1, 64, 4096, 8, 0x1234)),
                        (0, m.OP_DESCRIBE): describe, (adi_fn, arm.ArmAdi.TRANSFER): model.transfer})
    adi = arm.ArmAdi(hst, 1, adiv6=True)
    mem = arm.MemAp(adi, 0x2000)
    base = 0x200003C0                                             # 16 words to the next 1 KB
    mem.write_block(base, list(range(40)))
    assert [model.mem[base + 4 * i] for i in range(40)] == list(range(40))
    assert mem.read_block(base, 40) == list(range(40))
    assert mem.read32(base + 4) == 1
    assert {op for _, op, _ in hst.log} == {m.OP_CONFIRM, m.OP_DESCRIBE, arm.ArmAdi.TRANSFER}   # no block op sent
    assert max(model.sizes) <= 64                                  # every request fits the route


# ---- debug §6: no feature bits for arm-adi - a features tag with none set is no defect ---------------------------------

def test_an_arm_adi_features_tag_without_bits_is_not_a_defect():
    from oep_client.conformance.selected_metadata import SelectedMetadataChecks
    from oep_client.virtual_core import tlv
    from test_conformance_selected_metadata import setup as metadata_setup
    model, c = metadata_setup()
    model.extension.metadata[8] = [tlv(3, struct.pack("<H", 8)), tlv(5, b"\0" * 4)]
    r = SelectedMetadataChecks(c, model.metadata_selection).run()
    assert next(row for row in r["checks"] if row["id"] == "OEP-METADATA-8")["status"] == "passed", r["checks"]


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
