"""oep-spec ee562c3 against the fake: the gpio output strength (fixture §1.1: describe drive_levels, set's drive TLV,
the effective strength, read's drive TLV), the idle item's drive (probe.config §1), the slot's boot_reset (§1.1), the
retry with reset (§3.1) and slot_state's reset_at_ns (§3.3)."""

import struct

import pytest

from oep_client import catalog, config, core, endpoint, fake, fixture, host as h, message as m
from oep_client.fixture import Drive, DriveLevels

DRIVE = 0x01                                   # set's drive TLV / read's answer TLV
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


def drive_tlv(index, kind, value, critical=False):
    return m.tlv(DRIVE, struct.pack("<BBH", index, kind, value), critical=critical)


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


def test_drive_levels_pick():
    assert [LEVELS.pick(Drive.max_ma(ma)) for ma in (0, 4, 5, 15, 20, 39, 40, 1000)] == [0, 0, 0, 1, 2, 2, 3, 3]
    assert LEVELS.pick(3) == 3 and LEVELS.pick(Drive.level(4)) is None


# ---- set and read -----------------------------------------------------------------------------------------------

def test_set_drive_per_element_and_read_answers_the_levels():
    ep, hst, _ = open_probe()
    g = gpio_of(ep, hst, (20, 21, 22, 23))
    assert g.set([(20, g.OUTPUT_HIGH, Drive.level(0)), (21, g.OUTPUT_LOW, Drive.max_ma(15)), (22, g.OUTPUT_HIGH),
                  (23, g.INPUT_PULLUP)]) == []
    assert ep.requests[-1].payload.endswith(drive_tlv(0, 0, 0) + drive_tlv(1, 1, 15))   # non-critical, per element
    st = g.read_state([20, 21, 22, 23])
    assert st.levels == [1, 0, 1, 1]
    assert st.drive == [0, 1, 2, None]                            # 22 the default level; 23 not driven: 0xFF
    g.set([(20, g.OUTPUT_HIGH, 3)])                               # an int is a level number
    assert g.read_state([20]).drive == [3]
    g.set([(20, g.OPEN_DRAIN_LOW)])                               # not mode 3 / 4: not driven so
    assert g.read_state([20]).drive == [None]
    assert g.read([20, 21]) == [0, 0]                             # read() stays the levels alone


def test_the_strength_is_kept_until_the_next_set_which_starts_again_from_idle_or_default():
    ep, hst, _ = open_probe()
    config.ProbeConfig(hst).set([config.Idle(channel=21, mode="output-low", drive=Drive.level(1))])
    g = gpio_of(ep, hst)
    g.set([(20, g.OUTPUT_HIGH, 3), (21, g.OUTPUT_HIGH, 3)])
    g.set([(22, g.OUTPUT_HIGH)])                                  # another channel's set: 20 / 21 keep theirs
    assert g.read_state([20, 21, 22]).drive == [3, 3, 2]
    g.set([(20, g.OUTPUT_LOW), (21, g.OUTPUT_LOW)])               # set again without drive: not the last one
    assert g.read_state([20, 21]).drive == [2, 1]                 # the default; the idle item's drive


def test_take_and_release_use_the_idle_state_strength():
    ep, hst, _ = open_probe()
    cfg = config.ProbeConfig(hst)
    cfg.set([config.Idle(channel=20, mode="output-high", drive=Drive.max_ma(10)),
             config.Idle(channel=21, mode="output-high"), config.Idle(channel=22, mode="pull-up")])
    assert ep.parked_drive == {20: 1, 21: 2}                      # free pins: driven at the idle's strength
    g = gpio_of(ep, hst)
    assert g.read_state([20, 21, 22]).drive == [1, 2, None]       # taken: the idle state until the first set
    g.set([(20, g.OUTPUT_HIGH, 3)])
    core.plan_apply(hst, [(g.fn, 1, 20), (g.fn, 1, 23)])          # 20 in both plans: keeps its state and strength
    assert g.read_state([20]).drive == [3]
    core.plan_release(hst, [g.fn])
    assert ep.parked[20] == 4 and ep.parked_drive[20] == 1 and 20 not in ep.gpio_drive   # released: the idle's
    gpio_of(ep, hst, (20,))
    assert g.read_state([20]).drive == [1]


