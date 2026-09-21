"""v0 client against the in-process fake endpoint."""

import pytest

from oep_client.v0 import Client, FrameTransport, RequestError
from oep_client.v0 import codec
from oep_client.v0.fake import FakeStream
from oep_client.v0.services import ProbeIdentity

IDENTITY = codec.ProbeIdentityGetResult(profile_id=0x50344456, firmware_revision=0x30000,
                                        reserved_pin_mask=(1 << 2) | (1 << 54), fixture_pin_mask=0x3ff)


def identity_handler(operation, payload):
    if operation != codec.PROBE_IDENTITY_OP_GET:
        return codec.REJECT_UNKNOWN_OPERATION
    return IDENTITY.pack()


def make_client(**kw):
    stream = FakeStream(functions=[(*codec.DEF_PROBE_IDENTITY, identity_handler)], **kw)
    return Client(FrameTransport(stream)), stream


def test_confirm_list_identity():
    client, stream = make_client()
    limits = client.confirm()
    assert (limits.max_frame, limits.window_bytes, limits.max_inflight) == (1024, 4096, 8)
    functions = client.list_functions()
    assert [f.definition for f in functions] == [(0, 0), codec.DEF_PROBE_IDENTITY[:2]]
    identity = ProbeIdentity(client, client.find(*codec.DEF_PROBE_IDENTITY[:2]).function).get()
    assert identity == IDENTITY


def test_ping_and_rejections():
    client, _ = make_client()
    client.confirm()
    assert client.ping(b"hello") == b"hello"
    response = client.call(0x7fff, 1)
    assert response.rejected and response.detail == codec.REJECT_UNKNOWN_FUNCTION
    response = client.call(codec.DEF_CORE_FUNCTION, 0x7f)
    assert response.rejected and response.detail == codec.REJECT_UNKNOWN_OPERATION
    with pytest.raises(RequestError):
        response.expect_success()


def test_pipeline_respects_window_and_order():
    client, stream = make_client(limits=(64, 128, 4))
    client.confirm()
    payloads = [bytes([i]) * 20 for i in range(40)]
    responses = client.pipeline((codec.DEF_CORE_FUNCTION, codec.CORE_OP_PING, codec.CorePingRequest(data=p).pack())
                                for p in payloads)
    assert [codec.CorePingResult.unpack(r.payload).data for r in responses] == payloads
    assert stream.requests == 40 + 1


def test_oversized_request_is_refused_locally():
    client, _ = make_client(limits=(64, 64, 1))
    client.confirm()
    with pytest.raises(ValueError):
        client.ping(bytes(100))
