"""The pre-release hardware test (oep-spec docs/release-testing.ja.md §3), per board in OEP_HW_BOARDS, in this order:

  flash        the firmware (OEP_PROBE_DIR build or OEP_PROBE_VERSION release) onto the board, wait for its boot
  identity     confirm / list / describe: a new boot_id, the expected firmware string, the expected model
  config       probe.config set / get / save / state / unset with a disable item, a reboot in between (bridge boards)
  wire         scan, attach (with the reset TLV when OEP_HW_RESET names the line), halt -> read_block -> resume 50x
               with s0 / s1 / a0 / a1 read before and after each block op       (only with OEP_HW_TARGET)
  gpio         fixture gpio set / read on two free channels (outputs read back, pull-up / pull-down levels)
  uart         fixture uart configure / status (a loopback write / read with OEP_HW_UART_LOOP=rx,tx)
  capture      fixture logic: a one-shot at the lowest declared rate over a 10 ms window on the two free channels,
               pulled up then down through fixture gpio on the same pins -> all ones, then all zeros
  capture_analog  fixture analog (when listed): a one-shot on a free ADC channel at the lowest rate, the widest frontend
  capture_group   fixture capture-group (when listed): logic + analog bound and started together, both read back
  i2c_target   fixture i2c-target (when listed): configure 0x42 / status / preload a queue / reset / release
  spi_target   fixture spi-target (when listed): configure / status / arm one transaction / reset / release
  console      target console on the wire connection, mechanism dmseq: open, read for 1 s, close   (only with OEP_HW_TARGET)
  port_speed   linktest.matrix at the speed in force and the board's candidate rates (OEP_HW_RATES), in / out /
               duplex, in flight 1 and the probe's max, one frame size; verdict: one at a time <= 1 % broken + lost
  session      lease expiry -> Expired, the same id resumed as swept (2), a force takeover locks the old id out

Every test skips when the flash step failed. Measurements go to the run's record (tests/hw/results/)."""
from __future__ import annotations

import dataclasses
import os
import struct
import time

import pytest

from oep_client import capture, config, console as console_mod, core, fixture, host as h, linktest, registry as reg, riscv

from . import firmware as fwmod, flash, record

pytestmark = pytest.mark.hw

GPIO_ROLE_LINE = reg.FIXTURE_GPIO.enum["role"]["line"]
UART_RX, UART_TX = reg.FIXTURE_UART.enum["role"]["rx"], reg.FIXTURE_UART.enum["role"]["tx"]
CAPTURE_WINDOW_S = 0.01          # the capture tests' window: samples = rate x this (at least CAPTURE_MIN_SAMPLES)
CAPTURE_MIN_SAMPLES = 16
CAPTURE_BYTES_MAX = 16384        # a segment longer than this is cut (a 115200 bps link reads 11 KB/s)
I2C_ADDRESS = 0x42
# port_speed verdict (host guide §17.3.2): a one-at-a-time cell at a raised rate fails when broken + lost >= 3 and its ratio is
# over max(2 x the same cell's ratio at the boot speed, this floor). The floor is the guide's measured 5 %.
ERROR_RATE_FLOOR = float(os.environ.get("OEP_HW_ERROR_MAX", "") or 0.05)
MIN_BAD_FRAMES = 3


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
            elif board.kind == "esp32p4-usj":                       # the P4's own USB-Serial/JTAG: esptool, as a bridge
                rec.update(flash.flash_esp32_app(board.flash_port or run.port, fw.image_for("esp32p4")))
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

def _scan_settled(wire, pairs, rec: dict, settle_s: float = 2.0):
    """scan, again every 0.1 s for up to settle_s while it finds nothing: a target the probe powers from an output idle
    (the third P4's CH32V003) is switched on at the probe's boot and answered nothing for about 0.1 s after a flash
    (2026-10-02). The tries are recorded."""
    t0 = time.monotonic()
    tries = 0
    while True:
        tries += 1
        found = wire.scan(pairs)
        if found or time.monotonic() - t0 >= settle_s:
            rec["scan_tries"] = tries
            return found
        time.sleep(0.1)


def _wire_for(hst, pins_text: str):
    """The wires the probe offers (the one to use first) and the pair OEP_HW_TARGET names: one pin (swdio alone) is a
    one-wire link, so oep.wire.swio goes first when the probe offers it (OEP_HW_WIRE=<name> picks one by name)."""
    wires = [e.name for e in core.list_entries(hst, "oep.wire")]
    assert wires, "the probe offers no oep.wire.* interface"
    pairs = None
    if pins_text:
        nums = [int(v, 0) for v in pins_text.split(",")]
        pairs = [(nums[0], nums[1] if len(nums) > 1 else 0xFFFF)]
    want = os.environ.get("OEP_HW_WIRE") or ("oep.wire.swio" if pairs and pairs[0][1] == 0xFFFF else "")
    if want:
        assert want in wires, f"the probe does not offer {want} ({wires})"
        wires = [want] + [w for w in wires if w != want]
    return wires, pairs


