"""probe.config's named items (oep-spec probe-config §1, core §7.7), the network settings every probe takes the same way
(§1.3-§1.5, §2 network) and a probe's own items (§1.6, §1.7), on the virtual bench.

The point of §1.3 is that one host routine sets any probe up: the same `provision` below runs on a classic ESP32 with
Wi-Fi and on an ESP32-P4 with wired Ethernet, finding what each offers by name (describe's setting and netif lists)."""

import struct

import pytest

from oep_client import catalog, config, core, endpoint, host as h, message as m, registry as reg, virtual_bench

FORM, FLAG = reg.PROBE_CONFIG.enum["setting_form"], reg.PROBE_CONFIG.enum["setting_flag"]


class Clock:
    def __init__(self):
        self.ms = 0

    def __call__(self):
        return self.ms


def bench(probe):
    ep = endpoint.Endpoint(probe, Clock())
    hst = h.Host(lambda b: ep.handle(b, 1))
    hst.open(3000)
    return ep, hst, config.ProbeConfig(hst)


def provision(cfg: config.ProbeConfig, ssid: str, passphrase: str, address: str | None) -> config.Network:
    """A host's routine for any probe (§1.3): Wi-Fi when the probe has it, the first netif's IPv4 (DHCP for None)."""
    decl = cfg.describe()
    items = []
    if "wifi" in decl.netifs:
        items.append(config.Wifi(entry=0, ssid=ssid, passphrase=passphrase))
    items.append(config.Ipv4(netif=decl.netifs[0], address=address, gateway=None))
    cfg.set(items)
    return cfg.network()


@pytest.mark.parametrize("profile, netif", [(virtual_bench.esp32_v003, "wifi"), (virtual_bench.p4_x035, "ethernet")])
@pytest.mark.parametrize("address", [None, "192.168.7.9/24"])
def test_one_host_routine_sets_up_either_probe(profile, netif, address):
    ep, hst, cfg = bench(profile())
    net = provision(cfg, "lab", "password1", address)
    (row,) = net.netifs
    assert row.netif == netif and row.state == "up" and row.reason == "none"
    assert row.address == (address or {"wifi": "192.168.1.23/24", "ethernet": "10.0.0.23/16"}[netif])
    assert (row.entry == 0) == (netif == "wifi") and (row.rssi < 0) == (netif == "wifi")


def test_common_defaults_wifi_off_without_entries_dhcp_without_ipv4():
    ep, hst, cfg = bench(virtual_bench.esp32_v003())
    (row,) = cfg.network().netifs
    assert (row.state, row.entry, row.address) == ("off", None, None)                     # no wifi item: off (§1.3)
    cfg.set([config.Wifi(entry=0, ssid="lab", passphrase="password1")])
    (row,) = cfg.network().netifs
    assert row.state == "up" and row.address == "192.168.1.23/24"                       # no ipv4 item: DHCP
    ep2, hst2, cfg2 = bench(virtual_bench.p4_x035())
    (row,) = cfg2.network().netifs
    assert row.state == "up" and row.address == "10.0.0.23/16"                          # wired: on with no item
    ep2.ether_link = False
    assert cfg2.network().netifs[0].state == "off"


def test_the_wifi_passphrase_is_write_only():
    ep, hst, cfg = bench(virtual_bench.esp32_v003())
    cfg.set([config.Wifi(entry=0, ssid="lab", passphrase="password1")])
    (it,) = [i for i in cfg.items() if isinstance(i, config.Wifi)]
    assert it == config.Wifi(entry=0, ssid="lab", passphrase=None) and "password1" not in repr(it)
    assert b"password1" not in hst.call(cfg.fn, cfg.GET, struct.pack("<H", 0), locked=False).payload
    gen = cfg.get()[0]
    assert cfg.set([it]) == gen                                     # sent back as read: nothing changes (§1.4)
    assert cfg.network().netifs[0].state == "up"
    cfg.set([config.Wifi(entry=1, ssid="lab", passphrase="wrong-one")])
    assert cfg.network().netifs[0].entry == 0                       # entries tried in key order; 0 still connects