@pytest.mark.parametrize("tail", [
    drive_tlv(3, 0, 0),                                           # index n or more
    drive_tlv(0, 0, 0) + drive_tlv(0, 0, 1),                      # the same index twice
    drive_tlv(1, 0, 0),                                           # its element is not mode 3 / 4
    m.tlv(DRIVE, bytes([0, 0, 0])),                               # not index kind value
    drive_tlv(0, 0, 9) + drive_tlv(5, 0, 0),                      # malformed comes first, even after an ignored one
    drive_tlv(0, 0, 9, critical=True) + drive_tlv(5, 0, 0),       # ... and before a critical one's unsupported
])
def test_set_drive_malformed_rejects_the_whole_request(tail):
    ep, hst, _ = open_probe()
    g = gpio_of(ep, hst)
    with pytest.raises(h.Rejected, match="malformed"):
        raw_set(hst, g, [(20, 4), (21, 6), (22, 3)], tail)
    assert ep.gpio_modes.get(20) is None                          # nothing applied


def test_set_drive_past_the_levels_is_ignored_and_listed():
    ep, hst, _ = open_probe()
    config.ProbeConfig(hst).set([config.Idle(channel=21, mode="output-low", drive=0)])
    g = gpio_of(ep, hst)
    assert g.set([(20, g.OUTPUT_HIGH, 4), (21, g.OUTPUT_HIGH, Drive.level(9)), (22, g.OUTPUT_HIGH, 1)]) == [DRIVE, DRIVE]
    assert g.read_state([20, 21, 22]).drive == [2, 0, 1]          # ignored: as if none (default / the idle's)
    with pytest.raises(h.Rejected, match="unsupported"):          # critical: cannot be ignored (core §2.3)
        raw_set(hst, g, [(20, 4)], drive_tlv(0, 0, 4, critical=True))
    assert raw_set(hst, g, [(20, 4)], drive_tlv(0, 0, 3, critical=True)).ran   # a critical one it can honour


def test_set_lists_unknown_tlvs_as_ignored():
    ep, hst, _ = open_probe()
    g = gpio_of(ep, hst)
    r = raw_set(hst, g, [(20, 4)], m.tlv(0x22, b"\x01"))
    assert m.Reader(r.payload).tail().ignored == [0x22]


def test_a_probe_without_drive_levels_ignores_every_drive():
    ep, hst, _ = open_probe(fake.without_drive_levels(fake.p4_bench()))
    g = gpio_of(ep, hst)
    assert g.set([(20, g.OUTPUT_HIGH, 1), (21, g.OUTPUT_LOW, Drive.max_ma(5))]) == [DRIVE, DRIVE]
    assert ep.gpio_modes[20] == 4 and ep.gpio_modes[21] == 3      # the modes apply
    r = raw_set(hst, g, [(20, 6)], drive_tlv(7, 9, 0))            # even a malformed-looking one: ignored, not checked
    assert m.Reader(r.payload).tail().ignored == [DRIVE]
    with pytest.raises(h.Rejected, match="unsupported"):
        raw_set(hst, g, [(20, 4)], drive_tlv(0, 0, 0, critical=True))   # an unknown critical tag
    st = g.read_state([20, 21])
    assert st.levels == [1, 0] and st.drive is None               # read carries no drive TLV (20: released, 6)
    assert hst.request(g.fn, g.READ, bytes([1, 21, 0]), locked=False).payload == bytes([1, 0])


# ---- the idle item's drive --------------------------------------------------------------------------------------

def test_idle_item_drive_encodes_and_decodes():
    it = config.Idle(channel=7, mode="output-high", drive=Drive.max_ma(10))
    assert it.value() == bytes([7, 0, 4, 1, 10, 0])
    assert config.decode(config.ITEM["idle"], it.value()) == it
    assert config.Idle(channel=7, mode="output-low", drive=2).value() == bytes([7, 0, 3, 0, 2, 0])
    assert config.decode(config.ITEM["idle"], bytes([7, 0, 4])).drive is None
    with pytest.raises(ValueError, match="output-low / output-high"):
        config.Idle(channel=7, mode="pull-up", drive=1).value()
    with pytest.raises(ValueError):
        config.Idle(channel=7, mode="output-high", drive=Drive(2, 0)).value()


