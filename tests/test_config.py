"""oep.probe.config from the client (oep_client.config) and the `oep config` command, against the fake probe."""

import struct

import pytest

from oep_client import __main__ as cli, config, endpoint, fake, host as h, message as m


class Clock:
    t = 0

    def __call__(self):
        return self.t


def open_bench():
    ep = endpoint.Endpoint(fake.p4_bench(), Clock())
    hst = h.Host(lambda b: ep.handle(b, 1))                         # over vendor bulk
    hst.open(3000)
    return ep, hst


def test_slots_and_binds_round_trip_and_show_their_state():
    ep, hst = open_bench()
    cfg = config.ProbeConfig(hst)
    pair = ep.pairs[1][0]
    ep.targets[(1, pair)].target_id = 0x035E0601
    lock = (1, struct.pack("<I", 0xFFFFFF0F), struct.pack("<I", 0x035E0601 & 0xFFFFFF0F))
    cfg.set([config.Slot(slot=0, wire_fn=1, pins=pair, name="x035", attach="at-boot", retry_s=1, lock=lock),
             config.Bind(port=3, mode="manual", streams=[("slot", 0), ("uart", 5)], selected=0)])
    items = cfg.items()
    assert [type(i).__name__ for i in items] == ["Slot", "Bind"]
    assert items[0].name == "x035" and items[0].lock == lock and items[1].streams == [("slot", 0), ("uart", 5)]
    st = cfg.state()
    assert st.slots_max == 4 and st.bind_modes == ["last-reset", "manual", "mixed"]
    assert st.slots[0].state == "connected" and st.slots[0].target_id == struct.pack("<I", 0x035E0601)
    assert st.binds[0].port == 3 and st.binds[0].flow == "streaming"
    saved = cfg.save()
    assert cfg.state().saved_hash == saved and cfg.state().storage == "applied"
    cfg.set([config.remove("bind", 3)])
    assert [type(i).__name__ for i in cfg.items()] == ["Slot"]


def test_a_refused_set_changes_nothing():
    ep, hst = open_bench()
    cfg = config.ProbeConfig(hst)
    before = cfg.get()[0]
    try:
        cfg.set([config.Bind(port=1, mode="last-reset", streams=[("slot", 0)])])    # port 1 is vendor bulk, and slot 0 is not there
    except h.Rejected:
        pass
    assert cfg.get()[0] == before


def test_the_command(capsys, monkeypatch):
    ep, hst = open_bench()

    class FakeLink:
        def close(self):
            pass
    hst.link = FakeLink()
    hst.end()                                                       # the command takes the lock itself
    monkeypatch.setattr(cli.link, "open_host", lambda target: hst)
    assert cli.main(["config", "slot", "x", "--name", "x035", "--pins", "2,3", "--attach", "at-boot", "--retry", "1"]) == 0
    assert cli.main(["config", "bind", "x", "--port", "0", "--mode", "last-reset", "--stream", "slot:x035"]) == 0
    assert cli.main(["config", "show", "x"]) == 0
    out = capsys.readouterr().out
    assert "0 x035: fn 1 pins 2,3 at-boot retry 1 s dmseq" in out and "port 0 (usb_serial_jtag): last-reset [slot:x035]" in out


def test_plan_label_idle_from_the_command(capsys, monkeypatch):
    ep, hst = open_bench()

    class FakeLink:
        def close(self):
            pass
    hst.link = FakeLink()
    hst.end()
    monkeypatch.setattr(cli.link, "open_host", lambda target: hst)
    assert cli.main(["config", "plan", "x", "oep.fixture.uart", "rx=20", "tx=21"]) == 0
    assert cli.main(["config", "label", "x", "20", "DUT TX"]) == 0
    assert cli.main(["config", "idle", "x", "21", "pull-up", "--save"]) == 0
    assert {(5, 1, 20), (5, 2, 21)} <= ep.plan and 5 in ep.plan_from_config
    items = config.ProbeConfig(hst).items()
    assert config.Label(channel=20, text="DUT TX") in items and config.Idle(channel=21, mode="pull-up") in items
    assert ep.saved is not None


def test_slot_takes_the_only_wire_by_default(monkeypatch):
    ep = endpoint.Endpoint(fake.esp32_v003(), Clock())               # oep.wire.swio only
    hst = h.Host(lambda b: ep.handle(b, 0))

    class FakeLink:
        def close(self):
            pass
    hst.link = FakeLink()
    monkeypatch.setattr(cli.link, "open_host", lambda target: hst)
    assert cli.main(["config", "slot", "x", "--name", "v003", "--attach", "at-boot", "--retry", "1"]) == 0
    (slot,) = [i for i in config.ProbeConfig(hst).items() if isinstance(i, config.Slot)]
    assert slot.wire_fn == 1 and slot.pins == (16, 0xFFFF)


