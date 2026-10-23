"""The virtual bench against oep-spec a09fa45: gpio set without a strength and the 3-byte idle item (fixture §1,
core §13.8); a target silent until a reset; virtual_bench_serve's options."""

import struct

import pytest

from oep_client import core, settings, endpoint, virtual_bench, fixture, host as h, message as m, registry as reg


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


# ---- gpio set and the idle item: no strength selection (fixture §1, core §13.8) -------------------------------

def test_set_has_no_drive_tlv_and_the_idle_item_is_3_bytes():
    """fixture §1: revision 1 defines no strength selection, so the old drive TLV (0x01) is an unknown tag - ignored plain,
    unsupported critical."""
    ep, hst, _ = open_probe()
    g = gpio_of(ep, hst)
    assert raw_set(hst, g, [(20, 4)], m.tlv(0x01, b"\x00\x01")).ran and ep.gpio_modes[20] == 4
    with pytest.raises(h.Unsupported) as e:
        raw_set(hst, g, [(20, 3)], m.tlv(0x01, b"\x00\x01", critical=True))
    assert e.value.tag == 0x81 and ep.gpio_modes[20] == 4
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


# ---- a target silent until a reset; virtual_bench_serve's options ------------------------------------------------

V003_PAIR, NRST = (16, 0xFFFF), 23                                # esp32-v003: swio on 16, role 3 on 23


def test_a_host_attach_with_reset_wakes_a_silent_target_and_scan_does_not_see_it():
    ep, hst, _ = open_probe(virtual_bench.esp32_v003())
    tg = ep.targets[(1, V003_PAIR)]
    tg.silent_until_reset = True
    W = reg.WIRE_RVSWD.op
    assert hst.call(1, W["scan"], struct.pack("<BHH", 1, *V003_PAIR)).payload[1] == 0   # scan: nothing found
    attach = struct.pack("<BHHI", 0, *V003_PAIR, 1_000_000)
    assert not hst.request(1, W["attach"], attach).succeeded
    reset = m.tlv(reg.WIRE_RVSWD.tlv["attach"]["reset"], struct.pack("<HH", NRST, 20), critical=True)
    assert hst.call(1, W["attach"], attach + reset).succeeded and not tg.silent_until_reset


class FakeLink:
    def close(self):
        pass


def test_idle_from_the_command(capsys, monkeypatch):
    from oep_client import __main__ as cli
    ep, hst, _ = open_probe(virtual_bench.esp32_v003())
    hst.link = FakeLink()
    hst.end()
    monkeypatch.setattr(cli.link, "open_host", lambda target: hst)
    for gone in (["config", "idle", "x", "21", "low"], ["settings", "wifi", "x"]):
        with pytest.raises(SystemExit):                            # gone: the settings are fn 0's (core §13)
            cli.main(gone)
    assert cli.main(["settings", "set", "x", "core", "channel.GPIO21.idle=low"]) == 0
    assert ep.parked[21] == 3


# ---- virtual_bench_serve's options for other hosts' tests ----------------------------------------------------------------

def serve(*argv):
    from oep_client import virtual_bench_serve
    ep = virtual_bench_serve.build(virtual_bench_serve.parse(["--tcp", "0", *argv]))
    return ep, h.Host(lambda b: ep.handle(b, 1))


def test_virtual_bench_serve_has_no_slot_bind_or_wifi_options(capsys):
    from oep_client import virtual_bench_serve
    for argv in (["--slot", "v003"], ["--bind", "0"], ["--wifi-air", "lab"], ["--uart-plan"]):
        with pytest.raises(SystemExit):
            virtual_bench_serve.parse(argv)


def test_virtual_bench_serve_label_wants_ch_eq_text(capsys):
    from oep_client import virtual_bench_serve
    with pytest.raises(SystemExit):
        virtual_bench_serve.parse(["--label", "nrst"])
    assert "CH=TEXT" in capsys.readouterr().err
    ep, hst = serve("--label", "0x14=t.nrst")
    assert ep.saved and settings.find_line(hst, "nrst", "t") == 0x14
