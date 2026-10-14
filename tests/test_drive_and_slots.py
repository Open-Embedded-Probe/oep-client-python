"""The virtual bench against oep-spec a09fa45: gpio set without a strength and the 3-byte idle item (fixture §1,
probe.config §1); and, until the probe.config stage, the slot item and slot_state of oep-spec 0f455a0."""

import struct

import pytest

from oep_client import config, core, endpoint, virtual_bench, fixture, host as h, message as m, registry_legacy as reg


class Clock:
    t = 0

    def __call__(self):
        return self.t


def open_probe(probe=None):
    clock = Clock()
    ep = endpoint.Endpoint(probe or virtual_bench.p4_bench(), clock)
    hst = h.Host(lambda b: ep.handle(b, 1))
    hst.open(3000)
    return ep, hst, clock


def gpio_of(ep, hst, channels=(20, 21, 22)):
    fn = ep.fns["oep.fixture.gpio"]
    core.plan_apply(hst, [(fn, 1, ch) for ch in channels])
    return fixture.Gpio(hst, fn)


def raw_set(hst, g, pairs, tail=b""):
    body = bytes([len(pairs)]) + b"".join(struct.pack("<HB", ch, mode) for ch, mode in pairs) + tail
    return hst.call(g.fn, g.SET, body)


def raw_read(hst, g, channels):
    body = bytes([len(channels)]) + b"".join(struct.pack("<H", ch) for ch in channels)
    return hst.request(g.fn, g.READ, body, locked=False).payload


# ---- gpio set and the idle item: no strength selection (fixture §1, probe.config §1) -------------------------------

def test_set_has_no_drive_tlv_and_the_idle_item_is_3_bytes():
    """fixture §1: revision 1 defines no strength selection, so the old drive TLV (0x01) is an unknown tag - ignored plain,
    unsupported critical; probe.config §1: idle is channel(u16) mode(u8)."""
    ep, hst, _ = open_probe()
    g = gpio_of(ep, hst)
    assert raw_set(hst, g, [(20, 4)], m.tlv(0x01, b"\x00\x01")).ran and ep.gpio_modes[20] == 4
    with pytest.raises(h.Unsupported) as e:
        raw_set(hst, g, [(20, 3)], m.tlv(0x01, b"\x00\x01", critical=True))
    assert e.value.tag == 0x81 and ep.gpio_modes[20] == 4
    it = config.Idle(channel=7, mode="output-high")
    assert it.value() == bytes([7, 0, 4]) and config.decode(config.ITEM["idle"], it.value()) == it
    assert not any(t == 0x41 for t, _ in core.describe(hst, g.fn))      # no drive_levels


def test_set_ignores_an_unknown_tlv_silently_and_refuses_an_unknown_critical_one():
    """core §2.3: an unknown non-critical TLV is ignored (nothing listed); an unknown critical one is unsupported with
    the tag as received."""
    ep, hst, _ = open_probe()
    g = gpio_of(ep, hst)
    r = raw_set(hst, g, [(20, 4)], m.tlv(0x22, b"\x01"))
    assert r.ran and r.payload == b"" and ep.gpio_modes[20] == 4
    with pytest.raises(h.Unsupported) as e:
        raw_set(hst, g, [(20, 3)], m.tlv(0x22, b"\x01", critical=True))
    assert e.value.tag == 0xA2 and ep.gpio_modes[20] == 4


# ---- the slot item and slot_state ---------------------------------------------------------------------------------

V003_PAIR, NRST = (16, 0xFFFF), 23                                # esp32-v003: swio on 16, role 3 on 23


def v003(silent=True, reset_line=None):
    clock = Clock()
    clock.t = 7
    ep = endpoint.Endpoint(virtual_bench.esp32_v003(), clock)
    tg = ep.targets[(1, V003_PAIR)]
    tg.silent_until_reset, tg.reset_line = silent, reset_line
    return ep, clock, tg


