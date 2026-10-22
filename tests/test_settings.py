"""The settings (oep-spec core §13, transports §7) from the client (oep_client.settings) and the `oep settings` command,
against the virtual bench: fn 0's get / set / unset / save / state / network, the items by name, the origins, the
generation, the channel items' effects on plans, wires and pins, the network, the save by name and the settings file."""

import json
import pathlib
import struct

import pytest

from oep_client import __main__ as cli, core, endpoint, fixture, riscv, settings, virtual_bench, host as h, message as m

CORE = settings.CORE_CATEGORY
_UNA = h.Unavailable


class Clock:
    t = 0

    def __call__(self):
        return self.t


def open_bench(profile=virtual_bench.p4_bench):
    ep = endpoint.Endpoint(profile(), Clock())
    hst = h.Host(lambda b: ep.handle(b, 1))                         # over vendor bulk
    hst.open(3000)
    return ep, hst, settings.Settings(hst)


def _cause(e) -> tuple[str, list[int]]:
    v = e.value
    return v.cause, v.channels


def ch(n: int) -> str:
    return virtual_bench.channel_name(n)


def label(n: int) -> str:
    return f"channel.{ch(n)}.label"


def idle(n: int) -> str:
    return f"channel.{ch(n)}.idle"


def disable(n: int) -> str:
    return f"channel.{ch(n)}.disable"


def user(st) -> dict:
    return {(c, n): v for c, n, v, o in st.show() if o == "user"}


# ---- the ops and the generation (core §13.5) --------------------------------------------------------------------------

def test_values_round_trip_with_the_generation():
    """set / get / unset: values by (fn, number, key) in fn, number, key order; the generation counts the changes from
    1; the same contents change nothing; an unset of nothing changes nothing."""
    ep, hst, st = open_bench()
    assert user(st) == {} and st.get()[0] == 1
    assert st.set_named(CORE, {label(20): "DUT TX", idle(21): "pull-up", disable(30): True}) == 2
    assert user(st) == {(CORE, label(20)): "DUT TX", (CORE, idle(21)): "pull-up", (CORE, disable(30)): True}
    _, values = st.get()
    assert [(v.fn, v.number, v.key) for v in values] == sorted((v.fn, v.number, v.key) for v in values)
    assert st.set_named(CORE, {label(20): "DUT TX"}) == 2                      # the same contents: no change
    assert st.unset_named(CORE, [idle(21), idle(22)]) == 3                     # a value not there: nothing
    assert st.unset_named(CORE, [disable(30)]) == 4
    assert user(st) == {(CORE, label(20)): "DUT TX"}


def test_a_refused_set_changes_nothing():
    """A channel past `channels` or the probe's own, a choice not declared: unsupported with the value's item index; idle
    and disable on one channel cause 5; the values, the generation and the pins stay."""
    ep, hst, st = open_bench()
    st.set_named(CORE, {idle(21): "pull-up"})
    before = st.get()
    number = ep.setting_number("channel.{}.label")
    for key in (ep.channels, 24):                                             # past the channels; the probe's own
        with pytest.raises(h.Unsupported) as e:
            st.set([(0, ep.setting_number("channel.{}.idle"), 21, b"\x02"), (0, number, key, b"x")])
        assert e.value.item == 1
    with pytest.raises(h.Unsupported) as e:
        st.set([(0, ep.setting_number("channel.{}.idle"), 21, b"\x09")])     # a choice the item does not declare
    assert e.value.item == 0
    with pytest.raises(h.Unavailable) as e:
        st.set_named(CORE, {idle(40): "pull-up", disable(40): True})
    assert _cause(e) == ("held_by_settings", [40])
    assert st.get() == before


def test_forms_and_numbers_malformed_or_unsupported():
    """core §13.3, §13.5: a value's length outside its form, number 0, the same value twice: malformed; a number past
    the fn's items or a fn the probe does not have: unsupported / unknown_function."""
    ep, hst, st = open_bench()
    n_idle = ep.setting_number("channel.{}.idle")
    for values in ([(0, n_idle, 20, b"\x01\x00")], [(0, 0, 20, b"\x01")], [(0, n_idle, 20, b"\x01"), (0, n_idle, 20, b"\x02")],
                   [(0, ep.setting_number("channel.{}.disable"), 20, b"\x02")], [(0, ep.setting_number("channel.{}.label"), 20, b"")]):
        with pytest.raises(h.Rejected) as e:
            st.set(values)
        assert e.value.result.detail == m.MALFORMED, values
    with pytest.raises(h.Unsupported) as e:
        st.set([(0, 99, 20, b"\x01")])
    assert e.value.item == 0
    with pytest.raises(h.Rejected) as e:
        st.set([(77, 1, 0, b"\x01")])
    assert e.value.result.detail == m.UNKNOWN_FUNCTION
    assert st.get()[0] == 1