def test_pass_len_ff_without_the_entry_is_malformed():
    ep, hst, cfg = bench(virtual_bench.esp32_v003())
    with pytest.raises(h.Rejected) as e:
        cfg.set([config.Wifi(entry=2, ssid="lab", passphrase=None)])
    assert e.value.result.detail == m.MALFORMED


@pytest.mark.parametrize("address, gateway, reason", [
    ("0.0.0.0/24", None, m.MALFORMED), ("127.0.0.1/8", None, m.MALFORMED), ("224.0.0.1/4", None, m.MALFORMED),
    ("192.168.1.9/24", "10.0.0.1", m.MALFORMED)])
def test_a_static_address_a_host_cannot_have_is_malformed(address, gateway, reason):
    ep, hst, cfg = bench(virtual_bench.esp32_v003())
    with pytest.raises(h.Rejected) as e:
        cfg.set([config.Ipv4(netif="wifi", address=address, gateway=gateway)])
    assert e.value.result.detail == reason and cfg.items() == []


def test_dhcp_with_an_address_is_malformed_and_a_netif_past_the_list_unsupported():
    ep, hst, cfg = bench(virtual_bench.esp32_v003())
    ipv4 = ep.setting_number("ipv4")
    dhcp_with_address = bytes([0]) + bytes([192, 168, 1, 9, 24]) + bytes(8)
    with pytest.raises(h.Rejected) as e:
        hst.call(cfg.fn, cfg.SET, b"\x01" + struct.pack("<BHH", ipv4, 2 + 14, 1) + dhcp_with_address)
    assert e.value.result.detail == m.MALFORMED
    for netif in (0, len(cfg.describe().netifs) + 1):                # netifs from 1 (core §7.7)
        with pytest.raises(h.Unsupported) as e:
            hst.call(cfg.fn, cfg.SET, b"\x01" + struct.pack("<BHH", ipv4, 2 + 14, netif) + bytes(14))
        assert e.value.tag == ipv4


def test_a_change_to_the_settings_in_use_reconnects_after_the_answer():
    ep, hst, cfg = bench(virtual_bench.esp32_v003())
    cfg.set([config.Wifi(entry=0, ssid="lab", passphrase="password1")])
    wifi = cfg.describe().netif_number("wifi")
    assert ep.reconnects[wifi] == 0
    cfg.set([config.Wifi(entry=3, ssid="other", passphrase="")])     # not the entry in use: kept (§1.3)
    assert ep.reconnects[wifi] == 0
    cfg.set([config.Ipv4(netif="wifi", address="192.168.1.50/24")])  # the netif's ipv4: reconnects
    assert ep.reconnects[wifi] == 1 and cfg.network().netifs[0].address == "192.168.1.50/24"
    cfg.unset([("wifi", 0)])                                         # the entry in use gone: reconnects (to none)
    assert ep.reconnects[wifi] == 2 and cfg.network().netifs[0].state == "waiting"


def test_an_unsaved_change_is_undone_by_a_restart():
    ep, hst, cfg = bench(virtual_bench.esp32_v003())
    cfg.set([config.Wifi(entry=0, ssid="lab", passphrase="password1")])
    cfg.save()
    cfg.set([config.Ipv4(netif="wifi", address="10.9.9.9/8")])      # a mistake: not saved
    ep.reboot()
    hst.session = None
    hst.open(3000)
    (row,) = config.ProbeConfig(hst).network().netifs
    assert row.state == "up" and row.address == "192.168.1.23/24"   # the saved settings: DHCP again (§1.3)


def own(*items, profile=virtual_bench.esp32_v003):
    """The profile with its config fn listing these own items too (name, form, keys, max_len, flags)."""
    probe = profile()
    probe.offered = [virtual_bench._config(o.fn, o.instance, storage=1024, channels=40, ports=1, netifs=("wifi",),
                                           own=items) if o.name == "oep.probe.config" else o for o in probe.offered]
    return probe