def items(labels=((NRST, "v003.nrst"),), extra=(), retry_s=1):
    return [config.item(config.Slot(slot=0, wire_fn=1, pins=V003_PAIR, name="v003", attach="at-boot",
                                    retry_s=retry_s))] + \
        [config.item(config.Label(channel=ch, text=t)) for ch, t in labels] + [config.item(it) for it in extra]


def slot_state(ep):
    hst = h.Host(lambda b: ep.handle(b, 1))
    return config.ProbeConfig(hst).state().slots[0]


def test_slot_item_form_has_no_boot_reset_or_lock():
    """probe.config §1.1: slot wire_fn swdio swclk attach retry_ms max_speed_hz idle_clock mechanism name_len name."""
    s = config.Slot(slot=0, wire_fn=1, pins=(16, 0xFFFF), name="v003", attach="at-boot", retry_s=1.5,
                    max_speed=1_000_000)
    assert s.value() == struct.pack("<BHHHBIIBBB", 0, 1, 16, 0xFFFF, 1, 1500, 1_000_000, 0, 2, 4) + b"v003"
    assert config.decode(config.ITEM["slot"], s.value()) == s
    host_slot = config.Slot(slot=0, wire_fn=1, pins=(16, 0xFFFF), name="v003", retry_s=3)
    assert host_slot.value()[8:12] == b"\0\0\0\0"                 # retry_ms 0 on a host slot
    for gone in ("boot_reset", "lock"):
        with pytest.raises(TypeError):
            config.Slot(slot=0, wire_fn=1, pins=(16, 0xFFFF), name="v003", **{gone: None})


def test_slot_item_of_another_length_is_malformed_and_retry_ms_is_not_looked_at_on_a_host_slot():
    ep, hst, _ = open_probe(virtual_bench.esp32_v003())
    cfg = config.ProbeConfig(hst)
    v = config.Slot(slot=0, wire_fn=1, pins=V003_PAIR, name="v003").value()
    for bad in (v + b"\x55", v[:-1], v + b"\x01\x01\x02\x03\x04"):   # longer (an old boot_reset / lock) or shorter
        for critical in (False, True):
            with pytest.raises(h.Rejected, match="malformed"):
                cfg.set([m.tlv(config.ITEM["slot"], bad, critical=critical)])
    assert ep.slots == {}
    host_retry = v[:8] + struct.pack("<I", 1000) + v[12:]         # retry_ms on a host slot: accepted, not used
    cfg.set([m.tlv(config.ITEM["slot"], host_retry)])
    assert ep.slots[0].attach == 0 and ep.slots[0].retry_ms == 0


def test_slot_state_is_12_bytes_connected_or_absent():
    """probe.config §3.3: slot(u8) state(u8: 0 connected, 1 absent) connection(u16) last_try_at_ns(u64)."""
    assert reg.PROBE_CONFIG.enum["slot_state"] == {"connected": 0, "absent": 1}
    ep, clock, tg = v003(silent=True)
    ep.load_config(items())
    hst = h.Host(lambda b: ep.handle(b, 1))
    cfg = config.ProbeConfig(hst)
    p = hst.call(cfg.fn, cfg.STATE, bytes([0, 0]), locked=False).payload
    assert p[7] == 1 and p[8:20] == struct.pack("<BBHQ", 0, 1, 0, 7_000_000) and p[20:21] == b"\x00"
    assert p[21:] == m.tlv(config.STATE_TLV["wifi"], bytes([0, 0xFF, 0, 0]) + bytes(4))   # the wifi TLV: off
    tg.silent_until_reset = False
    clock.t = 1007
    ep.tick()
    st = slot_state(ep)
    assert st.state == "connected" and st.connection != 0 and st.last_try_at_ns == 1_007_000_000
    assert not hasattr(st, "reset_at_ns")


def test_an_at_boot_slot_never_resets_its_target():
    """probe.config §3.1: no retry with reset - a target silent until a reset stays absent, retried plainly."""
    ep, clock, tg = v003(silent=True)
    ep.load_config(items())
    assert slot_state(ep).state == "absent" and tg.silent_until_reset
    for t in (1007, 2007):
        clock.t = t
        ep.tick()
    assert slot_state(ep).state == "absent" and tg.silent_until_reset
    assert slot_state(ep).last_try_at_ns == 2_007_000_000
    for gone in ("slot_reset_log", "lock_taken", "reset_retried"):
        assert not hasattr(ep, gone)


