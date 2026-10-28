"""Draft capability discovery by name: names, wire forms, paging, dump (no hardware)."""

import json

import pytest

from oep_client import catalog, dump, virtual_bench, names

RESTART_FN = next(o.fn for o in virtual_bench.p4_x035().offered if o.name == virtual_bench.RESTART)


# ---- names ---------------------------------------------------------------

@pytest.mark.parametrize("name", [
    "oep.probe.plan", "oep.fixture.i2c-target", "io.github.ch32-riscv-ug.p4.i2c-target", "jp.example.probe",
    "oep.probe." + "a" * 38,                                        # 48 bytes (core §7.2)
    "xn--p1ai.example.probe", "com.example",                        # core §12 rule 1: labels, two or more - no more
])
def test_valid_names(name):
    assert names.validate(name) == name


@pytest.mark.parametrize("name, why", [
    ("oep", "namespace"),
    ("OEP.core", "label"),
    ("oep.fixture_uart", "label"),
    ("oep.tool", "layer"),                                          # oep.<layer>.<name> (core §12 rule 1)
    ("oep.tools.uart", "layer"),                                    # the layer: probe, wire, target or fixture
    ("oep.probe", "layer"),
    ("oep.probe." + "a" * 39, "bytes"),                             # 49 bytes: over the 48 of core §7.2
    ("oep.-fixture", "label"),                                      # a label never starts or ends with '-' (C-23)
    ("oep.fixture-", "label"),
    ("oep..core", "label"),
])
def test_invalid_names_say_why(name, why):
    with pytest.raises(names.InvalidName, match=why):
        names.validate(name)


def test_hosting_names_are_linted_not_rejected():
    # Well-formed, so validate() accepts them; lint() points at the namespace the rules mean.
    assert names.validate("github.ch32-riscv-ug.ch32rv")
    assert "io.github.ch32-riscv-ug" in names.lint("github.ch32-riscv-ug.ch32rv")[0]
    assert "io.github" in names.lint("com.github.ch32-riscv-ug.ch32rv")[0]
    assert names.lint("io.github.ch32-riscv-ug.ch32rv") == []


def test_github_hosted_names_use_io_github():
    assert names.kind("io.github.ch32-riscv-ug.ch32rv") == "domain"
    assert names.kind("oep.target.flash") == "oep"


def test_prefix_matches_on_label_boundaries():
    assert names.matches("oep.fixture.uart", "oep.fixture.uart", exact=False)
    assert names.matches("oep.fixture.uart.stream", "oep.fixture.uart", exact=False)
    assert not names.matches("oep.fixture.uart2", "oep.fixture.uart", exact=False)
    assert not names.matches("oep.fixture.uart.stream", "oep.fixture.uart", exact=True)
    assert names.matches("anything.at.all", "", exact=False)


# ---- wire ----------------------------------------------------------------

def test_list_round_trip():
    entries = [catalog.ListEntry(5, 3, 0, "oep.fixture.i2c-target"),
               catalog.ListEntry(6, 3, 1, "io.github.ch32-riscv-ug.p4.i2c-target")]
    assert catalog.unpack_list_result(catalog.pack_list_result(7, entries)) == (7, entries)
    assert catalog.unpack_list_request(catalog.pack_list_request(300)) == (300, b"")
    assert catalog.pack_list_request(0x1234) == bytes([0x34, 0x12])   # first(u16) only, no prefix (core §7.2)
    assert catalog.unpack_list_request(bytes([0x34, 0x12]) + catalog.tlv(0x3D, b"")) == (0x1234, catalog.tlv(0x3D, b""))


def test_list_total_is_u16_and_a_longer_answer_is_not_rejected():
    entries = [catalog.ListEntry(0, 0, 1, "oep.core")]
    packed = catalog.pack_list_result(300, entries)
    assert packed[:3] == bytes([0x2C, 0x01, 1])                    # total u16, count u8
    assert packed[3:] == catalog.pack_entry(entries[0])          # count x entry, no element length (core §2.3)
    assert catalog.unpack_list_result(packed + bytes([0x55, 2, 0, 9, 9])) == (300, entries)   # a TLV tail is skipped


def test_the_core_is_never_listed_and_dump_shows_it_first():
    """fn 0 has no name and list never returns it (core §0, §7.2): dump describes it first as the core."""
    probe = virtual_bench.esp32_v003()
    total, entries = catalog.unpack_list_result(probe.call(0, 0x02, catalog.pack_list_request(0)))
    assert total == len(probe.offered) - 1 and all(e.fn != 0 for e in entries)   # every fn but fn 0
    caps = dump.collect(probe.call)
    first = caps.offers[0].entry
    assert (first.fn, first.name, first.revision) == (0, "", 1)
    assert dump.describe_offer(caps.offers[0])["name"] == "(core)"
    assert caps.offers[0].description.ops == set(virtual_bench.reg.CORE.op.values())     # the eight and the settings ops
    assert caps.revision == 1 and caps.max_frame == 512                              # the classic ESP32 firmware's


