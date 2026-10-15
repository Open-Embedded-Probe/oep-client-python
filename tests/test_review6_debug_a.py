"""Sixth review, oep-if-debug up to §3.2 and oep-if-plan: code divergences from oep-spec a09fa45 found and fixed, each
test failing on the code before the fix.

- debug §2: "線からの応答無しで失敗したやり取り（上の線切れに数える失敗）から、その線でやり取りが成功するまで ... probe はやり取りの
  間、その線を放した状態で休ませ" with the failures counted "要求の中でもコンソールの読みの中でも" - the bench's console poll
  of a target that did not answer left the connection's lines driven at their rest state (only a riscv-dm request
  released them).
- debug §3 / §4.6: "attach は保留中の havereset を先に確認応答する" (flags bit0) - the bench's attach under reset (TLV 0x05)
  reset the target through the line but left no havereset pending, so a second attach under reset reported none.
- debug §1 / core §7.4: "channel_group に無い role は role_channels だけで決まる" - the declarations checker required a
  channel_group to list both pin roles (or role_channels to list both), failing a wire whose group fixes SWDIO and
  whose role_channels give SWCLK; `oep pins` built such a wire's pairs from role_channels alone and scanned none; the
  bench took a group's missing SWCLK as 0xFFFF (its allowed pairs, where its targets sit) and could not boot the wire.
- debug §2: "probe は 1 つの要求の中で線を再試行してよい（どれだけ続けるかは probe が決め、遅い速さでの再試行を含む）" - the
  retry checker required the probe-owned retry to run at a slower speed than the first try.
"""

from __future__ import annotations

import random
import struct

from oep_client import catalog, endpoint, host, message as m, pins, registry as reg, virtual_bench
from oep_client.conformance_oep_declarations import OepDeclarationChecks
from oep_client.conformance_retry import RetryChecks
from test_conformance_oep_declarations import SELECTION, setup as decl_setup
from test_conformance_retry import setup as retry_setup
from test_virtual_bench_spec import W, Clock, Host, att

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
        virtual_bench._core("3.0.0", "esp32", "c0c0c0c0c0c1", 30, {},
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