def test_wire(run: record.Run):
    spec = os.environ.get("OEP_HW_TARGET", "")
    if not spec:
        pytest.skip("no target wired (OEP_HW_TARGET=<name>[@swdio[,swclk]] says one is)")
    hst = run.take()
    name, _, pins_text = spec.partition("@")
    wires, pairs = _wire_for(hst, pins_text)
    wire = riscv.Wire(hst, wires[0])
    rec = run.record("wire", target=name, wire=wires[0], pairs_asked=pairs)
    t0 = time.monotonic()
    found = _scan_settled(wire, pairs, rec)
    rec["scan"] = [{"kind": f.kind, "pins": list(f.pins), "dmstatus": f"{f.dmstatus:#010x}"} for f in found]
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
    planned_here = True
    try:
        core.plan_apply(hst, [(u.fn, UART_RX, rx), (u.fn, UART_TX, tx)])
    except h.Unavailable:
        # The probe's settings may already give this UART its pins (a jig's plan items): then the test runs on those
        # and leaves the plan alone (planning it twice is what the probe refuses).
        plans = [it for it in config.ProbeConfig(hst).items() if isinstance(it, config.Plan) and it.fn == u.fn]
        if not plans:
            raise
        rx = next((it.channel for it in plans if it.role == UART_RX), rx)
        tx = next((it.channel for it in plans if it.role == UART_TX), tx)
        rec.update(rx=rx, tx=tx, settings_plan=True)
        planned_here = False
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
        if planned_here:
            core.plan_release(hst, [u.fn])


# ---- 7. fixture capture: logic, analog, the group --------------------------------------------------------------------------

_LOGIC_D, _ANALOG_D, _GROUP_D = (reg.FIXTURE_LOGIC.tlv["describe"], reg.FIXTURE_ANALOG.tlv["describe"],
                                 reg.FIXTURE_CAPTURE_GROUP.tlv["describe"])


def _listed(hst: h.Host, name: str) -> bool:
    return bool(core.list_entries(hst, name, True))


def _free_channels(board) -> list[int]:
    """The table's channels wired to nothing, most preferred first: the gpio pair, the disable channel, the UART pair."""
    out: list[int] = []
    for ch in (*board.gpio, board.disable, *board.uart):
        if ch and ch not in out:
            out.append(ch)
    return out


def _capture_declared(decl: dict) -> dict:
    """The capture declarations (oep-if-capture §3.5) a test plans by: rate_range, the one-shot's max_samples, max_read,
    segment_ring, channels (max, the layouts), the frontends (analog)."""
    out: dict = {"features": decl.get("features")}
    for v in decl["own"].get(_LOGIC_D["mode"], ()):
        mode, background, max_samples, max_segments = struct.unpack_from("<BBII", v)
        out.setdefault("modes", {})[mode] = {"background": background, "max_samples": max_samples, "max_segments": max_segments}
    rr = decl["own"].get(_LOGIC_D["rate_range"])
    if rr:
        lo, hi, exact = struct.unpack_from("<IIB", rr[0])
        out["rate_range"] = {"min_hz": lo, "max_hz": hi, "exact": bool(exact)}
    elif decl.get("min_clock_hz") and decl.get("max_clock_hz"):      # the common tags instead (the fake's esp32 profile)
        out["rate_range"] = {"min_hz": decl["min_clock_hz"], "max_hz": decl["max_clock_hz"], "exact": False}
    rates: list[int] = []
    for v in decl["own"].get(_LOGIC_D["rate_list"], ()):
        rates += list(struct.unpack_from(f"<{v[0]}I", v, 1))
    if rates:
        out["rate_list"] = rates
    ch = decl["own"].get(_LOGIC_D["channels"])
    if ch:
        out["channels"] = {"max": ch[0][0], "layouts": [1 << i for i in range(32) if struct.unpack_from("<I", ch[0], 1)[0] >> i & 1]}
    out["max_read"] = record.own_u(decl, _LOGIC_D["max_read"])
    out["segment_ring"] = record.own_u(decl, _LOGIC_D["segment_ring"], "<H")
    for v in decl["own"].get(_ANALOG_D["frontend"], ()):
        fe, lo, hi, att = struct.unpack_from("<BiiI", v)
        out.setdefault("frontends", {})[fe] = {"min_mv": lo, "max_mv": hi, "attenuation_mdb": att}
    return out


def _capture_rate_samples(declared: dict) -> tuple[int, int]:
    """The lowest declared rate (at least 1 kHz) and the samples of a CAPTURE_WINDOW_S window at it, within the
    one-shot's max_samples."""
    rr = declared.get("rate_range")
    if rr:
        rate = min(max(rr["min_hz"], 1000), rr["max_hz"])
    elif declared.get("rate_list"):
        rate = min(declared["rate_list"])
    else:
        rate = 1000
    samples = max(CAPTURE_MIN_SAMPLES, round(rate * CAPTURE_WINDOW_S))
    max_samples = (declared.get("modes", {}).get(capture.ONE_SHOT) or {}).get("max_samples")
    if max_samples:
        samples = min(samples, max_samples)
    return rate, samples