def _idle(ch, *rest):
    return m.tlv(config.ITEM["idle"], struct.pack("<H", ch) + bytes(rest))


@pytest.mark.parametrize("value, reason", [
    ((4, 0), "malformed"),                                        # 4 bytes
    ((4, 0, 1), "malformed"),                                     # 5 bytes
    ((4, 2, 0, 0), "unsupported"),                                # drive_kind undefined: a later revision's (C-02)
    ((1, 0, 0, 0), "malformed"),                                  # a drive on a mode other than 3 / 4
    ((0, 1, 10, 0), "malformed"),
    ((4, 0, 4, 0), "unsupported"),                                # level number = the number of levels
])
def test_idle_drive_refusals(value, reason):
    ep, hst, _ = open_probe()
    with pytest.raises(h.Rejected, match=reason):
        config.ProbeConfig(hst).set([_idle(20, *value)])


def test_idle_drive_accepted_forms():
    ep, hst, _ = open_probe()
    cfg = config.ProbeConfig(hst)
    cfg.set([_idle(20, 4, 0, 3, 0), _idle(21, 3, 1, 1, 0), _idle(22, 4, 1, 0xFF, 0xFF), _idle(23, 4, 0, 1, 0, 0xAA)])
    assert ep.parked_drive == {20: 3, 21: 0, 22: 3, 23: 1}        # a ceiling below every level: level 0; a tail skipped
    assert ep.config[(config.ITEM["idle"], 23)] == bytes([23, 0, 4, 0, 1, 0, 0xAA])   # ... and kept, not cut (§2)


def test_a_probe_without_drive_levels_keeps_the_idle_drive_and_drives_at_its_default():
    ep, hst, _ = open_probe(fake.without_drive_levels(fake.p4_bench()))
    cfg = config.ProbeConfig(hst)
    cfg.set([config.Idle(channel=20, mode="output-high", drive=Drive.level(9))])   # no levels: not checked
    assert config.Idle(channel=20, mode="output-high", drive=Drive.level(9)) in cfg.items()
    assert ep.parked[20] == 4 and ep.parked_drive == {}
    with pytest.raises(h.Rejected, match="malformed"):            # the form rules still hold
        cfg.set([_idle(21, 4, 0, 0)])
    with pytest.raises(h.Rejected, match="unsupported"):          # an undefined drive_kind (C-02)
        cfg.set([_idle(21, 4, 2, 0, 0)])


def test_a_probe_without_gpio_keeps_the_idle_drive():
    bench = fake.p4_bench()
    probe = fake.FakeProbe(bench.label, bench.max_frame, [o for o in bench.offered if o.name != "oep.fixture.gpio"])
    ep, hst, _ = open_probe(probe)
    config.ProbeConfig(hst).set([config.Idle(channel=20, mode="output-low", drive=Drive.level(7))])
    assert ep.parked[20] == 3


# ---- slot boot_reset --------------------------------------------------------------------------------------------

def test_slot_boot_reset_encodes_after_the_lock_and_decodes():
    s = config.Slot(slot=0, wire_fn=1, pins=(16, 0xFFFF), name="v003", attach="at-boot", boot_reset=True)
    assert s.value().endswith(b"\x00\x01")                        # lock_len 0, boot_reset 1
    assert config.decode(config.ITEM["slot"], s.value()) == s
    locked = config.Slot(slot=0, wire_fn=1, pins=(16, 0xFFFF), name="v003", attach="at-boot", boot_reset=True,
                         lock=(1, b"\xff" * 4, b"\x01\x02\x03\x04"))
    assert locked.value().endswith(b"\x01\x02\x03\x04\x01") and config.decode(config.ITEM["slot"], locked.value()) == locked
    plain = config.Slot(slot=0, wire_fn=1, pins=(16, 0xFFFF), name="v003", attach="at-boot")
    assert plain.value().endswith(b"v003\x00")                   # not placed: 0
    assert config.decode(config.ITEM["slot"], plain.value() + b"\x00").boot_reset is False
    with pytest.raises(ValueError, match="at-boot"):
        config.Slot(slot=0, wire_fn=1, pins=(16, 0xFFFF), name="v003", boot_reset=True).value()


