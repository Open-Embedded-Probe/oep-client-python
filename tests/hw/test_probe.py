"""The pre-release hardware test (oep-spec docs/release-testing.ja.md §3), per board in OEP_HW_BOARDS, in this order:

  flash        the firmware (OEP_PROBE_DIR build or OEP_PROBE_VERSION release) onto the board, wait for its boot
  identity     confirm / list / describe: a new boot_id, the expected firmware string, the expected model
  config       probe.config set / get / save / state / unset with a disable item, a reboot in between (bridge boards)
  wire         scan, attach (with the reset TLV when OEP_HW_RESET names the line), halt -> read_block -> resume 50x
               with s0 / s1 / a0 / a1 read before and after each block op       (only with OEP_HW_TARGET)
  gpio         fixture gpio set / read on two free channels (outputs read back, pull-up / pull-down levels)
  uart         fixture uart configure / status (a loopback write / read with OEP_HW_UART_LOOP=rx,tx)
  port_speed   linktest.matrix at the speed in force and the board's candidate rates (OEP_HW_RATES), in / out /
               duplex, in flight 1 and the probe's max, one frame size; verdict: one at a time <= 1 % broken + lost
  session      lease expiry -> Expired, the same id resumed as swept (2), a force takeover locks the old id out

Every test skips when the flash step failed. Measurements go to the run's record (tests/hw/results/)."""
from __future__ import annotations

import dataclasses
import os
import time

import pytest

from oep_client import config, core, fixture, host as h, linktest, registry as reg, riscv

from . import firmware as fwmod, flash, record

pytestmark = pytest.mark.hw

GPIO_ROLE_LINE = reg.FIXTURE_GPIO.enum["role"]["line"]
UART_RX, UART_TX = reg.FIXTURE_UART.enum["role"]["rx"], reg.FIXTURE_UART.enum["role"]["tx"]
ERROR_RATE_MAX = float(os.environ.get("OEP_HW_ERROR_MAX", "") or 0.01)   # port_speed verdict: one at a time, (broken + lost) / frames


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, "") or default)


# ---- 1. flash ----------------------------------------------------------------------------------------------------------

def test_flash(run: record.Run):
    board = run.board
    rec = run.record("flash", port=run.port)
    if board.kind != "fake":
        run.firmware["before"] = run.peek()
    try:
        fw = fwmod.obtain(board.profile) if board.kind != "fake" else fwmod.Firmware({"kind": "on-board", "why": "fake"}, None)
    except fwmod.FirmwareError as e:
        run.flash_failed = f"firmware: {e}"
        rec["error"] = str(e)
        pytest.fail(str(e))
    run.firmware["source"] = fw.source
    run.firmware["expected"] = fw.version
    run.firmware["flashed"] = False
    if fw.source["kind"] == "on-board":
        rec["skipped"] = "nothing flashed: " + ("the fake probe" if board.kind == "fake" else "OEP_HW_NOFLASH")
    else:
        try:
            if board.kind == "esp32":
                rec.update(flash.flash_esp32(board.flash_port or run.port, fw.image_for("esp32")))
            elif board.kind == "esp32p4":
                rec.update(flash.flash_p4(board.unit_id, fw.image_for("esp32p4"), board.usbip_busid))
            elif board.kind == "rp2":
                cdc = run.port if run.port.startswith("/") else None      # none: the board is in BOOTSEL already
                rec.update(flash.flash_rp2(cdc, fw.image_for("rp2"), board.unit_id))
            else:
                pytest.fail(f"no flasher for board kind {board.kind!r}")
            run.firmware["flashed"] = True
        except flash.FlashSkipped as e:
            rec["skipped"] = str(e)
            run.firmware["expected"] = None          # whatever is on the board: recorded, not compared
            run.firmware["source"] = {"kind": "on-board", "why": str(e), "wanted": fw.source}
        except (flash.FlashError, fwmod.FirmwareError) as e:
            run.flash_failed = str(e)
            rec["error"] = str(e)
            pytest.fail(str(e))
    t0 = time.monotonic()
    hst = run.connect(timeout_s=45.0)
    info = record.probe_info(hst)
    run.firmware["after"] = info
    rec["boot_seconds"] = round(time.monotonic() - t0, 1)
    rec["firmware_after"] = info.get("firmware")