def test_channel_bitmap_round_trip():
    chans = [0, 1, 3, 4, 5, 23, 26, 53]
    assert catalog.bitmap_to_channels(*catalog.channels_to_bitmap(chans)) == chans


def test_description_keeps_what_it_does_not_know():
    data = (catalog.role_channels(1, [4, 5]) + catalog.role_channels(1, [9])
            + catalog.channel_group(1, [(1, 18), (2, 23)])
            + catalog.u32(catalog.FEATURES, 0b101)
            + catalog.tlv(0x3E, b"\x01")                 # unknown common, not critical: skipped
            + catalog.tlv(0x80 | 0x3D, b"")              # bit 7 in an answer: an unknown tag, skipped (core §2.2)
            + catalog.u8(0x40, 2))                       # interface-specific: kept raw
    d = catalog.decode_description(data)
    assert d.roles == {1: [4, 5, 9]}
    assert d.groups == {1: [(1, 18), (2, 23)]}
    assert d.features == 0b101
    assert d.unknown_critical == []
    assert d.specific == [(0x40, b"\x02")]


# ---- virtual bench probes and dump ------------------------------------------------

def test_p4_follows_the_agreed_names():
    caps = dump.collect(virtual_bench.p4_x035().call)
    by_name = {o.entry.name: o for o in caps.offers}
    n = len(virtual_bench.p4_x035().offered)
    assert len(caps.offers) == n                     # fn 0 and the listed: link, plan and restart the last three
    assert [o.entry.name for o in caps.offers[-3:]] == ["oep.probe.link", "oep.probe.plan", "oep.probe.restart"]
    assert caps.requests["list"] == 1 and caps.requests["describe"] == n
    link_ops = virtual_bench.reg.PROBE_LINK.op
    assert by_name["oep.probe.link"].description.ops == {link_ops["source"], link_ops["sink"]}   # no UART bridge here
    assert by_name["oep.fixture.logic"].description.ops >= {0x01, 0x02}     # it sends notifications (core §10.3)
    assert not by_name["oep.fixture.gpio"].description.ops & {0x01, 0x02}   # it sends none
    assert all(o.description.ops for o in caps.offers) and not caps.missing  # every fn's describe carries ops
    # the debug port: wire, riscv-dm and console are the first instance of each
    assert {by_name[n].entry.instance for n in ("oep.wire.rvswd", "oep.target.riscv-dm", "oep.target.console")} == {0}
    assert by_name["oep.wire.rvswd"].description.groups[1] == [(1, 2), (2, 54)]
    i2c = by_name["oep.fixture.i2c-target"]
    assert i2c.description.roles[1] == i2c.description.roles[2]
    assert 2 not in i2c.description.roles[1]          # reserved for RVSWD
    for gone in ("oep.probe.identity", "oep.target.control", "oep.target.flash", "io.github.ch32-riscv-ug.esp32.i2c-target"):
        assert gone not in by_name


def test_the_probe_itself_is_described_by_core():
    row = dump.describe_offer(dump.collect(virtual_bench.esp32_v003().call).offers[0])
    assert row["name"] == "(core)" and row["namespace"] == "core"
    d = row["declares"]
    assert d["unit id"] == "fafe00000003" and d["transport"] == "0 = UART bridge"
    assert "16 = GPIO16" in d["channel name"] and "23 = GPIO23" in d["channel name"]    # repeated tags all kept
    assert d["netif"] == "wifi"


def test_paging_does_not_change_what_is_seen():
    small = virtual_bench.esp32_v003_64()                                    # max_frame 64: list and describe paged
    big = virtual_bench.VirtualProbe("big", 1024, small.offered)
    a, b = dump.collect(small.call), dump.collect(big.call)
    assert [dump.describe_offer(o) for o in a.offers] == [dump.describe_offer(o) for o in b.offers]
    assert a.requests["list"] > b.requests["list"]


def test_filters():
    probe = virtual_bench.esp32_v003()
    fixture = dump.collect(probe.call, "oep.fixture")
    assert fixture.offers[0].entry.fn == 0                          # the core first, whatever the prefix
    assert {o.entry.name for o in fixture.offers[1:]} == {"oep.fixture.gpio", "oep.fixture.uart", "oep.fixture.logic",
                                                    "oep.fixture.i2c-target", "oep.fixture.spi-target",
                                                    "oep.fixture.i2c-controller", "oep.fixture.spi-controller",
                                                    "oep.fixture.pwm", "oep.fixture.counter", "oep.fixture.dac"}
    one = dump.collect(probe.call, "oep.fixture.spi-target", exact=True)
    assert [o.entry.name for o in one.offers[1:]] == ["oep.fixture.spi-target"]
    assert one.offers[1].description.groups[2][0] == (1, 14)


def test_a_describe_without_ops_is_named_missing():
    probe = virtual_bench.VirtualProbe("x", 256, [virtual_bench.Offered(0, 0, ""), virtual_bench.Offered(1, 0, "oep.fixture.gpio")])
    bare = virtual_bench.VirtualProbe("x", 256, [virtual_bench.Offered(o.fn, o.instance, o.name, tuple(t for t in o.tlvs if t[0] != catalog.OPS))
                                     for o in probe.offered], fill_ops=False)
    caps = dump.collect(bare.call)
    assert "describe of fn 0: ops" in caps.missing and "describe of fn 1: ops" in caps.missing   # core §1.2, §7.4