def _slot(attach, boot_reset, extra=b""):
    v = config.Slot(slot=0, wire_fn=1, pins=(16, 0xFFFF), name="v003", attach="at-boot").value()
    v = v[:7] + bytes([attach]) + v[8:]
    return m.tlv(config.ITEM["slot"], v + (bytes([boot_reset]) if boot_reset is not None else b"") + extra)


def test_slot_boot_reset_malformed_rules():
    ep, hst, _ = open_probe(fake.esp32_v003())
    cfg = config.ProbeConfig(hst)
    with pytest.raises(h.Rejected, match="malformed"):
        cfg.set([_slot(1, 2)])                                    # 2 or more
    with pytest.raises(h.Rejected, match="malformed"):
        cfg.set([_slot(0, 1)])                                    # 1 on a host slot (its retry_ms is 0 already)
    cfg.set([_slot(0, 0)])                                        # 0 placed: fine on a host slot
    cfg.set([_slot(1, 1, b"\x55\x66")])                           # later fields after it: skipped
    assert ep.slots[0].boot_reset == 1


# ---- the retry with reset (probe.config §3.1) -------------------------------------------------------------------

V003_PAIR, NRST = (16, 0xFFFF), 23                                # esp32-v003: swio on 16, role 3 on 23


def v003(silent=True, reset_line=None):
    clock = Clock()
    clock.t = 7
    ep = endpoint.Endpoint(fake.esp32_v003(), clock)
    tg = ep.targets[(1, V003_PAIR)]
    tg.silent_until_reset, tg.reset_line = silent, reset_line
    return ep, clock, tg


def items(boot_reset=True, labels=((NRST, "v003.nrst"),), extra=(), retry_s=1):
    return [config.item(config.Slot(slot=0, wire_fn=1, pins=V003_PAIR, name="v003", attach="at-boot",
                                    retry_s=retry_s, boot_reset=boot_reset))] + \
        [config.item(config.Label(channel=ch, text=t)) for ch, t in labels] + [config.item(it) for it in extra]


def slot_state(ep):
    hst = h.Host(lambda b: ep.handle(b, 1))
    return config.ProbeConfig(hst).state().slots[0]


def test_retry_with_reset_attaches_a_target_silent_until_reset():
    ep, clock, tg = v003()
    host_resets = []
    ep._host_reset = lambda *a: host_resets.append(a)
    ep.load_config(items())
    assert ep.slot_reset_log == [(0, NRST, 20)]                   # hold_ms = slot_retry_reset_hold_ms
    st = slot_state(ep)
    assert st.state == "connected" and st.reset_at_ns == 7_000_000   # when it started pulling (the probe's clock)
    assert host_resets == []                                      # the probe's own attach: the bind selection stays
    assert not tg.halted                                          # method 0: running


def test_retry_with_reset_found_by_the_bare_name_any_case():
    ep, _, _ = v003()
    ep.load_config(items(labels=((NRST, "NRST"),)))               # one slot item: the bare name, ASCII case ignored
    assert slot_state(ep).state == "connected"


@pytest.mark.parametrize("kw", [
    dict(boot_reset=False),                                       # not asked for
    dict(labels=((NRST, "v003.nrst"), (22, "V003.NRST"))),        # two at one step: no line
    dict(labels=((22, "v003.nrst"),)),                            # not a reset channel of the wire (role 3)
])
def test_no_retry_with_reset(kw):
    ep, _, _ = v003()
    ep.load_config(items(**kw))
    assert ep.slot_reset_log == []
    st = slot_state(ep)
    assert st.state == "absent" and st.reset_at_ns is None