# ---- 2. confirm / list / describe ------------------------------------------------------------------------------------------

def test_identity(run: record.Run):
    hst = run.require()
    limits = hst.confirm()
    entries = core.list_entries(hst)
    info = record.probe_info(hst)
    rec = run.record("identity", boot_id=info["boot_id"], firmware=info.get("firmware"), model=info.get("model"),
                     unit_id=info.get("unit_id"), chip=info.get("chip"), revision=limits["revision"],
                     max_frame=limits["max_frame"], max_inflight=limits["max_inflight"],
                     interfaces=[f"{e.fn}:{e.name}@{e.revision}" for e in entries])
    assert limits["revision"] >= 1, "not a v1 probe"
    assert entries and entries[0].name == "oep.core"
    assert info.get("model") == run.board.model, f"model {info.get('model')!r}, expected {run.board.model!r}"
    before = run.firmware.get("before") or {}
    if run.firmware.get("flashed") and before.get("boot_id") is not None:
        rec["boot_id_before"] = before["boot_id"]
        assert info["boot_id"] != before["boot_id"], "boot_id did not change over the flash"
    expected = run.firmware.get("expected")
    rec["firmware_expected"] = expected
    if expected:
        assert info.get("firmware") == expected, f"firmware {info.get('firmware')!r}, expected {expected!r}"
    if run.board.unit_id:
        assert info.get("unit_id") == run.board.unit_id


# ---- 3. probe.config ------------------------------------------------------------------------------------------------------

def test_config(run: record.Run):
    board = run.board
    hst = run.take()
    cfg = config.ProbeConfig(hst)
    declared = cfg.describe()
    h0, items0 = cfg.get()
    rec = run.record("config", storage_bytes=declared.storage_bytes, slots_max=declared.slots_max,
                     items_before=len(items0), hash_before=h0)
    assert config.ITEM["disable"] in declared.items, "the probe declares no disable item"
    assert declared.storage_bytes > 0, "the probe declares no storage: nothing to save"
    label_ch, disable_ch = board.gpio[0], board.disable
    added = [config.Label(channel=label_ch, text="HW-A"), config.Disable(channel=disable_ch)]
    h1 = cfg.set(added)
    h_items, items1 = cfg.get()
    assert h_items == h1 == config.hash_of(items1), "set's hash is not the hash of what get returns"
    decoded = [config.decode(t, v) for t, v in items1]
    assert any(isinstance(it, config.Label) and it.channel == label_ch and it.text == "HW-A" for it in decoded)
    assert any(isinstance(it, config.Disable) and it.channel == disable_ch for it in decoded)
    rec.update(hash_set=h1, items_after_set=len(items1))
    # a plan naming the disabled channel is refused (unavailable, held by settings)
    gpio = fixture.Gpio(hst)
    with pytest.raises(h.Rejected) as e:
        core.plan_apply(hst, [(gpio.fn, GPIO_ROLE_LINE, disable_ch)])
    rec["disabled_refused_as"] = type(e.value).__name__
    assert isinstance(e.value, (core.PinsTaken, h.Unavailable))
    # save, state
    h_saved = cfg.save()
    st = cfg.state()
    rec.update(hash_saved=h_saved, storage=st.storage, saved_hash=st.saved_hash)
    assert h_saved == h1 and st.storage == "applied" and st.saved_hash == h_saved, f"state after save: {st}"
    # reboot: a bridge board's EN line through DTR / RTS (esptool's hard reset); a USB probe or the fake: skipped
    if board.resettable:
        flash.hard_reset(hst.link.stream)
        reboot = run.wait_reboot()
        rec["reboot"] = reboot
        cfg = config.ProbeConfig(hst)                 # fns found again on the new boot
        st2 = cfg.state()
        h2, items2 = cfg.get()
        rec.update(storage_after_reboot=st2.storage, saved_hash_after_reboot=st2.saved_hash, hash_after_reboot=h2)
        assert st2.storage == "applied" and st2.saved_hash == h_saved, f"saved settings not applied at boot: {st2}"
        decoded2 = [config.decode(t, v) for t, v in items2]
        assert any(isinstance(it, config.Disable) and it.channel == disable_ch for it in decoded2)
        assert any(isinstance(it, config.Label) and it.channel == label_ch for it in decoded2)
        hst = run.take()
    else:
        rec["reboot"] = "skipped: " + ("the fake probe" if board.kind == "fake" else "no reset line from the host (a USB probe)")
    # unset both, save: back to what was there
    h3 = cfg.unset([("label", label_ch), ("disable", disable_ch)])
    _, items3 = cfg.get()
    decoded3 = [config.decode(t, v) for t, v in items3]
    assert not any(isinstance(it, (config.Label, config.Disable)) and it.channel in (label_ch, disable_ch) for it in decoded3)
    assert h3 == h0, f"after unset the hash is {h3:#x}, before the test it was {h0:#x}"
    h_saved2 = cfg.save()
    st3 = cfg.state()
    rec.update(hash_after_unset=h3, saved_hash_final=st3.saved_hash)
    assert h_saved2 == h3 and st3.saved_hash == h3
    core.plan_apply(hst, [(gpio.fn, GPIO_ROLE_LINE, disable_ch)])      # enabled again
    core.plan_release(hst, [gpio.fn])


