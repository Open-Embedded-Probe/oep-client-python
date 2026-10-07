"""oep-spec 0f455a0 against the fake: the gpio output strength (fixture §1.1: describe drive_levels, set's drive TLV
index(u8) level(u8), the effective strength; read carries no drive TLV), the idle item's drive (probe.config §1:
4 bytes, drive(u8)), the slot item without boot_reset / lock (§1.1) and slot_state's 12-byte form, connected / absent
(§3.3)."""

import struct

import pytest

from oep_client import config, core, endpoint, fake, fixture, host as h, message as m, registry as reg
from oep_client.fixture import Drive, DriveLevels

DRIVE = 0x01                                   # set's drive TLV
LEVELS = DriveLevels(2, (5, 10, 20, 40))


class Clock:
    t = 0

    def __call__(self):
        return self.t


def open_probe(probe=None):
    clock = Clock()
    ep = endpoint.Endpoint(probe or fake.p4_bench(), clock)
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


def drive_tlv(index, level, critical=False):
    return m.tlv(DRIVE, bytes([index, level]), critical=critical)


# ---- describe ---------------------------------------------------------------------------------------------------

def test_describe_declares_the_levels_and_a_probe_without_them_does_not():
    ep, hst, _ = open_probe()
    assert fixture.Gpio(hst, ep.fns["oep.fixture.gpio"]).drive_levels() == LEVELS
    assert ep.drive_levels == (2, [5, 10, 20, 40])
    ep2, hst2, _ = open_probe(fake.without_drive_levels(fake.p4_bench()))
    assert fixture.Gpio(hst2, ep2.fns["oep.fixture.gpio"]).drive_levels() is None
    assert ep2.drive_levels is None
    for profile in fake.PROFILES.values():                        # every example profile declares them
        ep3, hst3, _ = open_probe(profile())
        assert fixture.Gpio(hst3, ep3.fns["oep.fixture.gpio"]).drive_levels() == LEVELS


def test_drive_is_a_level_number_and_at_most_picks_one():
    """fixture §1.1: the strength is a level number (u8), 0xFF the default level; a host carries an mA between probes
    with drive_levels (`at_most`)."""
    assert [LEVELS.at_most(ma) for ma in (0, 4, 5, 15, 20, 39, 40, 1000)] == \
        [Drive.level(n) for n in (0, 0, 0, 1, 2, 2, 3, 3)]
    assert LEVELS.pick(3) == 3 and LEVELS.pick(Drive.level(4)) is None and LEVELS.pick(Drive.default()) == 2
    assert Drive.level(3).pack() == b"\x03" and Drive.default().pack() == b"\xff" and Drive.default().is_default
    assert Drive.unpack(b"\x01") == Drive.level(1)
    with pytest.raises(ValueError):
        Drive.level(0xFF)                                         # 0xFF is the default, not a level number
    with pytest.raises(ValueError):
        Drive(0x100).pack()
    assert not hasattr(Drive, "max_ma")


# ---- set ----------------------------------------------------------------------------------------------------------

def test_set_drive_per_element_and_read_has_no_drive_tlv():
    ep, hst, _ = open_probe()
    g = gpio_of(ep, hst, (20, 21, 22, 23))
    assert g.set([(20, g.OUTPUT_HIGH, Drive.level(0)), (21, g.OUTPUT_LOW, 1), (22, g.OUTPUT_HIGH),
                  (23, g.INPUT_PULLUP)]) is None
    # per element; the client sends it critical (a strength that did not take would drive the line otherwise)
    assert ep.requests[-1].payload.endswith(drive_tlv(0, 0, critical=True) + drive_tlv(1, 1, critical=True))
    assert ep.gpio_drive == {20: 0, 21: 1, 22: 2}                 # 22: the default level; 23 is not driven
    assert raw_read(hst, g, [20, 21, 22, 23]) == bytes([4, 1, 0, 1, 1])   # n(u8) n x level, no drive TLV (fixture §1)
    assert g.read([20, 21]) == [1, 0]
    g.set([(20, g.OUTPUT_HIGH, Drive.default())])                 # 0xFF: the default level
    assert ep.gpio_drive[20] == 2
    g.set([(20, g.OPEN_DRAIN_LOW)])                               # not mode 3 / 4: not driven so
    assert 20 not in ep.gpio_drive