def test_a_host_attach_with_reset_wakes_a_silent_target_and_scan_does_not_see_it():
    ep, hst, _ = open_probe(virtual_bench.esp32_v003())
    tg = ep.targets[(1, V003_PAIR)]
    tg.silent_until_reset = True
    W = reg.WIRE_RVSWD.op
    assert hst.call(1, W["scan"], struct.pack("<BHH", 1, *V003_PAIR)).payload[1] == 0   # scan: nothing found
    attach = struct.pack("<BHHI", 0, *V003_PAIR, 1_000_000)
    assert not hst.request(1, W["attach"], attach).succeeded
    reset = m.tlv(0x05, struct.pack("<HH", NRST, 20), critical=True)
    assert hst.call(1, W["attach"], attach + reset).succeeded and not tg.silent_until_reset


class FakeLink:
    def close(self):
        pass


def test_state_from_the_command_shows_the_last_try(capsys, monkeypatch):
    from oep_client import __main__ as cli
    ep, _, _ = v003()
    ep.load_config(items())
    hst = h.Host(lambda b: ep.handle(b, 1))
    hst.link = FakeLink()
    monkeypatch.setattr(cli.link, "open_host", lambda target: hst)
    assert cli.main(["config", "state", "x"]) == 0
    assert "slot 0: absent, tried at 0.007 s" in capsys.readouterr().out


def test_slot_and_idle_from_the_command(capsys, monkeypatch):
    from oep_client import __main__ as cli
    ep, hst, _ = open_probe(virtual_bench.esp32_v003())
    hst.link = FakeLink()
    hst.end()
    monkeypatch.setattr(cli.link, "open_host", lambda target: hst)
    assert cli.main(["config", "slot", "x", "--name", "v003", "--attach", "at-boot", "--retry", "2"]) == 0
    assert ep.slots[0].attach == 1 and ep.slots[0].retry_ms == 2000
    with pytest.raises(SystemExit):                               # gone (probe.config §1.1)
        cli.main(["config", "slot", "x", "--name", "v003", "--attach", "at-boot", "--boot-reset"])
    assert cli.main(["config", "idle", "x", "21", "output-low"]) == 0
    assert ep.parked[21] == 3


# ---- virtual_bench_serve's options for other hosts' tests ----------------------------------------------------------------

def serve(*argv):
    from oep_client import virtual_bench_serve
    ep = virtual_bench_serve.build(virtual_bench_serve.parse(["--tcp", "0", *argv]))
    return ep, h.Host(lambda b: ep.handle(b, 1))


def test_virtual_bench_serve_slot_with_a_silent_target_stays_absent():
    ep, hst = serve("--profile", "esp32-v003", "--slot", "v003", "--silent-until-reset", "0",
                    "--label", "23=v003.nrst")
    assert ep.slots[0].attach == 1 and ep.saved                   # saved, as if the probe booted with them
    st = config.ProbeConfig(hst).state().slots[0]
    assert st.state == "absent" and st.last_try_at_ns is not None
    assert ep.targets[(1, V003_PAIR)].silent_until_reset          # the probe resets nothing on its own


def test_virtual_bench_serve_has_no_boot_reset(capsys):
    from oep_client import virtual_bench_serve
    with pytest.raises(SystemExit):
        virtual_bench_serve.parse(["--slot", "v003", "--boot-reset"])


def test_virtual_bench_serve_label_wants_ch_eq_text(capsys):
    from oep_client import virtual_bench_serve
    with pytest.raises(SystemExit):
        virtual_bench_serve.parse(["--label", "nrst"])
    assert "CH=TEXT" in capsys.readouterr().err
    ep, _ = serve("--label", "0x14=t.nrst")
    assert ep.line_for("t", "nrst") == 0x14