def test_count_0_is_a_no_op_and_a_critical_tail_is_refused_before_anything():
    ep, hst, st = open_bench()
    assert st.set([]) == 1
    with pytest.raises(h.Rejected) as e:
        hst.call(0, settings.OP["set"], bytes([1]) + struct.pack("<HBHH", 0, ep.setting_number("channel.{}.idle"), 20, 1)
                 + b"\x01" + m.tlv(0x3E | 0x80, b"x"))
    assert e.value.result.detail == m.UNSUPPORTED
    assert st.get()[0] == 1 and user(st) == {}


def test_the_lock_column():
    """get, state and network need no lock; set, unset and save do (core §11)."""
    ep, hst, st = open_bench(virtual_bench.p4_x035)
    free = h.Host(lambda b: ep.handle(b, 1))
    for op in ("get", "state", "network"):
        payload = struct.pack("<H", 0) if op == "get" else b""
        free.request(0, settings.OP[op], payload, locked=False)
    for op in ("set", "unset", "save"):
        with pytest.raises(h.Rejected) as e:
            free.request(0, settings.OP[op], b"\x00" if op != "save" else b"", locked=False)
        assert e.value.result.detail == m.SESSION_REQUIRED


def test_get_is_paged_at_a_small_frame():
    ep = endpoint.Endpoint(virtual_bench.esp32_v003_64(), Clock())
    hst = h.Host(lambda b: ep.handle(b, 0))
    hst.open(3000)
    st = settings.Settings(hst)
    values = {label(n): f"LINE{n}" for n in (4, 5, 13, 14, 17, 18, 19)}
    st.set_named(CORE, values)
    assert {n: v for (c, n), v in user(st).items()} == values


def test_generation_exhausted_refuses_a_change():
    ep, hst, st = open_bench()
    ep.generation = 0xFFFFFFFF
    with pytest.raises(h.Unavailable) as e:
        st.set_named(CORE, {label(20): "x"})
    assert e.value.cause == "limit"
    assert st.set([]) == 0xFFFFFFFF


# ---- origins (core §13.4) -----------------------------------------------------------------------------------------

def test_fixed_labels_refuse_set_and_unset():
    ep, hst, st = open_bench(virtual_bench.p4_x035)
    shown = {(c, n): (v, o) for c, n, v, o in st.show()}
    assert shown[(CORE, label(2))] == ("SWDIO", "fixed")
    with pytest.raises(h.Unsupported):
        st.set_named(CORE, {label(2): "other"})
    with pytest.raises(h.Unsupported):
        st.unset_named(CORE, [label(2)])
    assert settings.export_file(st.catalog(), st.get()[1])["settings"] == {}   # fixed values are not exported
    assert core.firmware_labels(hst)[:2] == [(2, "SWDIO"), (51, "LED")]


def test_a_default_comes_back_after_an_unset():
    profile = lambda: virtual_bench.VirtualProbe("defaults", 1024, virtual_bench.with_settings(
        [o for o in virtual_bench.p4_bench().offered if o.fn != 0] + [virtual_bench._core(
            "3.0.0", "esp32p4", "30eda0e3b002", 55, virtual_bench._transports([(virtual_bench.TRANSPORT["vendor_bulk"], 0)]))],
        defaults=((0, "channel.{}.idle", 30, bytes([2])),)), own_channels=[24, 25])
    ep, hst, st = open_bench(profile)
    assert ep.parked[30] == 2
    st.set_named(CORE, {idle(30): "pull-up"})
    assert ep.parked[30] == 1 and user(st)[(CORE, idle(30))] == "pull-up"
    st.unset_named(CORE, [idle(30)])
    assert ep.parked[30] == 2 and (CORE, idle(30)) not in user(st)
    assert {(c, n): o for c, n, v, o in st.show()}[(CORE, idle(30))] == "default"


# ---- the channel items (core §13.8) ------------------------------------------------------------------------------

