"""virtual_bench_serve --minimal-ops (virtual_bench.minimal_ops): console and fixture UART at their smallest - open /
read / close / streams, configure / read / status - so a host's test can check it sends marks / clear / mark / write
only when describe's ops set them (conformance §2, core §1.2)."""

import struct

import pytest

from oep_client import catalog, core, endpoint, host as h, message as m, registry as reg, virtual_bench


def bench(minimal: bool):
    probe = virtual_bench.esp32_v003()
    ep = endpoint.Endpoint(virtual_bench.minimal_ops(probe) if minimal else probe, lambda: 0)
    return h.Host(ep.handle)


def ops_of(hst: h.Host, fn: int) -> set[int]:
    payload = hst.call(m.CORE_FN, m.OP_DESCRIBE, struct.pack("<HH", fn, 0), locked=False).payload
    return set(catalog.decode_description(payload[1:]).ops)


@pytest.mark.parametrize("name", ["oep.target.console", "oep.fixture.uart"])
def test_the_optional_stream_ops_are_left_out(name):
    i = reg.INTERFACES[name]
    optional = {i.op[k] for k in virtual_bench.STREAM_OPTIONAL_OPS}
    full = bench(False)
    assert optional <= ops_of(full, core.find(full, name))         # the profile as it is offers them
    hst = bench(True)
    fn = core.find(hst, name)
    assert not optional & ops_of(hst, fn)                          # at its smallest: none declared ...
    core.take(hst, 5000)
    body = (struct.pack("<I", 1) if name == "oep.target.console" else b"") + b"\0"
    with pytest.raises(h.Rejected) as e:                           # ... and none answered (core §1.2)
        hst.call(fn, i.op["mark"], body)
    assert e.value.result.detail == m.UNKNOWN_OPERATION