def test_the_strength_is_kept_until_the_next_set_which_starts_again_from_idle_or_default():
    ep, hst, _ = open_probe()
    config.ProbeConfig(hst).set([config.Idle(channel=21, mode="output-low", drive=Drive.level(1))])
    g = gpio_of(ep, hst)
    g.set([(20, g.OUTPUT_HIGH, 3), (21, g.OUTPUT_HIGH, 3)])
    g.set([(22, g.OUTPUT_HIGH)])                                  # another channel's set: 20 / 21 keep theirs
    assert [ep.gpio_drive[ch] for ch in (20, 21, 22)] == [3, 3, 2]
    g.set([(20, g.OUTPUT_LOW), (21, g.OUTPUT_LOW)])               # set again without drive: not the last one
    assert [ep.gpio_drive[ch] for ch in (20, 21)] == [2, 1]       # the default; the idle item's drive


def test_take_and_release_use_the_idle_state_strength():
    ep, hst, _ = open_probe()
    cfg = config.ProbeConfig(hst)
    cfg.set([config.Idle(channel=20, mode="output-high", drive=Drive.level(1)),
             config.Idle(channel=21, mode="output-high"), config.Idle(channel=22, mode="pull-up")])
    assert ep.parked_drive == {20: 1, 21: 2}                      # free pins: driven at the idle's strength
    g = gpio_of(ep, hst)
    assert ep.gpio_drive.get(20) == 1 and ep.gpio_drive.get(21) == 2 and 22 not in ep.gpio_drive   # until the first set
    g.set([(20, g.OUTPUT_HIGH, 3)])
    core.plan_apply(hst, [(g.fn, 1, 20), (g.fn, 1, 23)])          # 20 in both plans: keeps its state and strength
    assert ep.gpio_drive[20] == 3
    core.plan_release(hst, [g.fn])
    assert ep.parked[20] == 4 and ep.parked_drive[20] == 1 and 20 not in ep.gpio_drive   # released: the idle's
    gpio_of(ep, hst, (20,))
    assert ep.gpio_drive[20] == 1


@pytest.mark.parametrize("critical", [False, True])
@pytest.mark.parametrize("tail", [
    drive_tlv(3, 0),                                              # index n or more
    drive_tlv(0, 0) + drive_tlv(0, 1),                            # the same index twice
    drive_tlv(1, 0),                                              # its element is not mode 3 / 4
    m.tlv(DRIVE, bytes([0])),                                     # not index level: another length
    m.tlv(DRIVE, bytes([0, 0, 0])),                               # ... longer too (core §2.3)
])
def test_set_drive_malformed_rejects_the_whole_request(tail, critical):
    ep, hst, _ = open_probe()
    g = gpio_of(ep, hst)
    if critical:                                                  # an implemented TLV: checked the same with bit 7
        tail = bytes([tail[0] | 0x80]) + tail[1:]
    with pytest.raises(h.Rejected, match="malformed"):
        raw_set(hst, g, [(20, 4), (21, 6), (22, 3)], tail)
    assert ep.gpio_modes.get(20) is None                          # nothing applied


