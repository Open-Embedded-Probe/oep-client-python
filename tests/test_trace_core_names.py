"""Trace of oep-spec 2b3d89a core §2.2 (the named extension TLV), §7.7 (named lists) and §12 rule 7 (how another party
adds), on the virtual bench and the client. Every number is looked up by name (the registry, describe's lists)."""

import struct

import pytest

from oep_client import catalog, config, console, core, endpoint, host as h, message as m, registry as reg
from oep_client import virtual_bench as vb
from oep_client.virtual_names import NAMED_EXTENSION, extension


class Clock:
    def __init__(self):
        self.ms = 0

    def __call__(self):
        return self.ms


def bench(profile=vb.esp32_v003):
    ep = endpoint.Endpoint(profile(), Clock())
    hst = h.Host(lambda b: ep.handle(b, 1))
    return ep, hst


def test_the_named_extension_tag_is_reserved_in_every_context():
    """core §2.2: tag 0x7F is the named extension in every context; no interface defines a TLV there."""
    for name, iface in reg.INTERFACES.items():
        for context, tags in iface.tlv.items():
            assert NAMED_EXTENSION not in tags.values(), (name, context)


@pytest.mark.parametrize("critical", [False, True])
def test_a_request_with_an_unknown_named_extension(critical):
    """core §2.2: an unknown name in a request is ignored, or refused unsupported naming tag 0xFF when critical;
    the extension may repeat."""
    ep, hst = bench()
    tail = extension() + extension(b"io.example.other", b"")
    if critical:
        tail += m.tlv(NAMED_EXTENSION, bytes([10]) + b"io.example", critical=True)
        with pytest.raises(h.Unsupported) as e:
            hst.call(0, m.OP_CLOCK, tail, locked=False)
        assert e.value.tag == NAMED_EXTENSION | m.TAG_CRITICAL
    else:
        assert hst.call(0, m.OP_CLOCK, tail, locked=False).succeeded


def test_a_reader_skips_an_unknown_named_extension_in_answers_and_describe():
    """core §2.2: in an answer and in describe, an extension the reader does not know is skipped."""
    ep, hst = bench()
    hst.open(3000)
    cfg = config.ProbeConfig(hst)
    cfg.set([config.Label(channel=21, text="DUT RX")])
    ep.config_get_tail = extension() + extension(b"io.example.other", b"\x01\x02")
    assert config.Label(channel=21, text="DUT RX") in cfg.items()
    plain = catalog.decode_description(m.tlv(catalog.OPS, b"\x10\x01"))
    extended = catalog.decode_description(m.tlv(catalog.OPS, b"\x10\x01") + extension())
    assert extended.ops == plain.ops


def test_a_named_lists_numbers_count_from_1_and_follow_the_probes_order():
    """core §7.7: the number is the place in describe's list from 1 (0 is never one); a host looks it up by name."""
    ep, hst = bench()
    hst.open(3000)
    con = console.Console(hst)
    names = con.mechanisms()
    assert names and [con.mechanism_number(n) for n in names] == list(range(1, len(names) + 1))
    decl = config.ProbeConfig(hst).describe()
    assert [decl.by_number(k).name for k in range(1, len(decl.settings) + 1)] == [s.name for s in decl.settings]
    assert decl.netif_number(decl.netifs[0]) == 1


def test_standard_names_have_no_dot_and_another_partys_have_one():
    """core §7.7, §12 rule 7: the registry's names are `a-z 0-9 -` without a dot; another party's are reverse-DNS."""
    for iface in reg.INTERFACES.values():
        for listed in getattr(iface, "names", {}).values():
            for name in listed:
                assert "." not in name and all(c.isalnum() or c == "-" for c in name) and name == name.lower(), name
    ep, hst = bench()
    hst.open(3000)
    own = [s.name for s in config.ProbeConfig(hst).describe().settings if "." in s.name]
    assert own == ["io.github.open-embedded-probe.ntp-server"]