def test_a_probes_own_item_by_its_name_and_form():
    ep, hst, cfg = bench(own(("com.example.ntp-server", FORM["text"], 1, 32, 0)))
    s = cfg.describe().by_name("com.example.ntp-server")
    assert (s.form, s.keys, s.max_len, s.flags) == (FORM["text"], 1, 32, 0)
    cfg.set([config.Own("com.example.ntp-server", "pool.ntp.org")])
    assert config.Own("com.example.ntp-server", "pool.ntp.org") in cfg.items()
    for bad in ("", "x" * 33):                                        # text is 1 to max_len bytes (§1.6)
        with pytest.raises(h.Rejected) as e:
            cfg.set([config.Own("com.example.ntp-server", bad)])
        assert e.value.result.detail == m.MALFORMED
    with pytest.raises(h.Unsupported):                               # a key past keys
        cfg.set([config.Own("com.example.ntp-server", "x", key_=1)])
    cfg.unset([("com.example.ntp-server", 0)])
    assert not [i for i in cfg.items() if isinstance(i, config.Own)]


@pytest.mark.parametrize("form, value, data", [(FORM["u32"], 7, struct.pack("<I", 7)), (FORM["bool"], True, b"\x01"),
                                               (FORM["ipv4"], "10.1.2.3", bytes([10, 1, 2, 3]))])
def test_own_items_of_every_form(form, value, data):
    ep, hst, cfg = bench(own(("io.example.value", form, 2, 0, 0)))
    cfg.set([config.Own("io.example.value", value, key_=1)])
    assert ep.config[("io.example.value", 1)] == data
    assert config.Own("io.example.value", value if form != FORM["ipv4"] else __import__("ipaddress").IPv4Address(value),
                      1) in cfg.items()


def test_a_secret_own_item_is_write_only():
    ep, hst, cfg = bench(own(("io.example.token", FORM["text"], 1, 16, FLAG["secret"])))
    cfg.set([config.Own("io.example.token", "s3cret")])
    assert b"s3cret" not in hst.call(cfg.fn, cfg.GET, struct.pack("<H", 0), locked=False).payload
    assert config.Own("io.example.token", None) in cfg.items()      # read back: the key only (§1.7)
    gen = cfg.get()[0]
    assert cfg.set([config.Own("io.example.token", None)]) == gen   # nothing: keeps the value
    assert ep.config[("io.example.token", 0)] == b"s3cret"


def test_item_numbers_follow_the_probes_list_and_the_host_finds_them_by_name():
    """core §7.7: a probe that lists the same items in another order numbers them otherwise; the client's set and get
    are the same calls (it looks every number up by name), and a save is read back by name."""
    probe = virtual_bench.esp32_v003()
    cfg_fn = next(o for o in probe.offered if o.name == "oep.probe.config")
    setting = reg.PROBE_CONFIG.tlv["describe"]["setting"]
    swapped = [t for t in cfg_fn.tlvs if t[0] == setting][::-1]
    probe.offered = [virtual_bench.Offered(o.fn, o.instance, o.name, tuple(t for t in o.tlvs if t[0] != setting) + tuple(swapped))
                     if o is cfg_fn else o for o in probe.offered]
    ep, hst, cfg = bench(probe)
    a, b = bench(virtual_bench.esp32_v003())[0], ep
    assert a.setting_number("label") != b.setting_number("label")
    cfg.set([config.Label(channel=21, text="DUT RX"), config.Idle(channel=21, mode="pull-up")])
    assert config.Label(channel=21, text="DUT RX") in cfg.items()
    cfg.save()
    ep.reboot()                                                      # a firmware whose list moved: read back by name
    hst.session = None
    hst.open(3000)
    assert config.Label(channel=21, text="DUT RX") in config.ProbeConfig(hst).items()


def test_the_network_op_is_offered_only_with_netifs():
    ep, hst, cfg = bench(virtual_bench.p4_bench())                   # no netif
    assert not cfg.describe().netifs and not cfg.offers(cfg.NETWORK)
    ep, hst, cfg = bench(virtual_bench.esp32_v003())
    assert cfg.offers(cfg.NETWORK) and cfg.network().tcp_port == 0  # a UART bridge probe: no TCP listener


