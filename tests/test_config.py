"""oep.probe.config from the client (oep_client.config) and the `oep config` command, against the virtual bench
(oep-spec a09fa45: label / idle / disable items, the generation counter, save / erase / state)."""

import struct

import pytest

from oep_client import __main__ as cli, config, endpoint, virtual_bench, host as h, message as m


class Clock:
    t = 0

    def __call__(self):
        return self.t


def open_bench(profile=virtual_bench.p4_bench):
    ep = endpoint.Endpoint(profile(), Clock())
    hst = h.Host(lambda b: ep.handle(b, 1))                         # over vendor bulk
    hst.open(3000)
    return ep, hst


SMALL_FRAME = 64


def test_items_round_trip_with_the_generation():
    """probe.config §1, §2: count(u8) items, tag bit 7 clear; get in tag, then channel order; the generation counts
    the changes from 1; the same contents change nothing."""
    ep, hst = open_bench()
    cfg = config.ProbeConfig(hst)
    assert cfg.get() == (1, [])
    wanted = [config.Label(channel=20, text="DUT TX"), config.Idle(channel=21, mode="pull-up"),
              config.Disable(channel=30)]
    assert cfg.set(wanted) == 2
    assert ep.requests[-1].payload == bytes([3]) + b"".join(config.item(it) for it in wanted)
    items = cfg.items()
    assert [type(it) for it in items] == [config.Label, config.Idle, config.Disable]    # tag 0x02, 0x03, 0x07
    assert config.same_items(items, wanted) and not config.same_items(items, wanted[:2])
    assert cfg.set([config.Label(channel=20, text="DUT TX")]) == 2              # the same contents: no change
    assert cfg.unset([("idle", 21), ("idle", 22)]) == 3                          # a key not there: nothing
    assert ep.requests[-1].payload == bytes([2]) + struct.pack("<BHBH", config.ITEM["idle"], 21, config.ITEM["idle"], 22)
    assert cfg.set([config.remove("disable", 30)]) == 4
    assert cfg.items() == [config.Label(channel=20, text="DUT TX")]


def test_a_refused_set_changes_nothing():
    """probe.config §1: a channel past `channels` or the probe's own and a mode left unused are unsupported with the
    item's tag; idle and disable on one channel are unavailable cause 5; a disable of a channel in use cause 1. The
    settings, the generation and the pins stay."""
    ep, hst = open_bench()
    cfg = config.ProbeConfig(hst)
    cfg.set([config.Idle(channel=21, mode="pull-up")])
    before = cfg.get()
    for bad in ([config.Label(channel=ep.channels, text="x")], [config.Label(channel=24, text="own")]):
        with pytest.raises(h.Unsupported) as e:
            cfg.set(bad)
        assert e.value.tag == config.ITEM["label"]
    with pytest.raises(h.Unsupported):
        hst.call(cfg.fn, cfg.SET, b"\x01" + m.tlv(config.ITEM["idle"], struct.pack("<HB", 22, 9)))
    with pytest.raises(h.Unavailable) as e:
        cfg.set([config.Disable(channel=21)])
    assert e.value.result.payload == m.tlv(1, b"\x05") + m.tlv(2, struct.pack("<H", 21))
    from oep_client import core
    core.plan_apply(hst, [(4, 1, 30)])
    with pytest.raises(h.Unavailable) as e:
        cfg.set([config.Disable(channel=30)])                    # in use: cause 1
    assert e.value.result.payload[:4] == m.tlv(1, b"\x01")
    assert cfg.get() == before


def test_an_item_of_another_length_or_number_0_is_malformed_and_an_unknown_one_unsupported():
    ep, hst = open_bench()
    cfg = config.ProbeConfig(hst)
    for body in (b"\x01" + m.tlv(config.ITEM["idle"], struct.pack("<HBB", 21, 1, 0)),     # 4 bytes
                 b"\x01" + m.tlv(config.ITEM["disable"], b"\x15"),
                 b"\x01\x00\x02\x00\x15\x00",                                                # item number 0
                 b"\x02" + m.tlv(config.ITEM["disable"], struct.pack("<H", 21)) * 2):       # a key twice
        with pytest.raises(h.Rejected) as e:
            hst.call(cfg.fn, cfg.SET, body)
        assert e.value.result.detail == m.MALFORMED
    for tag in (0x3E, 0xBE):                                       # bit 7 does not matter for items
        with pytest.raises(h.Unsupported) as e:
            hst.call(cfg.fn, cfg.SET, b"\x01" + struct.pack("<BH", tag, 2) + b"\x15\x00")
        assert e.value.tag == tag
    assert cfg.get() == (1, [])


def test_save_state_and_the_next_boot():
    """probe.config §3: save writes the whole, state's saved_generation is the generation saved; a reboot applies the
    stored items (generation 1 again); an item this firmware cannot take is left out: partial, reason item."""
    ep, hst = open_bench()
    cfg = config.ProbeConfig(hst)
    assert vars(cfg.state()) == {"storage": "none", "saved_generation": 0, "reason": None}
    cfg.set([config.Idle(channel=21, mode="output-high"), config.Label(channel=21, text="power_hi")])
    assert cfg.needs_save()
    assert cfg.save() == 2
    assert vars(cfg.state()) == {"storage": "applied", "saved_generation": 2, "reason": None} and not cfg.needs_save()
    cfg.set([config.Label(channel=21, text="power")])
    assert cfg.needs_save() and cfg.state().saved_generation == 2
    ep.reboot()
    hst = h.Host(lambda b: ep.handle(b, 1))
    cfg = config.ProbeConfig(hst)
    assert cfg.get()[0] == 1 and config.Idle(channel=21, mode="output-high") in cfg.items()
    assert vars(cfg.state()) == {"storage": "applied", "saved_generation": 1, "reason": None}
    assert ep.parked[21] == config.IDLE["output-high"]
    ep.saved[(config.ITEM["label"], 99)] = struct.pack("<H", 99) + b"gone"     # a channel this firmware lacks
    ep.reboot()
    assert vars(cfg.state()) == {"storage": "partial", "saved_generation": 0, "reason": "item"}
    assert len(cfg.items()) == 2                                   # the others applied
    hst.open(3000)
    cfg.erase()
    assert vars(cfg.state()) == {"storage": "none", "saved_generation": 0, "reason": None}