def test_a_plan_naming_a_disabled_channel_is_held_by_settings():
    ep, hst, st = open_bench()
    st.set_named(CORE, {disable(20): True})
    with pytest.raises(h.Rejected) as e:
        hst.call(core.plan_fn(hst), core.OP_PLAN_APPLY,
                 m.tlv(endpoint.ROLE_ASSIGNMENT, struct.pack("<HBH", 4, 1, 20), critical=True))
    assert _cause(e) == ("held_by_settings", [20])
    with pytest.raises(core.PinsTaken) as e2:                                 # the client names the setting
        core.plan_apply(hst, [(4, 1, 20)])
    assert "disabled" in e2.value.holders[0][1]


def test_an_attach_or_a_scan_naming_a_disabled_channel():
    ep, hst, st = open_bench()
    st.set_named(CORE, {disable(5): True})
    wire = riscv.Wire(hst, "oep.wire.rvswd")
    with pytest.raises(h.Rejected) as e:
        wire.attach(pins=(4, 5))
    assert _cause(e) == ("held_by_settings", [5])
    with pytest.raises(h.Rejected) as e:
        wire.scan([(4, 5)])
    assert _cause(e) == ("held_by_settings", [5])
    conn, _ = wire.attach(pins=(2, 3))                                        # another pair: as before
    assert conn in ep.conns


def test_gpio_on_a_disabled_channel():
    ep, hst, st = open_bench()
    st.set_named(CORE, {disable(30): True})
    gpio = fixture.Gpio(hst, 4)
    with pytest.raises(h.Rejected) as e:
        gpio.read([30])
    assert _cause(e) == ("held_by_settings", [30])


def test_disabling_a_channel_in_use_is_pin_in_use():
    ep, hst, st = open_bench()
    core.plan_apply(hst, [(4, 1, 30)])
    with pytest.raises(h.Rejected) as e:
        st.set_named(CORE, {disable(30): True})
    assert _cause(e) == ("pin_in_use", [30])
    core.plan_release(hst, [4])
    st.set_named(CORE, {disable(30): True})
    assert ep.disabled == {30}
    st.set_named(CORE, {disable(30): False})                                  # false: enabled again
    assert ep.disabled == set()


def test_unset_enables_it_again_and_parks_it():
    ep, hst, st = open_bench()
    st.set_named(CORE, {disable(20): True})
    ep.parked.pop(20, None)
    st.unset_named(CORE, [disable(20)])
    assert ep.parked[20] == 0
    core.plan_apply(hst, [(4, 1, 20)])
    assert (4, 1, 20) in ep.plan


def test_output_idle_on_an_input_only_channel_or_a_missing_pull_is_unsupported():
    ep, hst, st = open_bench()
    ep.input_only.add(40)
    ep.no_pull[41] = {1}
    with pytest.raises(h.Unsupported):
        st.set_named(CORE, {idle(40): "high"})
    with pytest.raises(h.Unsupported):
        st.set_named(CORE, {idle(41): "pull-up"})
    st.set_named(CORE, {idle(40): "pull-down", idle(41): "pull-down"})


def test_an_idle_set_while_the_channel_is_in_use_applies_when_it_is_free():
    ep, hst, st = open_bench()
    core.plan_apply(hst, [(4, 1, 30)])
    ep.parked.pop(30, None)
    st.set_named(CORE, {idle(30): "pull-up"})
    assert 30 not in ep.parked
    core.plan_release(hst, [4])
    assert ep.parked[30] == 1
    st.unset_named(CORE, [idle(30)])
    assert ep.parked[30] == 0                                                 # free: Hi-Z at once


def test_describe_lists_the_items_and_their_choices():
    ep, hst, st = open_bench(virtual_bench.p4_x035)
    cat = st.catalog()
    assert [it.name for it in cat.items[0]][:3] == ["channel.{}.label", "channel.{}.idle", "channel.{}.disable"]
    assert sorted(cat.items[0][1].choices.values()) == sorted(k.replace("_", "-") for k in endpoint.IDLE_MODE)
    assert cat.netif == {1: "ethernet"} and cat.channel[14] == "GPIO14"
    assert {cat.category[fn] for fn in cat.items if cat.items[fn]} >= {"core", "oep.wire.rvswd/0", "oep.fixture.uart/0",
                                                                         "oep.fixture.uart/1", "oep.target.console/0"}
    before = core.describe(hst, 4)
    st.set_named(CORE, {disable(30): True})
    assert core.describe(hst, 4) == before                                    # declarations only (core §7.3)


# ---- the save and the boot (core §13.7) ---------------------------------------------------------------------------