@pytest.mark.parametrize("critical", [False, True])
@pytest.mark.parametrize("level", [4, 9, 0xFE])
def test_set_drive_past_the_levels_is_unsupported_with_the_tag_as_received(level, critical):
    """fixture §1.1: a level the probe lacks (0xFF aside) is rejected unsupported, the tag as received, with or
    without bit 7 (no ignoring)."""
    ep, hst, _ = open_probe()
    g = gpio_of(ep, hst)
    with pytest.raises(h.Unsupported) as e:
        raw_set(hst, g, [(20, 4), (21, 3)], drive_tlv(0, 1) + drive_tlv(1, level, critical=critical))
    assert e.value.tag == DRIVE | (0x80 if critical else 0)
    assert ep.gpio_modes.get(20) is None and 20 not in ep.gpio_drive   # nothing applied
    with pytest.raises(h.Unsupported):                            # the client's own set: the same
        g.set([(20, g.OUTPUT_HIGH, level)])
    assert raw_set(hst, g, [(20, 4)], drive_tlv(0, 0xFF, critical=critical)).ran   # 0xFF: the default level
    assert ep.gpio_drive[20] == 2


def test_set_with_a_malformed_and_an_unsupported_drive_gets_either():
    """core §4.3: every check before any change, and any one reason that applies."""
    ep, hst, _ = open_probe()
    g = gpio_of(ep, hst)
    with pytest.raises(h.Rejected, match="malformed|unsupported"):
        raw_set(hst, g, [(20, 4), (21, 4)], drive_tlv(0, 9) + drive_tlv(5, 0))
    assert ep.gpio_modes.get(20) is None


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


def test_a_probe_without_drive_levels_refuses_every_drive():
    """fixture §1.1: on a probe without drive_levels any drive (0xFF too) is rejected unsupported, the tag as
    received; a set without one works."""
    ep, hst, _ = open_probe(fake.without_drive_levels(fake.p4_bench()))
    g = gpio_of(ep, hst)
    for level in (0, 1, 0xFF):
        for critical in (False, True):
            with pytest.raises(h.Unsupported) as e:
                raw_set(hst, g, [(20, 4)], drive_tlv(0, level, critical=critical))
            assert e.value.tag == DRIVE | (0x80 if critical else 0)
    with pytest.raises(h.Unsupported):
        g.set([(20, g.OUTPUT_HIGH, 1)])
    assert ep.gpio_modes.get(20) is None                          # nothing applied
    with pytest.raises(h.Rejected, match="malformed|unsupported"):   # a malformed-looking one: either reason applies
        raw_set(hst, g, [(20, 6)], drive_tlv(7, 0))
    g.set([(20, g.OUTPUT_HIGH), (21, g.OUTPUT_LOW)])
    assert ep.gpio_modes[20] == 4 and ep.gpio_modes[21] == 3 and ep.gpio_drive == {}
    assert raw_read(hst, g, [20, 21]) == bytes([2, 1, 0])


# ---- the idle item's drive ----------------------------------------------------------------------------------------

def test_idle_item_is_4_bytes_and_decodes():
    """probe.config §1: channel(u16) mode(u8) drive(u8), 0xFF the default level."""
    it = config.Idle(channel=7, mode="output-high", drive=Drive.level(1))
    assert it.value() == bytes([7, 0, 4, 1])
    assert config.decode(config.ITEM["idle"], it.value()) == it
    assert config.Idle(channel=7, mode="output-low", drive=2).value() == bytes([7, 0, 3, 2])
    assert config.Idle(channel=7, mode="pull-up").value() == bytes([7, 0, 1, 0xFF])
    assert config.Idle(channel=7, mode="output-high").value() == bytes([7, 0, 4, 0xFF])
    assert config.decode(config.ITEM["idle"], bytes([7, 0, 4, 0xFF])).drive is None   # the default level
    assert config.Idle(channel=7, mode="output-high", drive=Drive.default()).value() == bytes([7, 0, 4, 0xFF])
    with pytest.raises(ValueError, match="output-low / output-high"):
        config.Idle(channel=7, mode="pull-up", drive=1).value()


def _idle(ch, *rest, critical=False):
    return m.tlv(config.ITEM["idle"], struct.pack("<H", ch) + bytes(rest), critical=critical)


