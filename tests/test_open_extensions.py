"""Host.open's named extensions (core §2.2, §12; host guide §7: a broker's sharing is asked by one, sent critical)."""
import random

import pytest

from oep_client import core, endpoint, host as h, message as m, registry, virtual_bench as vb

SHARE = "io.github.ch32-riscv-ug.share"


class Clock:
    def __init__(self):
        self.ms = 0

    def __call__(self):
        return self.ms


def host_on(ep):
    sent = []

    def send(b):
        sent.append(bytes(b))
        return ep.handle(b, 0)
    hst = h.Host(send, rng=random.Random(7))
    return hst, sent


def test_a_named_extension_goes_after_the_owner_critical():
    """open carries `name_len name data` under the named extension's tag with bit 7, after the owner."""
    hst, sent = host_on(endpoint.Endpoint(vb.p4_x035(), Clock()))
    with pytest.raises(h.Unsupported):
        hst.open(3000, owner="tests", extensions=[SHARE, ("io.example.with-data", b"\x01\x02")])
    tail = m.Request.unpack(sent[-1]).payload[5:]                         # after lease_ms(u32) force(u8)
    owner = m.tlv(registry.CORE.tlv["open"]["owner"], b"tests")
    share = m.tlv(m.TAG_NAMED_EXTENSION, bytes([len(SHARE)]) + SHARE.encode(), critical=True)
    data = m.tlv(m.TAG_NAMED_EXTENSION, bytes([20]) + b"io.example.with-data" + b"\x01\x02", critical=True)
    assert tail == owner + share + data


def test_a_probe_that_does_not_know_it_refuses_the_open():
    """The virtual bench knows no named extension: a critical one is refused unsupported, its tag as received, and no
    session is opened - a broker's client opens again without it (host guide §7)."""
    ep = endpoint.Endpoint(vb.p4_x035(), Clock())
    hst, _ = host_on(ep)
    with pytest.raises(h.Unsupported) as e:
        core.take(hst, 3000, owner="tests", extensions=[SHARE])
    assert e.value.tag == m.TAG_NAMED_EXTENSION | m.TAG_CRITICAL and hst.session is None
    assert hst.open(3000, owner="tests").lease_ms == 3000                # without it, the open is taken


def test_a_name_out_of_its_length_is_refused_before_sending():
    hst, sent = host_on(endpoint.Endpoint(vb.p4_x035(), Clock()))
    with pytest.raises(ValueError):
        hst.open(3000, extensions=[""])
    with pytest.raises(ValueError):
        hst.open(3000, extensions=["x" * 256])