def test_save_state_and_the_next_boot():
    ep, hst, st = open_bench()
    st.set_named(CORE, {disable(20): True, idle(21): "pull-down", label(22): "X"})
    assert st.state().storage == "none" and st.needs_save()
    assert st.save() == 2
    assert st.state() == settings.State("applied", 2, None) and not st.needs_save()
    ep.reboot(0x4321)
    assert ep.disabled == {20} and 20 not in ep.parked and ep.parked[21] == 2
    hst2 = h.Host(lambda b: ep.handle(b, 1))
    hst2.open(3000)
    st2 = settings.Settings(hst2)
    assert st2.state() == settings.State("applied", 1, None) and st2.get()[0] == 1
    assert user(st2) == user(st)


def test_a_saved_value_this_firmware_cannot_take_is_left_out():
    ep, hst, st = open_bench()
    ep.load_settings([(0, "channel.{}.idle", 21, b"\x01"), (0, "channel.{}.idle", 24, b"\x01"),   # 24: the probe's own
                      (0, "channel.{}.disable", 21, b"\x01")])
    assert ep.storage_state == 3 and ep.storage_reason == 2
    assert ep.disabled == {21} and (CORE, idle(21)) not in user(st)          # disable wins over idle


def test_a_save_past_storage_is_cause_3_before_anything():
    ep, hst, st = open_bench()
    st.set_named(CORE, {label(n): "x" * 30 for n in range(26, 50)})
    ep.storage_max = 100
    with pytest.raises(h.Unavailable) as e:
        st.save()
    assert e.value.cause == "storage_full" and st.state().storage == "none"


@pytest.mark.parametrize("fault, after", [("old", ("applied", 2)), ("none", ("none", 0)), ("unreadable", ("unreadable", 0))])
def test_a_failed_save(fault, after):
    ep, hst, st = open_bench()
    st.set_named(CORE, {label(20): "A"})
    st.save()
    st.set_named(CORE, {label(20): "B"})
    ep.storage_fault = fault
    with pytest.raises(h.Failed):
        st.save()
    state = st.state()
    assert (state.storage, state.saved_generation) == after and user(st)[(CORE, label(20))] == "B"


def test_a_save_by_name_survives_moved_fns():
    """The save names its category by the interface's name and instance (core §13.1): a firmware with its fns
    renumbered applies the same values (here an fn reference: the console pass-through's wire)."""
    ep, hst, st = open_bench(virtual_bench.p4_x035)
    port = next(k for k, v in ep.transports.items() if v in virtual_bench.SERIAL_KINDS)
    route = st.catalog().route[port]
    st.set_named("oep.target.console/0", {f"passthrough.{route}.wire": "oep.wire.rvswd/0",
                                          f"passthrough.{route}.swdio": "GPIO2", f"passthrough.{route}.swclk": "GPIO54",
                                          f"passthrough.{route}.mechanism": "dmseq"})
    st.save()
    saved = dict(ep.saved)
    assert (("oep.target.console", 0), "passthrough.{}.wire", port) in saved
    assert saved[(("oep.target.console", 0), "passthrough.{}.wire", port)] == ("oep.wire.rvswd", 0)


# ---- the network (transports §7) ------------------------------------------------------------------------------------

def test_wifi_off_without_entries_dhcp_without_ipv4():
    ep = endpoint.Endpoint(virtual_bench.esp32_v003(), Clock())
    hst = h.Host(lambda b: ep.handle(b, 0))
    hst.open(3000)
    st = settings.Settings(hst)
    net = st.network()
    assert [(n.netif, n.state) for n in net.netifs] == [("wifi", "off")]
    st.set_named(CORE, {"wifi.0.ssid": "lab", "wifi.0.passphrase": "password1"})
    row = st.network().netifs[0]
    assert row.state == "up" and row.entry == 0 and row.address == "192.168.1.23/24"