def _configure(cap: capture.LogicCapture, rate: int, samples: int, **kw) -> capture.Config:
    """configure a one-shot, cut to CAPTURE_BYTES_MAX when the probe's layout makes the segment longer."""
    cfg = cap.configure(rate=rate, samples=samples, **kw)
    if cfg.bytes > CAPTURE_BYTES_MAX:
        cfg = cap.configure(rate=rate, samples=max(CAPTURE_MIN_SAMPLES, CAPTURE_BYTES_MAX * samples // cfg.bytes), **kw)
    return cfg


def _one_shot(cap: capture.LogicCapture, cfg: capture.Config, fake: bool = False) -> tuple[capture.Segment, bytes, dict]:
    """start -> done -> the one segment read back, with the host's timing: the capture may not finish before its window
    (samples / actual_rate, less the declared rate uncertainty; not asked of the fake, whose one-shot is done as start
    answers), and finishes within a second after it."""
    window_s = float(cfg.samples / cfg.rate)
    t0 = time.monotonic()
    blocking = cap.start()
    segs = cap.wait(timeout=max(5.0, window_s + 5.0))
    done_s = time.monotonic() - t0
    assert len(segs) == 1, f"a one-shot gave {len(segs)} segments"
    seg = segs[0]
    assert seg.generation == cap.generation and seg.serial == 0 and seg.position == 0, f"segment {seg}"
    assert seg.samples == cfg.samples, f"segment has {seg.samples} samples, configure said {cfg.samples}"
    t1 = time.monotonic()
    data = cap.read_segment(seg)
    read_s = time.monotonic() - t1
    assert len(data) == cfg.bytes, f"read {len(data)} bytes of a {cfg.bytes}-byte segment"
    tolerance = max(cfg.rate_ppm / 1e6, 0.01)
    assert fake or done_s >= window_s * (1 - tolerance), f"done {done_s * 1e3:.1f} ms after start, the window is {window_s * 1e3:.1f} ms"
    assert done_s <= window_s + 1.0, f"done {done_s:.2f} s after start, the window is {window_s * 1e3:.1f} ms"
    st = cap.status()
    assert st.state == capture.STATE["done"] and st.serial_done == 1 and st.write_pos == cfg.bytes and not st.flags, f"status {st}"
    timing = {"blocking_ms": blocking, "done_s": round(done_s, 4), "read_s": round(read_s, 3), "window_s": round(window_s, 5),
              "start_ns": seg.start_ns, "start_uncertainty_ns": seg.start_uncertainty_ns, "flags": seg.flags,
              "generation": seg.generation}
    return seg, data, timing


def _config_record(cfg: capture.Config) -> dict:
    out = {"actual_rate": float(cfg.rate), "samples": cfg.samples, "segments": cfg.segments, "bytes": cfg.bytes,
           "jitter_kind": cfg.jitter_kind, "jitter_ns": cfg.jitter_ns, "rate_measured": cfg.rate_measured,
           "rate_ppm": cfg.rate_ppm, "blocking_ms": cfg.blocking_ms, "ignored": cfg.ignored}
    if cfg.width:
        out["layout"] = {"w": cfg.width, "pos": cfg.positions}
    else:
        out["layout"] = {"s": cfg.slot, "o": cfg.offset, "b": cfg.bits, "order": cfg.order}
        out.update(zero=cfg.zero, scale_nv=cfg.scale_nv, frontend_used=cfg.frontend, skew_ns=cfg.skew_ns,
                   reference=list(cfg.reference) if cfg.reference else None)
    return out


class _Pull:
    """How a capture test sets the level of a channel wired to nothing: fixture gpio's pull-up / pull-down on the same
    channel when the probe lets the capture share its pins (a logic capture listens only, oep-if-capture §1.2), else
    (logic only) the settings' idle item on the released channel, set before the capture's plan - or nothing: then the
    levels are recorded, not checked. The analog gets no idle fallback: an analog pad drops the pulls when it is planned
    (the ESP32's: 137 mV mean, 0 to 2575 of 4095, under an idle pull-up, 2026-10-02)."""

    def __init__(self, hst: h.Host, cap: capture.LogicCapture, assignments: list[tuple[int, int, int]], channels: list[int],
                 rec: dict, idle_fallback: bool = True):
        self.hst, self.cap, self.assignments, self.channels = hst, cap, assignments, channels
        self.gpio = fixture.Gpio(hst)
        self.cfg: config.ProbeConfig | None = None
        self.idle_set = False
        try:
            core.plan_apply(hst, assignments + [(self.gpio.fn, GPIO_ROLE_LINE, ch) for ch in channels])
            self.how = "gpio on the same channel"
        except h.Rejected as e:
            rec["shared_with_gpio"] = f"refused: {type(e).__name__}: {e}"
            if idle_fallback and _listed(hst, "oep.probe.config"):
                self.cfg = config.ProbeConfig(hst)
                self.how = "idle item on the released channel"
            else:
                self.how = None
                core.plan_apply(hst, assignments)
        rec["pull"] = self.how

    @property
    def checks(self) -> bool:
        return self.how is not None

    def set(self, up: bool) -> None:
        if self.cfg is not None:
            core.plan_release(self.hst, [self.cap.fn])
            self.cfg.set([config.Idle(channel=ch, mode="pull-up" if up else "pull-down") for ch in self.channels])
            self.idle_set = True
            core.plan_apply(self.hst, self.assignments)
        elif self.how:
            self.gpio.set([(ch, self.gpio.INPUT_PULLUP if up else self.gpio.INPUT_PULLDOWN) for ch in self.channels])
        time.sleep(0.005)

    def release(self) -> None:
        core.plan_release(self.hst, [self.cap.fn] + ([self.gpio.fn] if self.how and self.cfg is None else []))
        if self.idle_set:
            self.cfg.unset([("idle", ch) for ch in self.channels])


def test_capture(run: record.Run):
    hst = run.take()
    if not _listed(hst, capture.LogicCapture.NAME):
        pytest.skip("the probe does not list oep.fixture.logic")
    cap = capture.LogicCapture(hst)
    decl = record.declared(hst, cap.fn)
    declared = _capture_declared(decl)
    chans = list(run.board.gpio)
    allowed = decl["role_channels"]
    for role, ch in enumerate(chans):
        if role in allowed and ch not in allowed[role]:
            pytest.skip(f"channel {ch} is not one the logic capture's role {role} allows ({allowed[role]})")
    rate, samples = _capture_rate_samples(declared)
    rec = run.record("capture", channels=chans, rate=rate, samples_asked=samples, declared=declared)
    pull = _Pull(hst, cap, [(cap.fn, role, ch) for role, ch in enumerate(chans)], chans, rec)
    wrong, levels, generations, captures = [], {}, [], []
    judged = pull.checks and run.board.kind != "fake"            # the fake captures a counter, not its pins
    if not judged:
        rec["levels_judged"] = False
    try:
        for name, up in (("pull-up", True), ("pull-down", False)):
            pull.set(up)
            cfg = _configure(cap, rate, samples)
            rec["configured"] = _config_record(cfg)
            assert cfg.width and len(cfg.positions) == len(chans), f"layout w {cfg.width} pos {cfg.positions} for {len(chans)} channels"
            assert len(set(cfg.positions)) == len(cfg.positions) and all(p < cfg.width for p in cfg.positions), f"layout {cfg.positions}"
            seg, data, timing = _one_shot(cap, cfg, run.board.kind == "fake")
            captures.append(timing)
            generations.append(seg.generation)
            for k, ch in enumerate(chans):
                ones = sum(cap.channel(data, k, seg.samples))
                levels[f"{ch}:{name}"] = {"ones": ones, "samples": seg.samples}
                want = seg.samples if up else 0
                if judged and ones != want:
                    wrong.append(f"channel {ch} {name}: {ones} of {seg.samples} samples read 1, expected {want}")
    finally:
        pull.release()
        rec.update(levels=levels, captures=captures, generations=generations,
                   actual_rate=rec.get("configured", {}).get("actual_rate"), layout=rec.get("configured", {}).get("layout"),
                   samples=rec.get("configured", {}).get("samples"), seconds=captures[-1]["window_s"] if captures else None)
    assert len(generations) == 2 and generations[1] == generations[0] + 1, f"generations {generations}: not +1 per start"
    assert not wrong, "; ".join(wrong)


def _analog_channel(hst: h.Host, board, decl: dict) -> int:
    """The free channel the analog capture's role 0 allows (OEP_HW_ANALOG=<channel> names one)."""
    env = os.environ.get("OEP_HW_ANALOG")
    if env:
        return int(env)
    allowed = decl["role_channels"].get(0)
    for ch in _free_channels(board):
        if allowed is None or ch in allowed:
            return ch
    pytest.skip(f"no free channel of the table is one the analog capture allows ({allowed}); OEP_HW_ANALOG=<channel> names one")


def _widest_frontend(declared: dict) -> int | None:
    fes = declared.get("frontends")
    if not fes:
        return None
    return max(fes, key=lambda fe: fes[fe]["max_mv"] - fes[fe]["min_mv"])


def _analog_stats(ana: capture.AnalogCapture, data: bytes, samples: int) -> dict:
    vals = ana.values(data, 0, samples)
    mean = sum(vals) / len(vals)
    return {"min": min(vals), "max": max(vals), "mean": round(mean, 1), "mean_mv": round(ana.millivolts(0, mean), 1),
            "full_scale": (1 << ana.config.bits) - 1}


def test_capture_analog(run: record.Run):
    hst = run.take()
    if not _listed(hst, capture.AnalogCapture.NAME):
        pytest.skip("the probe does not list oep.fixture.analog")
    ana = capture.AnalogCapture(hst)
    decl = record.declared(hst, ana.fn)
    declared = _capture_declared(decl)
    ch = _analog_channel(hst, run.board, decl)
    rate, samples = _capture_rate_samples(declared)
    fe = _widest_frontend(declared)
    rec = run.record("capture_analog", channel=ch, rate=rate, samples_asked=samples, frontend=fe, declared=declared)
    pull = _Pull(hst, ana, [(ana.fn, 0, ch)], [ch], rec, idle_fallback=False)
    kw = {"frontends": {0: fe}} if fe is not None else {}
    wrong, values, captures = [], {}, []
    try:
        rounds = (("pull-up", True), ("pull-down", False)) if pull.checks else (("idle", None),)
        for name, up in rounds:
            if up is not None:
                pull.set(up)
            cfg = _configure(ana, rate, samples, **kw)
            rec["configured"] = _config_record(cfg)
            assert cfg.slot in (8, 16, 32) and 1 <= cfg.bits <= 31 and cfg.offset + cfg.bits <= cfg.slot, f"layout {rec['configured']['layout']}"
            assert cfg.order == [0], f"order {cfg.order} for one channel"
            if fe is not None:
                assert cfg.frontend.get(0) == fe, f"frontend_used {cfg.frontend}, asked {fe}"
            seg, data, timing = _one_shot(ana, cfg, run.board.kind == "fake")
            captures.append(timing)
            stats = _analog_stats(ana, data, seg.samples)
            values[name] = stats
            assert 0 <= stats["min"] and stats["max"] <= stats["full_scale"]
            if up is not None:
                # the bands: pulled up the pin sits at the supply, over every frontend's range (full scale); pulled down at 0
                band_ok = stats["mean"] >= 0.8 * stats["full_scale"] if up else stats["mean"] <= 0.2 * stats["full_scale"]
                if not band_ok:
                    wrong.append(f"{name}: mean {stats['mean']} of {stats['full_scale']} ({stats['mean_mv']} mV)")
        cal = ana.calibration()
        rec["calibration"] = {"factory": [(fe_, scheme, len(raw)) for fe_, scheme, raw in cal.factory], "vrefint": cal.vrefint,
                              "vrefint_nominal_mv": cal.vrefint_nominal_mv}
    finally:
        pull.release()
        rec.update(values=values, captures=captures, actual_rate=rec.get("configured", {}).get("actual_rate"),
                   layout=rec.get("configured", {}).get("layout"), samples=rec.get("configured", {}).get("samples"),
                   seconds=captures[-1]["window_s"] if captures else None)
    if not pull.checks:
        rec["bands"] = "not checked: the probe shares an analog channel with nothing (oep-if-capture §1.2), so no pull reaches it"
    assert not wrong, "; ".join(wrong)


def test_capture_group(run: record.Run):
    hst = run.take()
    for name in (capture.CaptureGroup.NAME, capture.LogicCapture.NAME, capture.AnalogCapture.NAME):
        if not _listed(hst, name):
            pytest.skip(f"the probe does not list {name}")
    grp, cap, ana = capture.CaptureGroup(hst), capture.LogicCapture(hst), capture.AnalogCapture(hst)
    decl = record.declared(hst, grp.fn)
    tracks_v = decl["own"].get(_GROUP_D["tracks"])
    tracks = list(struct.unpack_from(f"<{tracks_v[0][0]}H", tracks_v[0], 1)) if tracks_v else []
    declared = {"tracks": tracks, "max_tracks": record.own_u(decl, _GROUP_D["max_tracks"], "<B"), "features": decl.get("features"),
                "budget": [(struct.unpack_from("<I", v)[0], list(struct.unpack_from(f"<{v[4]}H", v, 5)))
                           for v in decl["own"].get(_GROUP_D["budget"], ())],
                "start_skew_ns": {fn: ns for fn, ns in (struct.unpack_from("<HI", v) for v in decl["own"].get(_GROUP_D["start_skew"], ()))}}
    if cap.fn not in tracks or ana.fn not in tracks:
        pytest.skip(f"the group binds tracks {tracks}, not logic {cap.fn} + analog {ana.fn}")
    chans = list(run.board.gpio)
    a_ch = _analog_channel(hst, run.board, record.declared(hst, ana.fn))
    l_rate, l_samples = _capture_rate_samples(_capture_declared(record.declared(hst, cap.fn)))
    a_decl = _capture_declared(record.declared(hst, ana.fn))
    a_rate, a_samples = _capture_rate_samples(a_decl)
    fe = _widest_frontend(a_decl)
    rec = run.record("capture_group", logic_channels=chans, analog_channel=a_ch, declared=declared)
    core.plan_apply(hst, [(cap.fn, k, ch) for k, ch in enumerate(chans)] + [(ana.fn, 0, a_ch)])
    bound = False
    try:
        l_cfg = _configure(cap, l_rate, l_samples)
        a_cfg = _configure(ana, a_rate, a_samples, **({"frontends": {0: fe}} if fe is not None else {}))
        rec.update(logic=_config_record(l_cfg), analog=_config_record(a_cfg))
        grp.bind([cap, ana])
        bound = True
        t0 = time.monotonic()
        blocking, start_ns = grp.start([cap, ana])
        st = grp.wait(timeout=10.0)
        done_s = time.monotonic() - t0
        rec.update(blocking_ms=blocking, start_ns=start_ns, generations=grp.generations, done_s=round(done_s, 4),
                   state=st.state, trigger_ns=st.trigger_ns, trigger_fn=st.trigger_fn)
        assert set(grp.generations) == {cap.fn, ana.fn}, f"the start answer names generations for {list(grp.generations)}"
        offsets = {}
        for track, cfg in ((cap, l_cfg), (ana, a_cfg)):
            segs = track.segments()
            assert len(segs) == 1 and segs[0].samples == cfg.samples, f"{track.name}: segments {segs}"
            assert segs[0].generation == grp.generations[track.fn], f"{track.name}: segment generation {segs[0].generation}, start said {grp.generations[track.fn]}"
            data = track.read_segment(segs[0])
            assert len(data) == cfg.bytes
            offsets[track.name] = segs[0].start_ns - start_ns
            assert 0 <= offsets[track.name] <= 1_000_000_000, f"{track.name} started {offsets[track.name]} ns from the group's start"
        rec["track_offset_ns"] = offsets
        window_s = max(float(l_cfg.samples / l_cfg.rate), float(a_cfg.samples / a_cfg.rate))
        rec["window_s"] = round(window_s, 5)
        assert done_s <= window_s + 1.0
    finally:
        if bound:
            try:
                grp.bind([])
            except h.OepError as e:
                rec["unbind_error"] = str(e)
        core.plan_release(hst, [cap.fn, ana.fn])


# ---- 8. fixture i2c-target / spi-target ------------------------------------------------------------------------------------

_I2C_QUEUE_DEPTH = reg.FIXTURE_I2C_TARGET.tlv["describe"]["queue_depth"]
_SPI_QUEUE_DEPTH = reg.FIXTURE_SPI_TARGET.tlv["describe"]["queue_depth"]


def _target_declared(decl: dict, queue_tag: int) -> dict:
    return {"queue_depth": record.own_u(decl, queue_tag, "<B"), "max_clock_hz": decl.get("max_clock_hz"),
            "max_length": decl.get("max_length"), "features": decl.get("features"), "implementation": decl.get("implementation")}


def _plan_roles(board, decl: dict, roles: list[int], env: str) -> dict[int, int]:
    """A free channel for each role (role_channels says which it may take; distinct channels; env `name=ch,ch,...`
    overrides), or the first channel_group when the interface comes as fixed pin sets. Skips when the table has too few."""
    text = os.environ.get(env, "")
    if text:
        chans = [int(v) for v in text.split(",")]
        assert len(chans) == len(roles), f"{env} needs {len(roles)} channels"
        return dict(zip(roles, chans))
    allowed = decl["role_channels"]
    if not allowed and decl["channel_groups"]:
        _, pins = decl["channel_groups"][0]
        return {role: ch for role, ch in pins if role in roles}
    out: dict[int, int] = {}
    for role in roles:
        for ch in _free_channels(board):
            if ch not in out.values() and (role not in allowed or ch in allowed[role]):
                out[role] = ch
                break
        else:
            pytest.skip(f"no free channel of the table for role {role} (allowed {allowed.get(role)}); {env}=<channels> names them")
    return out


def test_i2c_target(run: record.Run):
    hst = run.take()
    if not _listed(hst, fixture.I2cTarget.NAME):
        pytest.skip("the probe does not list oep.fixture.i2c-target")
    t = fixture.I2cTarget(hst)
    decl = record.declared(hst, t.fn)
    roles = _plan_roles(run.board, decl, [t.ROLE_SDA, t.ROLE_SCL], "OEP_HW_I2C")
    rec = run.record("i2c_target", sda=roles[t.ROLE_SDA], scl=roles[t.ROLE_SCL], address=I2C_ADDRESS,
                     declared=_target_declared(decl, _I2C_QUEUE_DEPTH) | {"max_stretch_us": t.max_stretch_us})
    features = decl.get("features") or 0
    core.plan_apply(hst, t.assignments(roles[t.ROLE_SDA], roles[t.ROLE_SCL]))
    try:
        st0 = t.status()
        rec["status_unconfigured"] = dataclasses.asdict(st0)
        assert st0.state == 0, f"state {st0.state} before configure"
        if features & reg.FIXTURE_I2C_TARGET.enum["features"]["preloaded_tx"]:
            t.configure(I2C_ADDRESS, t.MODE_PRELOADED_TX)
            st1 = t.status()
            rec["status_configured"] = dataclasses.asdict(st1)
            assert st1.state == 1 and st1.mode == t.MODE_PRELOADED_TX and st1.tx_slots == 0 and st1.queued == 0, f"status {st1}"
            depth = rec["declared"]["queue_depth"] or 2
            slots = [t.preload_tx(bytes([0xA0 + i, i])) for i in range(min(depth, 3))]
            st2 = t.status()
            rec.update(preloaded=slots, status_preloaded=dataclasses.asdict(st2))
            assert slots == list(range(1, len(slots) + 1)), f"preload_tx counted {slots}"
            assert st2.tx_slots == len(slots), f"status after {len(slots)} preloads: {st2}"
        else:
            rec["preloaded"] = "mode 3 not declared"
        t.configure(I2C_ADDRESS, t.MODE_FIXED_RX)
        t.arm_rx(4)
        st3 = t.status()
        rec["status_armed"] = dataclasses.asdict(st3)
        assert st3.state == 1 and st3.mode == t.MODE_FIXED_RX and st3.tx_slots == 0, f"status armed: {st3}"
        # the lines float (no controller, no pull-ups): a glitch may count as an error or a frame - recorded, not judged
        assert st3.armed or st3.queued, f"arm_rx did not arm: {st3}"
        pending, data = t.read_rx()
        rec["read_rx"] = [pending, data.hex()]
        t.reset()
        st4 = t.status()
        rec["status_reset"] = dataclasses.asdict(st4)
        assert st4.state == 1 and st4.mode == t.MODE_FIXED_RX and not st4.armed and st4.queued == 0 and st4.errors == 0 \
            and st4.rx_frames == 0, f"status after reset: {st4}"
    finally:
        core.plan_release(hst, [t.fn])


def test_spi_target(run: record.Run):
    hst = run.take()
    if not _listed(hst, fixture.SpiTarget.NAME):
        pytest.skip("the probe does not list oep.fixture.spi-target")
    t = fixture.SpiTarget(hst)
    decl = record.declared(hst, t.fn)
    roles = _plan_roles(run.board, decl, [t.ROLE_SCK, t.ROLE_MOSI, t.ROLE_MISO, t.ROLE_CS], "OEP_HW_SPI")
    rec = run.record("spi_target", sck=roles[t.ROLE_SCK], mosi=roles[t.ROLE_MOSI], miso=roles[t.ROLE_MISO], cs=roles[t.ROLE_CS],
                     declared=_target_declared(decl, _SPI_QUEUE_DEPTH))
    core.plan_apply(hst, t.assignments(roles[t.ROLE_SCK], roles[t.ROLE_MOSI], roles[t.ROLE_MISO], roles[t.ROLE_CS]))
    try:
        st0 = t.status()
        rec["status_unconfigured"] = dataclasses.asdict(st0)
        assert st0.state == 0, f"state {st0.state} before configure"
        t.configure(0, t.MSB_FIRST)
        st1 = t.status()
        rec["status_configured"] = dataclasses.asdict(st1)
        assert st1.state == 1 and st1.mode == 0 and st1.bit_order == 0 and not st1.armed, f"status {st1}"
        t.arm(4, b"\xa5\x5a")
        st2 = t.status()
        rec["status_armed"] = dataclasses.asdict(st2)
        # No controller is wired: no transfer happens, so the arm stays armed (a CS edge without SCK is no transfer, fixture
        # §4; the probe counted such edges as transactions before oep-probe-arduino a3dcabd), and a second arm is refused
        assert st2.armed and st2.queued == 0 and st2.transactions == 0 and st2.errors == 0, f"arm did not arm: {st2}"
        try:
            t.arm(4, b"\x01")
        except h.Unavailable as e:
            rec["second_arm"] = f"refused: {e}"
        else:
            raise AssertionError("a second arm while armed was accepted (fixture §4: one at a time, unavailable)")
        pending, bits, data = t.read_rx()
        rec["read_rx"] = [pending, bits, data.hex()]
        t.reset()
        st3 = t.status()
        rec["status_reset"] = dataclasses.asdict(st3)
        assert st3.state == 1 and st3.mode == 0 and st3.bit_order == 0 and not st3.armed and st3.queued == 0 \
            and st3.transactions == 0 and st3.errors == 0, f"status after reset: {st3}"
    finally:
        core.plan_release(hst, [t.fn])


# ---- 9. the target's console -------------------------------------------------------------------------------------------------

_MECHANISMS = reg.TARGET_CONSOLE.tlv["describe"]["mechanisms"]


def test_console(run: record.Run):
    spec = os.environ.get("OEP_HW_TARGET", "")
    if not spec:
        pytest.skip("no target wired (OEP_HW_TARGET=<name>[@swdio[,swclk]] says one is): the console needs a wire connection")
    hst = run.take()
    if not _listed(hst, console_mod.Console.NAME):
        pytest.skip("the probe does not list oep.target.console")
    name, _, pins_text = spec.partition("@")
    wires, pairs = _wire_for(hst, pins_text)
    wire = riscv.Wire(hst, wires[0])
    con = console_mod.Console(hst)
    rec = run.record("console", target=name, wire=wires[0])
    found = _scan_settled(wire, pairs, rec)
    assert found, "scan found no target"
    conn, status = wire.attach(halt=False, pins=found[0].pins)        # running: whatever it prints is what arrives
    rec.update(connection=conn, dmstatus=f"{status:#010x}")
    seen = bytearray()
    try:
        # A probe with several wires may have a console per wire (instances): the one that serves this connection is
        # the one whose open does not answer no_connection.
        for fn in core.find_all(hst, console_mod.Console.NAME):
            con.fn = fn
            decl = record.declared(hst, con.fn)
            mechanisms = list(decl["own"].get(_MECHANISMS, [b""])[0])
            mechanism = con.DMSEQ if con.DMSEQ in mechanisms or not mechanisms else mechanisms[0]
            rec.update(console_fn=fn, mechanisms=mechanisms, mechanism=mechanism)
            try:
                stream = con.open(conn, mechanism)
                break
            except h.NoConnection:
                rec.setdefault("no_connection_on", []).append(fn)
        else:
            pytest.fail(f"no oep.target.console serves connection {conn} of {wires[0]}")
        rec.update(stream=stream, existing=con.existing)
        backlog = con.read(con.FROM_OLDEST, 0, 1000)
        rec["backlog_bytes"] = len(backlog.data)
        io = console_mod.ConsoleIO(con)
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            seen += io.read(512)
            time.sleep(0.02)
        rec.update(bytes_seen=len(seen), lost=io.lost, marks=len(con.marks()),
                   streams=[dataclasses.asdict(s) for s in con.streams()], sample=bytes(seen[:64]).decode("ascii", "replace"))
        assert any(s.stream == stream and s.connection == conn and s.mechanism == mechanism for s in con.streams()), \
            "the streams list does not show the stream just opened"
        con.close()
    finally:
        try:
            wire.detach(conn)
        except h.OepError as e:
            rec["detach_error"] = str(e)


# ---- 10. port_speed (UART bridge) --------------------------------------------------------------------------------------------

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
    baseline: dict[tuple[str, int], float] = {}
    passed_rates: list[int] = []
    failed_rates: list[int] = []
    t0 = time.monotonic()
    for asked, result in zip(rates, linktest.matrix(hst, rates=rates, patterns=list(linktest.PATTERNS), inflight=inflight,
                                                    frames=frames, timeout=timeout)):
        row = {"asked": asked or "now", "rate": result.rate, "actual": result.actual, "switched": result.switched,
               "why": result.why, "cells": []}
        row_failing: list[str] = []
        for c in result.cells:
            cell = dataclasses.asdict(c)
            if not c.error:
                cell["error_rate"] = round(c.error_rate, 4)
                key = (c.pattern, c.inflight)
                if asked is None:
                    baseline[key] = c.error_rate          # the boot speed: the baseline for the same flow
                elif c.inflight == 1:
                    threshold = max(2 * baseline.get(key, 0.0), ERROR_RATE_FLOOR)
                    cell["threshold"] = round(threshold, 4)
                    if c.broken + c.lost >= MIN_BAD_FRAMES and c.error_rate > threshold:
                        row_failing.append(f"{c.pattern} x1: {c.error_rate * 100:.1f} % > {threshold * 100:.1f} % "
                                           f"(broken {c.broken}, lost {c.lost}, baseline {baseline.get(key, 0.0) * 100:.1f} %)")
            row["cells"].append(cell)
        if result.why and asked is None:
            failing.append(f"the speed in force: {result.why}")
        if asked is not None and result.switched:
            # the guide keeps the first candidate whose flows pass: a candidate that fails is recorded, the run fails only when
            # no candidate passes (a bridge's marginal rate is a fact about the bridge, not about the probe or the client)
            row["passed"] = not row_failing
            row["failing"] = row_failing
            (passed_rates if not row_failing else failed_rates).append(result.rate)
        rows.append(row)
        print("  " + result.text().replace("\n", "\n  "), flush=True)
    if failed_rates and not passed_rates:
        failing.append("no candidate passed: " + "; ".join(f"{r['rate']}: {', '.join(r['failing'])}" for r in rows if r.get("failing")))
    rec.update(results=rows, seconds=round(time.monotonic() - t0, 1), failing=failing, passed_rates=passed_rates,
               failed_rates=failed_rates,
               link_counters={"corrupt": hst.link.corrupt, "stale": hst.link.stale, "noise": hst.link.noise,
                              "retries": hst.link.retries, "resyncs": hst.link.resyncs})
    assert not failing, "; ".join(failing)


# ---- 11. the session --------------------------------------------------------------------------------------------------------

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
