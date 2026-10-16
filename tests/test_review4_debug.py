"""Review 4 of oep-if-debug (a09fa45): the code divergences found, each test failing on the code before the fix."""
import struct

import pytest

from oep_client import endpoint, message as m, virtual_bench
from oep_client.conformance_attach import AttachChecks
from test_conformance_attach import setup as attach_setup
from test_virtual_bench_spec import RV, W, Clock, Host, att, bench


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


@pytest.mark.parametrize("case", ["ZERO-SPEED", "LATER-WIDTH"])
def test_the_scan_checker_requires_max_speed_0_and_a_bad_later_copy_to_be_malformed(case):
    """debug §1: "max_speed（u32 Hz）: ... 0はmalformed。" (scan: TLV 0x01) and core §2.3 "後続の同じtagも上の検証対象
    である。" - the logical scan fixture answered max_speed 0 unsupported and skipped a later copy unchecked, and the
    scan checker did not look; both cases now fail that reading."""
    from oep_client.conformance_scan import ScanChecks
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
    from oep_client.conformance_selected_metadata import SelectedMetadataChecks
    from oep_client.virtual_core import tlv
    from test_conformance_selected_metadata import setup as metadata_setup
    model, c = metadata_setup()
    model.extension.metadata[fn] = [tlv(3, struct.pack("<H", 4096))]
    r = SelectedMetadataChecks(c, model.metadata_selection).run()
    assert next(x for x in r["checks"] if x["id"] == f"OEP-METADATA-{fn}")["status"] == "passed", r["checks"]