def test_the_passphrase_is_a_secret():
    ep = endpoint.Endpoint(virtual_bench.esp32_v003(), Clock())
    hst = h.Host(lambda b: ep.handle(b, 0))
    hst.open(3000)
    st = settings.Settings(hst)
    st.set_named(CORE, {"wifi.0.ssid": "lab", "wifi.0.passphrase": "password1"})
    shown = {n: v for c, n, v, o in st.show()}
    assert shown["wifi.0.passphrase"] == {"secret": True}
    (v,) = [v for v in st.get()[1] if v.number == ep.setting_number("wifi.{}.passphrase")]
    assert v.data == b""
    st.set([(0, v.number, 0, b"")])                                          # 0 bytes: kept
    assert st.network().netifs[0].state == "up"
    with pytest.raises(h.Rejected) as e:
        st.set([(0, v.number, 1, b"")])                                      # nothing there to keep: malformed
    assert e.value.result.detail == m.MALFORMED
    for bad in ("short", "x" * 64):
        with pytest.raises(h.Rejected) as e:
            st.set_named(CORE, {"wifi.1.passphrase": bad})
        assert e.value.result.detail == m.MALFORMED


@pytest.mark.parametrize("values", [{"ipv4.wifi.address": "127.0.0.1"}, {"ipv4.wifi.address": "0.0.0.0"},
                                    {"ipv4.wifi.prefix": 33}, {"ipv4.wifi.prefix": 0}])
def test_an_ipv4_value_a_host_cannot_have_is_malformed(values):
    ep = endpoint.Endpoint(virtual_bench.esp32_v003(), Clock())
    hst = h.Host(lambda b: ep.handle(b, 0))
    hst.open(3000)
    with pytest.raises(h.Rejected) as e:
        settings.Settings(hst).set_named(CORE, values)
    assert e.value.result.detail == m.MALFORMED


def test_static_ipv4_and_a_change_in_use_reconnects():
    ep = endpoint.Endpoint(virtual_bench.esp32_v003(), Clock())
    hst = h.Host(lambda b: ep.handle(b, 0))
    hst.open(3000)
    st = settings.Settings(hst)
    st.set_named(CORE, {"wifi.0.ssid": "lab", "wifi.0.passphrase": "password1"})
    st.set_named(CORE, {"ipv4.wifi.mode": "static", "ipv4.wifi.address": "192.168.1.50", "ipv4.wifi.prefix": 24})
    assert st.network().netifs[0].address == "192.168.1.50/24" and ep.reconnects[1] == 1
    st.unset_named(CORE, ["ipv4.wifi.address"])                             # static without an address: none
    row = st.network().netifs[0]
    assert row.state == "waiting" and row.reason == "no_address"


def test_the_network_op_only_with_netifs():
    ep, hst, st = open_bench()                                               # p4-bench: no netif
    assert not st.offers("network")
    with pytest.raises(h.Rejected) as e:
        hst.request(0, settings.OP["network"], b"", locked=False)
    assert e.value.result.detail == m.UNKNOWN_OPERATION


# ---- the settings file (settings file §3, §4) -----------------------------------------------------------------------

def test_export_and_import_through_another_probe():
    ep, hst, st = open_bench(virtual_bench.p4_x035)
    st.set_named(CORE, {label(14): "NRST", idle(14): "pull-up", disable(30): True})
    st.set_named("oep.wire.rvswd/0", {"hold.GPIO2": False})
    doc = st.export()
    assert doc["settings"] == {CORE: {label(14): "NRST", idle(14): "pull-up", disable(30): True},
                               "oep.wire.rvswd/0": {"hold.GPIO2": False}}
    assert doc["probe"]["unit_id"] == "fafe00000035"
    ep2, hst2, st2 = open_bench(virtual_bench.p4_x035)
    st2.set_named(CORE, {label(15): "OLD"})
    doc2 = json.loads(json.dumps(doc))
    doc2["settings"]["io.example.gone/0"] = {"x": 1}
    plan = st2.plan_import(doc2)
    assert plan.unresolved == [("io.example.gone/0", "x")] and len(plan.sets) == 4 and len(plan.unsets) == 1
    st2.apply_import(plan)
    assert user(st2) == user(st)


FILE_VECTORS = json.loads((pathlib.Path(__file__).parent / "vectors" / "settings_file.json").read_text())


def vector_catalog(probe) -> settings.Catalog:
    """A case's probe: fn 0's describe TLVs and each listed fn's."""
    tlvs = lambda s: m.split_tlvs(bytes.fromhex(s))
    return settings.Catalog(tlvs(probe["fn0_describe_tlvs_hex"]),
                            [(e["fn"], e["name"], e["instance"], tlvs(e["describe_tlvs_hex"])) for e in probe["list"]])


@pytest.mark.parametrize("case", FILE_VECTORS["export"], ids=lambda c: c["name"])
def test_settings_file_export_vectors(case):
    """settings file §2, §3: the values get gives, written with the probe's names."""
    values = settings.values_from([bytes.fromhex(case["get_values_hex"])])
    assert settings.export_file(vector_catalog(case["probe"]), values) == case["file"]