@pytest.mark.parametrize("critical", [False, True])
@pytest.mark.parametrize("value, reason", [
    ((4,), "malformed"),                                          # 3 bytes: the idle is 4 (probe.config §1)
    ((4, 0, 0), "malformed"),                                     # 5 bytes: longer than its one form, too
    ((4, 1, 0xAA, 0), "malformed"),
    ((4, 4), "unsupported"),                                      # a level = the number of levels
    ((3, 9), "unsupported"),
    ((4, 0xFE), "unsupported"),
])
def test_idle_drive_refusals(value, reason, critical):
    ep, hst, _ = open_probe()
    with pytest.raises(h.Rejected, match=reason) as e:
        config.ProbeConfig(hst).set([_idle(20, *value, critical=critical)])
    if reason == "unsupported":                                   # the item's tag as received
        assert e.value.tag == config.ITEM["idle"] | (0x80 if critical else 0)
    assert (config.ITEM["idle"], 20) not in ep.config


def test_idle_drive_accepted_forms():
    ep, hst, _ = open_probe()
    cfg = config.ProbeConfig(hst)
    cfg.set([_idle(20, 4, 3), _idle(21, 3, 0), _idle(22, 4, 0xFF), _idle(26, 1, 5), _idle(27, 2, 0xFF)])
    assert ep.parked_drive == {20: 3, 21: 0, 22: fake.DRIVE_DEFAULT}
    assert ep.parked[26] == 1 and ep.parked[27] == 2               # an input idle: drive not looked at
    assert 26 not in ep.parked_drive and 27 not in ep.parked_drive


def test_a_probe_without_drive_levels_refuses_an_output_idle_drive():
    """probe.config §1: on a probe without drive_levels a drive other than 0xFF on mode 3 / 4 is rejected unsupported;
    an input mode's drive is not looked at."""
    ep, hst, _ = open_probe(fake.without_drive_levels(fake.p4_bench()))
    cfg = config.ProbeConfig(hst)
    with pytest.raises(h.Unsupported) as e:
        cfg.set([config.Idle(channel=20, mode="output-high", drive=Drive.level(0))])
    assert e.value.tag == config.ITEM["idle"]
    cfg.set([config.Idle(channel=20, mode="output-high"), _idle(21, 1, 3)])
    assert ep.parked[20] == 4 and ep.parked[21] == 1 and ep.parked_drive == {}
    with pytest.raises(h.Rejected, match="malformed"):            # the form rules still hold
        cfg.set([_idle(22, 4, 0xFF, 0)])


def test_a_probe_without_gpio_refuses_an_output_idle_drive():
    bench = fake.p4_bench()
    probe = fake.FakeProbe(bench.label, bench.max_frame, [o for o in bench.offered if o.name != "oep.fixture.gpio"],
                           own_channels=bench.own_channels)
    ep, hst, _ = open_probe(probe)
    cfg = config.ProbeConfig(hst)
    with pytest.raises(h.Unsupported):                            # no drive_levels without oep.fixture.gpio
        cfg.set([config.Idle(channel=20, mode="output-low", drive=Drive.level(1))])
    cfg.set([config.Idle(channel=20, mode="output-low")])
    assert ep.parked[20] == 3


# ---- the slot item and slot_state ---------------------------------------------------------------------------------

V003_PAIR, NRST = (16, 0xFFFF), 23                                # esp32-v003: swio on 16, role 3 on 23