def test_describe_and_a_probe_without_storage():
    ep, hst = open_bench()
    d = config.ProbeConfig(hst).describe()
    assert d.storage_bytes == 4096 and d.items == ["label", "idle", "disable", "passthrough", "console-passthrough"]
    probe = virtual_bench.VirtualProbe("no-storage", 1024, [virtual_bench._config(o.fn, 0, storage=0)
                                       if o.name == "oep.probe.config" else o for o in virtual_bench.p4_bench().offered])
    ep = endpoint.Endpoint(probe, Clock())
    hst = h.Host(lambda b: ep.handle(b, 1))
    hst.open(3000)
    cfg = config.ProbeConfig(hst)
    assert cfg.describe().storage_bytes == 0
    for op in (cfg.SAVE, cfg.ERASE, cfg.STATE):
        assert not cfg.offers(op)
        with pytest.raises(h.Rejected) as e:
            hst.call(cfg.fn, op, locked=op != cfg.STATE)
        assert e.value.result.detail == m.UNKNOWN_OPERATION


def test_get_is_paged_at_a_small_frame():
    probe = virtual_bench.rp2350_pins()
    small = virtual_bench.VirtualProbe("small", SMALL_FRAME, probe.offered + [virtual_bench._config(9, 0)],
                                       own_channels=probe.own_channels)
    ep = endpoint.Endpoint(small, Clock())
    hst = h.Host(lambda b: ep.handle(b, 1))
    hst.open(3000)
    cfg = config.ProbeConfig(hst)
    labels = [config.Label(channel=ch, text=f"line {ch}") for ch in range(8)]
    for it in labels:
        cfg.set([it])
    gets = len([r for r in ep.requests if r.op == cfg.GET])
    assert cfg.items() == labels
    assert len([r for r in ep.requests if r.op == cfg.GET]) - gets > 1            # more than one page
    with pytest.raises(ValueError, match="exceeds this transport's max_frame"):
        cfg.set(labels)                                            # one set: past 64 bytes
    longest = config.Label(channel=4, text="L" * 32)               # the longest label fits 64 (probe.config §4)
    assert cfg.set([longest]) > 0


def test_the_command(capsys, monkeypatch):
    ep, hst = open_bench()

    class FakeLink:
        def close(self):
            pass
    hst.link = FakeLink()
    hst.end()                                                       # the command takes the lock itself
    monkeypatch.setattr(cli.link, "open_host", lambda target: hst)
    assert cli.main(["config", "label", "x", "20", "DUT TX"]) == 0
    assert cli.main(["config", "idle", "x", "21", "pull-up", "--save"]) == 0
    assert cli.main(["config", "disable", "x", "30", "31"]) == 0
    assert cli.main(["config", "remove", "x", "disable", "31"]) == 0
    items = config.ProbeConfig(hst).items()
    assert config.Label(channel=20, text="DUT TX") in items and config.Idle(channel=21, mode="pull-up") in items
    assert config.Disable(channel=30) in items and config.Disable(channel=31) not in items
    assert ep.saved is not None
    capsys.readouterr()
    assert cli.main(["config", "show", "x"]) == 0
    out = capsys.readouterr().out
    assert "storage: applied" in out and "saved generation 3" in out and "now 5" in out
    assert cli.main(["config", "state", "x"]) == 0
    assert "storage: applied, saved generation 3" in capsys.readouterr().out


def test_lines_by_label():
    """host guide §5.3: `<profile>.<name>` first, then for a single target the bare name, then the firmware's label;
    two channels at one step: none."""
    labels = [config.Label(channel=3, text="A.NRST"), config.Label(channel=4, text="nrst"),
              config.Label(channel=5, text="power_hi"), config.Label(channel=6, text="b.power_hi")]
    assert config.find_line(labels, "nrst", "a", single=False) == 3
    assert config.find_line(labels, "nrst") == 4
    assert config.find_line(labels, "nrst", "b", single=False) is None
    assert config.find_line(labels, "power_hi", "b", single=False) == 6
    assert config.find_line(labels, "power_lo", firmware=[(9, "POWER_LO")]) == 9
    assert config.find_line(labels + [config.Label(channel=7, text="NRST")], "nrst") is None   # ambiguous


def test_reset_channels_and_a_named_reset_line():
    """oep-if-debug §3: the host reads the reset channels the probe allows and names one; there is no default."""
    from oep_client import riscv
    ep = endpoint.Endpoint(virtual_bench.esp32_v003(), Clock())
    hst = h.Host(lambda b: ep.handle(b, 0))
    hst.open(3000)
    wire = riscv.Wire(hst, "oep.wire.swio")
    assert wire.reset_channels() == [23]
    conn, dpc = wire.attach_under_reset(23)
    assert conn in ep.conns and dpc == 0