def test_retry_with_reset_finds_the_firmware_label_as_step_c():
    """PC-1 (probe.config §1.3 step (c)): with no settings label, the firmware's fixed label NRST (describe 0x46,
    channel 23 on this profile) is the slot's line - only while the settings hold at most one slot item."""
    ep, _, _ = v003()
    ep.load_config(items(labels=()))
    assert ep.slot_reset_log == [(0, NRST, 20)] and slot_state(ep).state == "connected"
    assert ep.line_for("v003", "nrst") == NRST
    ep, _, _ = v003()
    ep.load_config(items(labels=((22, "NRST"),)))                 # step (b) finds the settings' one first
    assert ep.line_for("v003", "nrst") == 22
    two = config.Slot(slot=1, wire_fn=1, pins=V003_PAIR, name="other")
    assert ep.line_for("v003", "nrst") == 22
    ep.config[(config.ITEM["slot"], 1)] = config.item(two)[2:]   # two slot items: (b) and (c) are not searched
    assert ep.line_for("v003", "nrst") is None


def test_no_retry_with_reset_through_a_disabled_or_planned_line():
    ep, _, _ = v003()
    ep.load_config(items(extra=(config.Disable(channel=NRST),)))
    assert ep.slot_reset_log == [] and slot_state(ep).state == "absent"
    ep, _, _ = v003()
    gpio_fn = ep.fns["oep.fixture.gpio"]
    ep.load_config(items(extra=(config.Plan(fn=gpio_fn, role=1, channel=NRST),)))
    assert ep.slot_reset_log == [] and slot_state(ep).state == "absent"


def test_no_retry_with_reset_after_a_success_or_a_lock_mismatch():
    ep, _, tg = v003(silent=False)
    ep.load_config(items())
    assert ep.slot_reset_log == [] and slot_state(ep).state == "connected"
    ep, _, tg = v003(silent=False)
    tg.target_id = 0x11111111
    slot = config.Slot(slot=0, wire_fn=1, pins=V003_PAIR, name="v003", attach="at-boot", boot_reset=True,
                       lock=(1, b"\xff" * 4, struct.pack("<I", 0x22222222)))
    ep.load_config([config.item(slot), config.item(config.Label(channel=NRST, text="v003.nrst"))])
    assert ep.slot_reset_log == [] and slot_state(ep).state == "lock-mismatch"


def test_retry_with_reset_once_per_boot_then_plain_retries():
    ep, clock, tg = v003(reset_line=4)                            # its reset is elsewhere: the retry does not help
    ep.load_config(items())
    assert ep.slot_reset_log == [(0, NRST, 20)] and slot_state(ep).state == "absent"
    for t in (1007, 2007, 3007):                                  # retry_ms 1000: plain retries, no second reset
        clock.t = t
        ep.tick()
    assert len(ep.slot_reset_log) == 1 and slot_state(ep).reset_at_ns == 7_000_000
    tg.silent_until_reset = False
    clock.t = 4007
    ep.tick()
    assert slot_state(ep).state == "connected" and len(ep.slot_reset_log) == 1
    tg.silent_until_reset = True
    ep.reboot(0x5678)                                             # a new boot: once more
    assert len(ep.slot_reset_log) == 1 and ep.slot_reset_log == [(0, NRST, 20)]   # the log is the boot's
    assert slot_state(ep).reset_at_ns == 4_007_000_000


def test_no_retry_with_reset_once_a_session_took_the_lock():
    ep, clock, tg = v003(silent=False)
    ep.load_config(items())
    assert slot_state(ep).state == "connected"
    hst = h.Host(lambda b: ep.handle(b, 1))
    hst.open(3000)
    hst.end()                                                     # taken and released: still none this boot
    ep.lose_connections()
    tg.silent_until_reset = True
    clock.t = 2000
    ep.tick()
    assert ep.slot_reset_log == [] and slot_state(ep).state == "absent"


def test_retry_with_reset_before_the_lock_after_a_lost_target():
    ep, clock, tg = v003(silent=False)
    ep.load_config(items())
    ep.lose_connections()
    tg.silent_until_reset = True
    clock.t = 2000
    ep.tick()                                                     # no session yet: the at-boot retry may reset once
    assert ep.slot_reset_log == [(0, NRST, 20)] and slot_state(ep).state == "connected"