# ---- 4. the wire -----------------------------------------------------------------------------------------------------------

def test_wire(run: record.Run):
    spec = os.environ.get("OEP_HW_TARGET", "")
    if not spec:
        pytest.skip("no target wired (OEP_HW_TARGET=<name>[@swdio[,swclk]] says one is)")
    hst = run.take()
    name, _, pins_text = spec.partition("@")
    wires = [e.name for e in core.list_entries(hst, "oep.wire.")]
    assert wires, "the probe offers no oep.wire.* interface"
    wire = riscv.Wire(hst, wires[0])
    pairs = None
    if pins_text:
        nums = [int(v, 0) for v in pins_text.split(",")]
        pairs = [(nums[0], nums[1] if len(nums) > 1 else 0xFFFF)]
    rec = run.record("wire", target=name, wire=wires[0], pairs_asked=pairs)
    t0 = time.monotonic()
    found = wire.scan(pairs)
    rec["scan"] = [{"kind": f.kind, "pins": list(f.pins), "status": f.status} for f in found]
    rec["scan_seconds"] = round(time.monotonic() - t0, 2)
    assert found, "scan found no target"
    reset = os.environ.get("OEP_HW_RESET")
    reset_tlv = None
    if reset:
        ch = int(reset, 0)
        assert ch in wire.reset_channels(), f"channel {ch} is not one the wire allows for reset ({wire.reset_channels()})"
        reset_tlv = (ch, 20)
    conn, status = wire.attach(halt=True, pins=found[0].pins, reset=reset_tlv)
    rec.update(connection=conn, dmstatus=f"{status:#010x}", attach_flags=wire.flags, speed_hz=wire.speed_hz,
               halted=wire.halted, dpc=None if wire.dpc is None else f"{wire.dpc:#010x}",
               target_id=None if wire.target_id is None else wire.target_id[1].hex(), reset=reset_tlv)
    dm = riscv.RiscvDm(hst, conn)
    address = int(os.environ.get("OEP_HW_TARGET_ADDR", "0x20000000"), 0)
    loops = _env_int("OEP_HW_LOOPS", 50)
    regs = (0x1008, 0x1009, riscv.REG_A0, riscv.REG_A1)          # s0, s1, a0, a1
    changed = []
    t0 = time.monotonic()
    try:
        for i in range(loops):
            dm.halt()
            before = [dm.read_register(r) for r in regs]
            data = dm.read_block(address, 8)
            after = [dm.read_register(r) for r in regs]
            if before != after:
                changed.append({"loop": i, "before": [f"{v:#010x}" for v in before], "after": [f"{v:#010x}" for v in after]})
            dm.resume()
            assert len(data) == 32
    finally:
        rec.update(loops=loops, loop_seconds=round(time.monotonic() - t0, 2), registers_changed=changed,
                   block_address=f"{address:#010x}")
        try:
            wire.detach(conn)
        except h.OepError as e:
            rec["detach_error"] = str(e)
    assert not changed, f"s0 / s1 / a0 / a1 changed over a read_block in {len(changed)} of {loops} loops"


