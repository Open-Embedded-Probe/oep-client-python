"""Typed wrappers for the v0 standard definitions. Each takes the offered-function reference."""

from __future__ import annotations

from . import codec
from .client import Client


class ProbeIdentity:
    DEFINITION = codec.DEF_PROBE_IDENTITY

    def __init__(self, client: Client, function: int):
        self.client, self.function = client, function

    def get(self) -> codec.ProbeIdentityGetResult:
        response = self.client.call(self.function, codec.PROBE_IDENTITY_OP_GET, codec.ProbeIdentityGetRequest().pack())
        return codec.ProbeIdentityGetResult.unpack(response.expect_success("probe.identity get"))
