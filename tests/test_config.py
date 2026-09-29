"""oep.probe.config from the client (oep_client.config) and the `oep config` command, against the fake probe."""

import struct

from oep_client import __main__ as cli, config, endpoint, fake, host as h


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
    cfg.set([config.Slot(0, 1, pair, "x035", "at-boot", 1, "dmseq", lock),
             config.Bind(3, "manual", [("slot", 0), ("uart", 5)], selected=0)])
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
        cfg.set([config.Bind(1, "last-reset", [("slot", 0)])])    # port 1 is vendor bulk, and slot 0 is not there
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
    assert config.Label(20, "DUT TX") in items and config.Idle(21, "pull-up") in items
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