# ---- 5. fixture gpio -------------------------------------------------------------------------------------------------------

def test_gpio(run: record.Run):
    hst = run.take()
    g = fixture.Gpio(hst)
    a, b = run.board.gpio
    rec = run.record("gpio", channels=[a, b])
    core.plan_apply(hst, [(g.fn, GPIO_ROLE_LINE, a), (g.fn, GPIO_ROLE_LINE, b)])
    wrong = []
    levels = {}
    try:
        for ch in (a, b):
            for mode_name, mode, want in (("output_high", g.OUTPUT_HIGH, 1), ("output_low", g.OUTPUT_LOW, 0),
                                          ("input_pullup", g.INPUT_PULLUP, 1), ("input_pulldown", g.INPUT_PULLDOWN, 0)):
                g.set([(ch, mode)])
                time.sleep(0.005)
                level = g.read([ch])[0]
                levels[f"{ch}:{mode_name}"] = level
                if level != want:
                    wrong.append(f"channel {ch} {mode_name}: read {level}, expected {want}")
            g.set([(ch, g.INPUT)])
        both = g.read([a, b])
        levels["both:input"] = both
    finally:
        core.plan_release(hst, [g.fn])
        rec["levels"] = levels
    assert not wrong, "; ".join(wrong)


# ---- 6. fixture uart -------------------------------------------------------------------------------------------------------

def test_uart(run: record.Run):
    hst = run.take()
    loop = os.environ.get("OEP_HW_UART_LOOP", "")
    rx, tx = (int(v) for v in loop.split(",")) if loop else run.board.uart
    u = fixture.FixtureUart(hst)
    rec = run.record("uart", rx=rx, tx=tx, loopback=bool(loop))
    core.plan_apply(hst, [(u.fn, UART_RX, rx), (u.fn, UART_TX, tx)])
    try:
        actual = u.configure(115200, fixture.FixtureUart.EIGHT_N_1)
        st = u.status()
        rec.update(actual_115200=actual, status_configured=st.configured, status_baud=st.baud, status_format=st.format)
        assert abs(actual - 115200) <= 115200 * 0.05, f"configure(115200) ran at {actual}"
        assert st.configured == "session" and abs(st.baud - 115200) <= 115200 * 0.05 and st.format == 0, f"status {st}"
        rec["actual_9600"] = u.configure(9600, fixture.FixtureUart.format_byte(8, "N", 1))
        if loop:
            io = fixture.FixtureUartIO(hst)
            io.configure(115200, fixture.FixtureUart.EIGHT_N_1)
            payload = b"oep-hw-loop " + str(int(time.time())).encode()
            io.write(payload)
            got, deadline = b"", time.monotonic() + 2.0
            while len(got) < len(payload) and time.monotonic() < deadline:
                got += io.read(len(payload) - len(got))
                if len(got) < len(payload):
                    time.sleep(0.02)
            rec.update(loop_sent=len(payload), loop_received=len(got))
            assert got == payload, f"loopback: sent {payload!r}, got {got!r}"
    finally:
        core.plan_release(hst, [u.fn])