def v003(silent=True, reset_line=None):
    clock = Clock()
    clock.t = 7
    ep = endpoint.Endpoint(fake.esp32_v003(), clock)
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
    ep, hst, _ = open_probe(fake.esp32_v003())
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
    assert p[7] == 1 and p[8:20] == struct.pack("<BBHQ", 0, 1, 0, 7_000_000) and p[20:] == b"\x00"
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
    ep, hst, _ = open_probe(fake.esp32_v003())
    tg = ep.targets[(1, V003_PAIR)]
    tg.silent_until_reset = True
    assert hst.call(1, 0x01, struct.pack("<BHH", 1, *V003_PAIR)).payload[1] == 0   # scan: nothing found
    attach = bytes([0]) + m.tlv(0x01, struct.pack("<I", 1_000_000), critical=True)
    assert not hst.request(1, 0x02, attach).succeeded
    reset = m.tlv(0x05, struct.pack("<HH", NRST, 20), critical=True)
    assert hst.call(1, 0x02, attach + reset).succeeded and not tg.silent_until_reset


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
    ep, hst, _ = open_probe(fake.esp32_v003())
    hst.link = FakeLink()
    hst.end()
    monkeypatch.setattr(cli.link, "open_host", lambda target: hst)
    assert cli.main(["config", "slot", "x", "--name", "v003", "--attach", "at-boot", "--retry", "2"]) == 0
    assert ep.slots[0].attach == 1 and ep.slots[0].retry_ms == 2000
    with pytest.raises(SystemExit):                               # gone (probe.config §1.1)
        cli.main(["config", "slot", "x", "--name", "v003", "--attach", "at-boot", "--boot-reset"])
    assert cli.main(["config", "idle", "x", "21", "output-high", "--drive-ma", "10"]) == 0
    assert ep.parked_drive[21] == 1
    assert cli.main(["config", "idle", "x", "21", "output-low", "--drive-level", "3"]) == 0
    assert ep.parked[21] == 3 and ep.parked_drive[21] == 3
    with pytest.raises(SystemExit, match="output-low / output-high"):
        cli.main(["config", "idle", "x", "21", "pull-up", "--drive-level", "1"])


# ---- fake_serve's options for other hosts' tests ----------------------------------------------------------------

def serve(*argv):
    from oep_client import fake_serve
    ep = fake_serve.build(fake_serve.parse(["--tcp", "0", *argv]))
    return ep, h.Host(lambda b: ep.handle(b, 1))


def test_fake_serve_no_drive_levels():
    ep, hst = serve("--profile", "p4-bench", "--no-drive-levels")
    assert ep.drive_levels is None
    hst.open(3000)
    g = gpio_of(ep, hst)
    assert g.drive_levels() is None
    with pytest.raises(h.Unsupported):
        g.set([(20, g.OUTPUT_HIGH, 1)])
    g.set([(20, g.OUTPUT_HIGH)])
    assert raw_read(hst, g, [20]) == bytes([1, 1])
    ep, hst = serve("--profile", "p4-bench")                     # without the option: the profile's levels
    assert fixture.Gpio(hst, ep.fns["oep.fixture.gpio"]).drive_levels() == LEVELS


def test_fake_serve_slot_with_a_silent_target_stays_absent():
    ep, hst = serve("--profile", "esp32-v003", "--slot", "v003", "--silent-until-reset", "0",
                    "--label", "23=v003.nrst")
    assert ep.slots[0].attach == 1 and ep.saved                   # saved, as if the probe booted with them
    st = config.ProbeConfig(hst).state().slots[0]
    assert st.state == "absent" and st.last_try_at_ns is not None
    assert ep.targets[(1, V003_PAIR)].silent_until_reset          # the probe resets nothing on its own


def test_fake_serve_has_no_boot_reset(capsys):
    from oep_client import fake_serve
    with pytest.raises(SystemExit):
        fake_serve.parse(["--slot", "v003", "--boot-reset"])


def test_fake_serve_label_wants_ch_eq_text(capsys):
    from oep_client import fake_serve
    with pytest.raises(SystemExit):
        fake_serve.parse(["--label", "nrst"])
    assert "CH=TEXT" in capsys.readouterr().err
    ep, _ = serve("--label", "0x14=t.nrst")
    assert ep.line_for("t", "nrst") == 0x14