def test_restart_without_restart_max_ms_is_named_missing():
    """oep-if-restart §1: oep.probe.restart's describe carries restart_max_ms; the profiles do, and dump names it."""
    full = virtual_bench.p4_x035()
    caps = dump.collect(full.call)
    assert not caps.missing
    row = dump.describe_offer(next(o for o in caps.offers if o.entry.name == "oep.probe.restart"))
    assert row["declares"] == {"restart max ms": "2000"}
    assert "restart max ms: 2000" in dump.to_text(caps)
    bare = virtual_bench.VirtualProbe("x", 256, [virtual_bench.Offered(o.fn, o.instance, o.name,
                                                  tuple(t for t in o.tlvs if not (o.name == virtual_bench.RESTART
                                                                                  and t[0] == virtual_bench.RESTART_MAX_MS_TAG)))
                                     for o in full.offered], fill_ops=False)
    assert dump.collect(bare.call).missing == [f"describe of fn {RESTART_FN} (oep.probe.restart): restart_max_ms"]
    assert dump.required_of(bare.call) == [f"describe of fn {RESTART_FN} (oep.probe.restart): restart_max_ms"]
    assert dump.required_of(full.call) == []


def test_fn0_ops_lacking_a_core_op_or_broken_are_named_missing():
    """fn 0's ops: every core op (all mandatory, core §1.2, §12), as a core §7.4 value: base + a bitmap of 1 byte or
    more, base + 8 x bytes <= 256 (any such value that names the set - no one encoding)."""
    def with_core_ops(value: bytes):
        offered = [virtual_bench.Offered(0, 0, "", (catalog.tlv(catalog.OPS, value),) + tuple(
            t for t in o.tlvs if t[0] != catalog.OPS)) if o.fn == 0 else o for o in virtual_bench.esp32_v003().offered]
        return dump.collect(virtual_bench.VirtualProbe("x", 64, offered, fill_ops=False).call).missing
    assert with_core_ops(catalog.pack_ops({1, 2, 3, 0x10, 0x11, 0x12, 0x13})) == ["describe of fn 0: ops clock"]
    every = set(virtual_bench.reg.CORE.op.values())
    assert with_core_ops(catalog.pack_ops(every) + b"\x00\x00") == []          # a longer bitmap: the same set
    assert with_core_ops(bytes.fromhex("0100")) == ["describe of fn 0: ops " + ", ".join(
        dump.CORE_REQUIRED_OPS)]                                               # valid, names no op (the settings ops optional)
    for broken in (bytes.fromhex("01"), bytes.fromhex("f901"), bytes.fromhex("f0000000")):   # no bitmap; past 0xFF
        (why,) = with_core_ops(broken)
        assert why.startswith("describe of fn 0: ops in core §7.4"), why


def test_an_fn_with_broken_ops_is_named_and_unusable():
    offered = [virtual_bench.Offered(o.fn, o.instance, o.name, (catalog.tlv(catalog.OPS, b"\xf9\x01"),) + tuple(
        t for t in o.tlvs if t[0] != catalog.OPS)) if o.name == "oep.fixture.gpio" else o
        for o in virtual_bench.esp32_v003_64().offered]
    caps = dump.collect(virtual_bench.VirtualProbe("x", 64, offered).call)
    (why,) = caps.missing                                           # base 0xF9 + 8 > 256: past op 0xFF (core §7.4)
    assert why.startswith("describe of fn 4: ops in core §7.4") and "past op 0xFF" in why and why.endswith("not used)")
    row = dump.describe_offer(next(o for o in caps.offers if o.entry.fn == 4))
    assert row["unusable"].startswith("ops outside core §7.4")

def test_unknown_interfaces_are_shown_raw():
    probe = virtual_bench.VirtualProbe("x", 256, [
        virtual_bench.Offered(0, 0, ""),
        virtual_bench.Offered(1, 1, "local.bench.widget", (catalog.role_channels(3, [7]), catalog.u32(catalog.FEATURES, 0b10),
                                                   catalog.u8(0x41, 9)))])
    row = dump.describe_offer(dump.collect(probe.call).offers[1])
    assert row["known"] is False
    assert row["roles"] == {"role3": "7"} and row["features"] == ["bit1"]
    assert row["declares"] == {"tag 0x41": "09"}


def test_text_and_json_outputs():
    caps = dump.collect(virtual_bench.p4_x035().call)
    text = dump.to_text(caps)
    assert "instance 0" in text and "oep.fixture.i2c-target" in text
    assert "preloaded tx" not in text and "clock stretching" not in text   # one form (fixture §3); stretch is an op
    assert "ops: configure, read_rx, preload_tx, status, stretch" in text
    n = len(virtual_bench.p4_x035().offered)
    assert f"{n - 1} interfaces in 1 list and {n} describe requests" in text
    data = json.loads(dump.to_json(caps))
    assert data["max_frame"] == 1024 and len(data["interfaces"]) == n