# ---- 7. port_speed (UART bridge) ---------------------------------------------------------------------------------------------

def test_port_speed(run: record.Run):
    board = run.board
    hst = run.take()
    limits = hst.confirmed()
    info = run.firmware.get("after") or {}
    if not any(t["kind"] == core.TRANSPORT_KIND["uart_bridge"] for t in info.get("transports", ())) \
            or getattr(hst.link, "framing", "") != "cobs":
        pytest.skip("port_speed is for a UART bridge probe only")
    if not info.get("port_speed"):
        pytest.skip("the probe declares no port_speed")
    inflight = sorted({1, limits["max_inflight"]})
    frames = _env_int("OEP_HW_FRAMES", 100)
    timeout = float(os.environ.get("OEP_HW_LT_TIMEOUT", "") or 0.3)
    rates = [None] + list(board.rates)
    rec = run.record("port_speed", rates=[r or "now" for r in rates], inflight=inflight, frames=frames,
                     size=limits["max_frame"] - 16, timeout=timeout)
    rows, failing = [], []
    t0 = time.monotonic()
    for asked, result in zip(rates, linktest.matrix(hst, rates=rates, patterns=list(linktest.PATTERNS), inflight=inflight,
                                                    frames=frames, timeout=timeout)):
        row = {"asked": asked or "now", "rate": result.rate, "actual": result.actual, "switched": result.switched,
               "why": result.why, "cells": []}
        for c in result.cells:
            cell = dataclasses.asdict(c)
            if not c.error:
                cell["error_rate"] = round(c.error_rate, 4)
                if c.inflight == 1 and c.error_rate > ERROR_RATE_MAX:
                    failing.append(f"{result.rate} {c.pattern} x1: {c.error_rate * 100:.1f} % (broken {c.broken}, lost {c.lost})")
            row["cells"].append(cell)
        if result.why and asked is None:
            failing.append(f"the speed in force: {result.why}")
        rows.append(row)
        print("  " + result.text().replace("\n", "\n  "), flush=True)
    rec.update(results=rows, seconds=round(time.monotonic() - t0, 1), failing=failing,
               link_counters={"corrupt": hst.link.corrupt, "stale": hst.link.stale, "noise": hst.link.noise,
                              "retries": hst.link.retries, "resyncs": hst.link.resyncs})
    assert not failing, "; ".join(failing)


# ---- 8. the session ---------------------------------------------------------------------------------------------------------

def test_session(run: record.Run):
    hst = run.require()
    try:
        if hst.session is not None:
            hst.end()
    except h.OepError:
        pass
    hst.session = None
    rec = run.record("session")
    opened = hst.open(lease_ms=1000, owner="oep tests/hw lease")
    rec.update(lease_ms=opened.lease_ms, resumed_first=opened.resumed)
    assert opened.lease_ms == 1000 and opened.resumed == 0
    hst.keepalive()
    time.sleep(1.6)
    with pytest.raises(h.Expired) as e:
        hst.keepalive()
    rec["expired"] = str(e.value)
    again = hst.open(session=hst.session, lease_ms=3000)
    rec["resumed_after_lapse"] = again.resumed
    assert again.swept and again.resumed == 2, f"a lapsed id re-opened says resumed {again.resumed}, expected 2 (swept)"
    old = hst.session
    hst.session = None
    forced = hst.open(lease_ms=3000, force=True, owner="oep tests/hw force")
    new = hst.session
    rec.update(forced_resumed=forced.resumed, forced_new_id=new != old)
    assert new != old and forced.resumed == 0
    hst.session = old
    with pytest.raises(h.Locked) as locked:
        hst.keepalive()
    rec["old_id_after_force"] = str(locked.value)
    hst.session = new
    hst.end()
    hst.session = None