def test_a_host_attach_with_reset_wakes_a_silent_target_and_scan_does_not_see_it():
    ep, hst, _ = open_probe(fake.esp32_v003())
    tg = ep.targets[(1, V003_PAIR)]
    tg.silent_until_reset = True
    assert hst.call(1, 0x01, struct.pack("<BHH", 1, *V003_PAIR)).payload[1] == 0   # scan: nothing found
    attach = bytes([0]) + m.tlv(0x01, struct.pack("<I", 1_000_000), critical=True)
    assert not hst.request(1, 0x02, attach).succeeded
    reset = m.tlv(0x05, struct.pack("<HH", NRST, 20), critical=True)
    assert hst.call(1, 0x02, attach + reset).succeeded and not tg.silent_until_reset


def test_state_from_the_command_shows_the_retry(capsys, monkeypatch):
    from oep_client import __main__ as cli
    ep, _, _ = v003()
    ep.load_config(items())
    hst = h.Host(lambda b: ep.handle(b, 1))

    class FakeLink:
        def close(self):
            pass
    hst.link = FakeLink()
    monkeypatch.setattr(cli.link, "open_host", lambda target: hst)
    assert cli.main(["config", "state", "x"]) == 0
    assert "reset retried at 0.007 s" in capsys.readouterr().out


def test_slot_and_idle_from_the_command(capsys, monkeypatch):
    from oep_client import __main__ as cli
    ep, hst, _ = open_probe(fake.esp32_v003())

    class FakeLink:
        def close(self):
            pass
    hst.link = FakeLink()
    hst.end()
    monkeypatch.setattr(cli.link, "open_host", lambda target: hst)
    assert cli.main(["config", "slot", "x", "--name", "v003", "--attach", "at-boot", "--boot-reset"]) == 0
    assert ep.slots[0].boot_reset == 1
    with pytest.raises(SystemExit, match="at-boot"):
        cli.main(["config", "slot", "x", "--name", "v003", "--boot-reset"])
    assert cli.main(["config", "idle", "x", "21", "output-high", "--drive-ma", "10"]) == 0
    assert ep.parked_drive[21] == 1
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
    assert g.set([(20, g.OUTPUT_HIGH, 1)]) == [DRIVE]              # the drive: ignored and listed
    assert g.read_state([20]).drive is None
    ep, hst = serve("--profile", "p4-bench")                     # without the option: the profile's levels
    assert fixture.Gpio(hst, ep.fns["oep.fixture.gpio"]).drive_levels() == LEVELS


def test_fake_serve_retry_with_reset_at_start():
    ep, hst = serve("--profile", "esp32-v003", "--slot", "v003", "--boot-reset", "--silent-until-reset", "0",
                    "--label", "23=v003.nrst")
    assert ep.slots[0].boot_reset == 1 and ep.saved                # saved, as if the probe booted with them
    assert ep.slot_reset_log == [(0, NRST, 20)]
    st = config.ProbeConfig(hst).state().slots[0]
    assert st.state == "connected" and st.reset_at_ns is not None
    assert not ep.targets[(1, V003_PAIR)].silent_until_reset


@pytest.mark.parametrize("argv", [
    ("--boot-reset", "--label", "23=v003.nrst", "--label", "22=V003.NRST"),   # two at one step: no line
    ("--label", "23=v003.nrst"),                                  # the slot does not ask for it
])
def test_fake_serve_no_retry_with_reset(argv):
    ep, hst = serve("--profile", "esp32-v003", "--slot", "v003", "--silent-until-reset", "0", *argv)
    assert ep.slot_reset_log == []
    st = config.ProbeConfig(hst).state().slots[0]
    assert st.state == "absent" and st.reset_at_ns is None
    assert ep.targets[(1, V003_PAIR)].silent_until_reset


def test_fake_serve_label_wants_ch_eq_text(capsys):
    from oep_client import fake_serve
    with pytest.raises(SystemExit):
        fake_serve.parse(["--label", "nrst"])
    assert "CH=TEXT" in capsys.readouterr().err
    ep, _ = serve("--label", "0x14=t.nrst")
    assert ep.line_for("t", "nrst") == 0x14
