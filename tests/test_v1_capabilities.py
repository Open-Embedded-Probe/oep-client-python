"""Draft capability discovery by name: names, wire forms, paging, dump (no hardware)."""

import json

import pytest

from oep_client.v1 import dump, fake, names, wire


# ---- names ---------------------------------------------------------------

@pytest.mark.parametrize("name", [
    "oep.core", "oep.fixture.i2c-target", "io.github.ch32-riscv-ug.p4.i2c-target",
    "local.bench.thing", "uuid.0123456789abcdef0123456789abcdef.tool", "jp.example.probe",
])
def test_valid_names(name):
    assert names.validate(name) == name


@pytest.mark.parametrize("name, why", [
    ("oep", "namespace"),
    ("OEP.core", "label"),
    ("oep.fixture_uart", "label"),
    ("1com.example.x", "top-level"),
    ("io.github", "reverse DNS"),
    ("uuid.1234.tool", "32 lowercase hex"),
    ("oep." + "a" * 60, "bytes"),
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
    assert names.kind("oep.target.flash") == "standard"


def test_prefix_matches_on_label_boundaries():
    assert names.matches("oep.fixture.uart", "oep.fixture.uart", exact=False)
    assert names.matches("oep.fixture.uart.stream", "oep.fixture.uart", exact=False)
    assert not names.matches("oep.fixture.uart2", "oep.fixture.uart", exact=False)
    assert not names.matches("oep.fixture.uart.stream", "oep.fixture.uart", exact=True)
    assert names.matches("anything.at.all", "", exact=False)


# ---- wire ----------------------------------------------------------------

def test_list_round_trip():
    entries = [wire.ListEntry(5, 3, 0, 0, "oep.fixture.i2c-target"),
               wire.ListEntry(6, 3, 1, 0, "io.github.ch32-riscv-ug.p4.i2c-target")]
    assert wire.unpack_list_result(wire.pack_list_result(7, entries)) == (7, entries)
    assert wire.unpack_list_request(wire.pack_list_request("oep.fixture", True, 4)) == ("oep.fixture", True, 4)


def test_channel_bitmap_round_trip():
    chans = [0, 1, 3, 4, 5, 23, 26, 53]
    assert wire.bitmap_to_channels(*wire.channels_to_bitmap(chans)) == chans


def test_description_keeps_what_it_does_not_know():
    data = (wire.role_channels(1, [4, 5]) + wire.role_channels(1, [9])
            + wire.channel_group(1, [(1, 18), (2, 23)])
            + wire.u32(wire.FEATURES, 0b101)
            + wire.tlv(0x3E, b"\x01")                 # unknown common, not critical: skipped
            + wire.tlv(0x80 | 0x3D, b"")              # unknown common, critical: remembered
            + wire.u8(0x40, 2))                       # interface-specific: kept raw
    d = wire.decode_description(data)
    assert d.roles == {1: [4, 5, 9]}
    assert d.groups == {1: [(1, 18), (2, 23)]}
    assert d.features == 0b101
    assert d.unknown_critical == [0xBD]
    assert d.specific == [(0x40, b"\x02")]


# ---- fake probes and dump ------------------------------------------------

def test_p4_follows_the_agreed_names():
    caps = dump.collect(fake.p4_x035().call)
    by_name = {o.entry.name: o for o in caps.offers}
    assert len(caps.offers) == 10
    assert caps.requests["list"] == 1 and caps.requests["describe"] == 10
    # the debug port: wire, riscv-dm and console are one instance
    assert {by_name[n].entry.instance for n in ("oep.wire.rvswd", "oep.target.riscv-dm", "oep.target.console")} == {1}
    assert by_name["oep.wire.rvswd"].description.groups[1] == [(1, 2), (2, 54)]
    i2c = by_name["io.github.ch32-riscv-ug.esp32.i2c-target"]
    assert i2c.description.roles[1] == i2c.description.roles[2]
    assert 2 not in i2c.description.roles[1]          # reserved for RVSWD
    for gone in ("oep.probe.identity", "oep.target.control", "oep.target.flash", "oep.fixture.i2c-target"):
        assert gone not in by_name


def test_the_probe_itself_is_described_by_core():
    row = dump.describe_offer(dump.collect(fake.esp32_v003().call).offers[0])
    assert row["name"] == "oep.core"
    d = row["declares"]
    assert d["unit id"] == "0070070d9394" and d["uart rates"] == "115200"
    assert "16 = SWIO" in d["label"] and "23 = NRST" in d["label"]    # repeated tags all kept


def test_paging_does_not_change_what_is_seen():
    small = fake.esp32_v003()
    big = fake.FakeProbe("big", 1024, small.offered)
    a, b = dump.collect(small.call), dump.collect(big.call)
    assert [dump.describe_offer(o) for o in a.offers] == [dump.describe_offer(o) for o in b.offers]
    assert a.requests["list"] > b.requests["list"]


def test_filters():
    probe = fake.esp32_v003()
    fixture = dump.collect(probe.call, "oep.fixture")
    assert {o.entry.name for o in fixture.offers} == {"oep.fixture.gpio", "oep.fixture.uart", "oep.fixture.capture"}
    one = dump.collect(probe.call, "io.github.ch32-riscv-ug.esp32.spi-target", exact=True)
    assert [o.entry.name for o in one.offers] == ["io.github.ch32-riscv-ug.esp32.spi-target"]
    assert one.offers[0].description.groups[2][0] == (1, 14)


def test_unknown_interfaces_are_shown_raw():
    probe = fake.FakeProbe("x", 256, [
        fake.Offered(0, 0, "oep.core"),
        fake.Offered(1, 1, "local.bench.widget", (wire.role_channels(3, [7]), wire.u32(wire.FEATURES, 0b10),
                                                   wire.u8(0x41, 9)))])
    row = dump.describe_offer(dump.collect(probe.call).offers[1])
    assert row["known"] is False
    assert row["roles"] == {"role3": "7"} and row["features"] == ["bit1"]
    assert row["declares"] == {"tag 0x41": "09"}


def test_text_and_json_outputs():
    caps = dump.collect(fake.p4_x035().call)
    text = dump.to_text(caps)
    assert "instance 6" in text and "io.github.ch32-riscv-ug.esp32.i2c-target" in text
    assert "features: preloaded tx, clock stretching" in text
    data = json.loads(dump.to_json(caps))
    assert data["max_frame"] == 1024 and len(data["interfaces"]) == 10