def raw_set(ep, hst, cfg, name, key, data):
    """One item set as the wire carries it (number looked up by name), for a form the client itself would not build."""
    return hst.call(cfg.fn, cfg.SET, b"\x01" + struct.pack("<BHH", ep.setting_number(name), 2 + len(data), key) + data)


@pytest.mark.parametrize("data", [
    b"\x00\x00",                                                     # ssid of 0 bytes
    bytes([33]) + b"s" * 33 + b"\x00",                               # ssid of 33 bytes
    b"\x03lab\x07passwor",                                          # passphrase of 7
    b"\x03lab\x08pass\x7fwor",                                      # a byte past 0x7E
    b"\x03lab\x40" + b"g" * 64,                                      # 64 that are not hex
    b"\x03lab\x41" + b"a" * 65,                                      # pass_len 65
    b"\x03lab\x00x",                                                 # a byte past the data
    b"\x03lab",                                                      # no pass_len
])
def test_a_wifi_item_out_of_its_ranges_is_malformed(data):
    """§1.4: ssid 1-32 bytes, passphrase none / 8-63 printable / 64 hex, data exactly 2 + ssid + passphrase."""
    ep, hst, cfg = bench(virtual_bench.esp32_v003())
    with pytest.raises(h.Rejected) as e:
        raw_set(ep, hst, cfg, "wifi", 0, data)
    assert e.value.result.detail == m.MALFORMED and cfg.items() == []


@pytest.mark.parametrize("data", [b"\x02" + bytes(13), b"\x01" + bytes([192, 168, 1, 9, 0]) + bytes(8),
                                  b"\x01" + bytes([192, 168, 1, 9, 33]) + bytes(8), bytes(13), bytes(15)])
def test_an_ipv4_item_of_another_mode_prefix_or_length_is_malformed(data):
    """§1.5: mode 0 or 1, prefix 1-32 when static, data 14 bytes."""
    ep, hst, cfg = bench(virtual_bench.esp32_v003())
    with pytest.raises(h.Rejected) as e:
        raw_set(ep, hst, cfg, "ipv4", cfg.describe().netif_number("wifi"), data)
    assert e.value.result.detail == m.MALFORMED


def test_the_network_rows_reason_when_it_does_not_connect():
    """§2 network: state 3 (waiting) with reason 1 when no entry's ssid is around, 2 when the passphrase is wrong;
    entry 0xFFFF and the address zero until it connects, reason 0 then."""
    ep, hst, cfg = bench(virtual_bench.esp32_v003())
    cfg.set([config.Wifi(entry=0, ssid="elsewhere", passphrase="password1")])
    (row,) = cfg.network().netifs
    assert (row.state, row.reason, row.entry, row.address, row.rssi) == ("waiting", "not_found", None, None, 0)
    cfg.set([config.Wifi(entry=1, ssid="lab", passphrase="wrong-one")])
    assert (cfg.network().netifs[0].state, cfg.network().netifs[0].reason) == ("waiting", "auth")
    cfg.set([config.Wifi(entry=1, ssid="lab", passphrase="password1")])
    (row,) = cfg.network().netifs
    assert (row.state, row.reason, row.entry) == ("up", "none", 1)


def test_setting_flags_restart_and_no_other_bit():
    """§1.7: a restart item's get shows the new value at once; the bench's settings carry only the defined bits."""
    ep, hst, cfg = bench(own(("io.example.mode", FORM["u32"], 1, 0, FLAG["restart"])))
    s = cfg.describe().by_name("io.example.mode")
    assert s.restart and not s.secret
    cfg.set([config.Own("io.example.mode", 3)])
    assert config.Own("io.example.mode", 3) in cfg.items()
    defined = sum(FLAG.values())
    for profile in (virtual_bench.esp32_v003, virtual_bench.p4_x035, virtual_bench.p4_bench):
        assert all(not s.flags & ~defined for s in bench(profile())[2].describe().settings)