def test_a_plan_refused_for_a_saved_plan_names_the_holder():
    import pytest
    from oep_client import core
    ep, hst = open_bench()
    cfg = config.ProbeConfig(hst)
    cfg.set([config.Plan(fn=5, role=1, channel=20), config.Plan(fn=5, role=2, channel=21)])        # fixture.uart keeps 20 / 21 as a setting
    with pytest.raises(core.PinsTaken) as e:
        core.plan_apply(hst, [(4, 1, 21)])                          # gpio wants 21
    assert e.value.holders == [(21, "the saved plan of fn 5 (role 2)")] and "fn 5" in str(e.value)


def test_a_settings_plan_is_not_the_sessions():
    """core §8: plan_release leaves a plan the settings put in (n = 0 too); plan_apply naming its fn is refused."""
    ep, hst = open_bench()
    config.ProbeConfig(hst).set([config.Plan(fn=5, role=1, channel=20), config.Plan(fn=5, role=2, channel=21)])
    assert 5 in ep.plan_from_config
    with pytest.raises(h.Rejected) as e:
        hst.call(m.CORE_FN, m.OP_PLAN_APPLY, m.tlv(0x90, struct.pack("<HBH", 5, 1, 22)))
    assert e.value.result.detail == m.UNAVAILABLE
    hst.call(m.CORE_FN, m.OP_PLAN_RELEASE, b"\x00")
    assert {(5, 1, 20), (5, 2, 21)} <= ep.plan and 5 in ep.plan_from_config


def test_reset_channels_and_a_named_reset_line():
    """oep-if-debug §3: the host reads the reset channels the probe allows and names one; there is no default."""
    from oep_client import riscv
    ep = endpoint.Endpoint(fake.esp32_v003(), Clock())
    hst = h.Host(lambda b: ep.handle(b, 0))
    hst.open(3000)
    wire = riscv.Wire(hst, "oep.wire.swio")
    assert wire.reset_channels() == [23]
    conn, _dpc = wire.attach_under_reset(23)
    assert conn in ep.conns


def test_slot_line_settings_from_the_command(capsys, monkeypatch):
    ep, hst = open_bench()

    class FakeLink:
        def close(self):
            pass
    hst.link = FakeLink()
    hst.end()
    monkeypatch.setattr(cli.link, "open_host", lambda target: hst)
    assert cli.main(["config", "slot", "x", "--name", "l103", "--pins", "2,3", "--attach", "at-boot", "--retry", "1",
                     "--max-speed", "1000000", "--idle-clock", "low"]) == 0
    (slot,) = [i for i in config.ProbeConfig(hst).items() if isinstance(i, config.Slot)]
    assert (slot.max_speed, slot.idle_clock) == (1_000_000, "low")
    assert cli.main(["config", "show", "x"]) == 0
    assert "max 1000000 Hz idle-low" in capsys.readouterr().out


def test_saved_settings_follow_their_interfaces_by_name_not_number():
    """probe.config §2: the saved items name their fns by (name, instance, revision); a firmware that adds an
    interface before them renumbers them, and they apply there. One whose interface is gone is left unapplied."""
    import dataclasses

    ep, hst = open_bench()
    cfg = config.ProbeConfig(hst)
    pair = ep.pairs[1][0]
    uart = ep.fns["oep.fixture.uart"]
    cfg.set([config.Slot(slot=0, wire_fn=1, pins=pair, name="x035"),
             config.Bind(port=3, mode="manual", streams=[("slot", 0), ("uart", uart)])])
    cfg.save()

    def update(profile):                                             # a DFU to another firmware: the storage stays
        new = endpoint.Endpoint(profile, Clock())
        new.saved, new.saved_ids = ep.saved, ep.saved_ids
        new.reboot(0x5678)
        h2 = h.Host(lambda b: new.handle(b, 1))
        h2.open(3000)
        return new, config.ProbeConfig(h2)

    old = fake.p4_bench()
    moved = [o if o.fn == 0 else dataclasses.replace(o, fn=o.fn + 1) for o in old.offered]
    moved.insert(1, fake.Offered(1, 0, "io.github.test.new-first"))
    new, cfg2 = update(fake.FakeProbe("moved", old.max_frame, moved))
    st = cfg2.state()
    assert st.storage == "applied" and st.unreadable is None
    slot, bind = cfg2.items()
    assert slot.wire_fn == 2 and bind.streams == [("slot", 0), ("uart", uart + 1)]   # renumbered with them

    gone = [o for o in old.offered if o.name != "oep.fixture.uart"]
    _, cfg3 = update(fake.FakeProbe("gone", old.max_frame, gone))
    st = cfg3.state()
    assert st.storage == "unreadable" and "gone" in st.unreadable and cfg3.items() == []