@pytest.mark.parametrize("case", FILE_VECTORS["import"], ids=lambda c: c["name"])
def test_settings_file_import_vectors(case):
    """settings file §4: a file into a probe with other numbers - what to set and unset, the secrets it does not hold,
    what does not resolve."""
    plan = settings.plan_import(vector_catalog(case["probe"]),
                                settings.values_from([bytes.fromhex(case["current_values_hex"])]), case["file"])
    assert [(fn, n, k, d.hex()) for fn, n, k, d in plan.sets] == [
        (r["fn"], r["number"], r["key"], r["data_hex"]) for r in case["expected_set"]]
    assert plan.unsets == [(r["fn"], r["number"], r["key"]) for r in case["expected_unset"]]
    assert plan.secret_without_value == [tuple(r) for r in case["secret_without_value"]]
    assert plan.unresolved == [tuple(r) for r in case["unresolved"]]


def test_unset_all_then_save_empties_the_storage():
    ep, hst, st = open_bench()
    st.set_named(CORE, {disable(20): True})
    st.save()
    st.unset_all()
    st.save()
    assert ep.saved == {} and st.state().storage == "applied"


def test_a_label_given_at_boot_on_a_board_whose_wire_takes_any_pins():
    """rp2350-pins declares the core's items too: a label given as saved (virtual_bench_serve --label) names a port."""
    ep, hst, st = open_bench(virtual_bench.rp2350_pins)
    ep.load_settings([(0, "channel.{}.label", 2, b"bench-a.swdio")])
    assert user(st) == {(CORE, label(2)): "bench-a.swdio"}
    assert settings.find_line(hst, "swdio", "bench-a") == 2
    with pytest.raises(ValueError, match="declares no item"):
        ep.load_settings([(0, "channel.{}.colour", 2, b"x")])


# ---- the command ---------------------------------------------------------------------------------------------------

def _cli_bench(monkeypatch, profile=virtual_bench.p4_bench):
    ep, hst, st = open_bench(profile)

    class FakeLink:
        def close(self):
            pass
    hst.link = FakeLink()
    hst.end()
    monkeypatch.setattr(cli.link, "open_host", lambda target, **kw: hst)
    return ep, hst, st


def test_the_command(capsys, monkeypatch, tmp_path):
    ep, hst, st = _cli_bench(monkeypatch)
    assert cli.main(["settings", "set", "x", CORE, f"{disable(40)}=true", f"{label(41)}=DUT RX", "--save"]) == 0
    assert ep.disabled == {40} and ep.saved is not None
    out = capsys.readouterr().out
    assert "set: generation 2" in out and "saved: generation 2" in out
    assert cli.main(["settings", "show", "x"]) == 0
    out = capsys.readouterr().out
    assert f"core {label(41)} = \"DUT RX\"" in out and "storage: applied" in out
    path = tmp_path / "jig.oep-settings.json"
    assert cli.main(["settings", "export", "x", str(path)]) == 0
    assert json.loads(path.read_text())["settings"][CORE][disable(40)] is True
    assert cli.main(["settings", "unset", "x", CORE, disable(40)]) == 0 and ep.disabled == set()
    assert cli.main(["settings", "import", "x", str(path), "--yes"]) == 0 and ep.disabled == {40}
    assert cli.main(["settings", "unset", "x", "--all", "--save"]) == 0 and ep.saved == {}
    assert cli.main(["settings", "set", "x", CORE, "channel.GPIO99.label=x"]) == 2


# ---- the label convention (host guide §5.3) -------------------------------------------------------------------------

def test_find_line_by_labels():
    labels = [(20, "a.nrst"), (21, "b.nrst"), (22, "power_hi")]
    assert settings.find_line(labels, "nrst", "a") == 20
    assert settings.find_line(labels, "nrst") is None                       # two ports named: a bare name finds none
    assert settings.find_line([(5, "NRST")], "nrst") == 5                     # ASCII case ignored
    assert settings.find_line([(5, "nrst"), (6, "nrst")], "nrst") is None     # ambiguous


def test_find_line_reads_the_probe_fixed_labels_too():
    ep = endpoint.Endpoint(virtual_bench.esp32_v003(), Clock())
    hst = h.Host(lambda b: ep.handle(b, 0))
    hst.open(3000)
    assert settings.find_line(hst, "nrst") == 23