def test_a_standard_name_is_form_named_and_an_own_one_has_a_form():
    """probe.config §4: a standard item's setting has form 0 (its name decides the form), an own item's 1-5."""
    for profile in (virtual_bench.esp32_v003, virtual_bench.p4_x035):
        for s in bench(profile())[2].describe().settings:
            assert (s.form == FORM["named"]) == ("." not in s.name), s


def test_the_command_sets_up_the_network_and_own_settings(capsys, monkeypatch):
    """`oep config wifi / ipv4 / network / set` (the passphrase from the environment, never printed)."""
    from oep_client import __main__ as cli
    ep, hst, cfg = bench(virtual_bench.esp32_v003())

    class FakeLink:
        def close(self):
            pass
    hst.link = FakeLink()
    hst.end()
    monkeypatch.setattr(cli.link, "open_host", lambda target: hst)
    monkeypatch.setenv("LAB_PASS", "password1")
    assert cli.main(["config", "wifi", "x", "0", "lab", "--passphrase-env", "LAB_PASS"]) == 0
    assert cli.main(["config", "ipv4", "x", "192.168.1.50/24", "--gateway", "192.168.1.1"]) == 0
    own = next(s.name for s in cfg.describe().settings if "." in s.name)
    assert cli.main(["config", "set", "x", own, "pool.ntp.org"]) == 0
    capsys.readouterr()
    assert cli.main(["config", "network", "x"]) == 0
    out = capsys.readouterr().out
    assert "wifi: up entry 0" in out and "192.168.1.50/24" in out and "password1" not in out
    assert cli.main(["config", "show", "x"]) == 0
    shown = capsys.readouterr().out
    assert "password1" not in shown and "lab" in shown and "pool.ntp.org" in shown
    assert cli.main(["config", "ipv4", "x", "dhcp"]) == 0
    assert config.Ipv4(netif="wifi") in config.ProbeConfig(hst).items() or not [
        i for i in config.ProbeConfig(hst).items() if isinstance(i, config.Ipv4) and i.address]


def test_the_command_sets_the_pass_throughs(capsys, monkeypatch):
    """`oep config passthrough / console-passthrough`: fns by name#instance, the mechanism by name (core §7.7)."""
    from oep_client import __main__ as cli
    ep, hst, cfg = bench(virtual_bench.p4_x035())

    class FakeLink:
        def close(self):
            pass
    hst.link = FakeLink()
    hst.end()
    monkeypatch.setattr(cli.link, "open_host", lambda target: hst)
    port = next(i for i, kind, _ in core.transports(hst) if kind in virtual_bench.SERIAL_KINDS)
    uart = next(e for e in core.list_entries(hst, "oep.fixture.uart", exact=True))
    described = catalog.decode_description(b"".join(m.tlv(t, v) for t, v in core.describe(hst, uart.fn)))
    rx = described.roles[reg.FIXTURE_UART.enum["role"]["rx"]][0]
    assert cli.main(["config", "passthrough", "x", str(port), f"oep.fixture.uart#{uart.instance}", str(rx)]) == 0
    assert config.Passthrough(port=port, fn=uart.fn, rx=rx) in config.ProbeConfig(hst).items()
    assert cli.main(["config", "remove", "x", "passthrough", str(port)]) == 0
    wire = next(e for e in core.list_entries(hst, "oep.wire.rvswd", exact=True))
    console = next(e for e in core.list_entries(hst, "oep.target.console", exact=True))
    capsys.readouterr()
    rc = cli.main(["config", "console-passthrough", "x", str(port), f"oep.wire.rvswd#{wire.instance}", "1", "2",
                   "--console", f"oep.target.console#{console.instance}", "--mechanism", "dmseq"])
    assert rc in (0, 2)   # the bench's wire may not allow pins 1 / 2: refused, never a crash
    if rc == 0:
        (it,) = [i for i in config.ProbeConfig(hst).items() if isinstance(i, config.ConsolePassthrough)]
        assert it.mechanism == console_mechanism(hst, console.fn, "dmseq")


def console_mechanism(hst, fn, name):
    from oep_client import console as c
    names = [v.decode() for t, v in core.describe(hst, fn) if t == c.Console.TAG_MECHANISM]
    return names.index(name) + 1
