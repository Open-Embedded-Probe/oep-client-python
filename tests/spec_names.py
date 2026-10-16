"""The selected SPEC's registry (tests/spec/oep-v1.toml, synced with the vectors by tools/sync_registry.sh) by name.

Tests, checks and fixtures look every protocol number up here by name - an op, a tag, an enum value, a family's shared
value - and never write it as a literal: a bare number says nothing at a glance and goes silently wrong when the
numbering moves (oep-spec core §2.5). Named lists (core §7.7: settings, console mechanisms, netifs) are numbered by the
probe, so those come from its describe, not from here. This module reads the spec's own file, independent of the
runtime client's generated registry (src/oep_client/registry.py)."""

from __future__ import annotations

import tomllib
from pathlib import Path

SPEC = tomllib.loads((Path(__file__).parent / "spec" / "oep-v1.toml").read_text())
_IFACES = {i["name"]: i for i in SPEC["interface"]}


def iface(name: str) -> dict:
    return SPEC["core"] if name == "core" else _IFACES[name]


def op(name: str, op_name: str) -> int:
    return next(o["code"] for o in iface(name)["op"] if o["name"] == op_name)


def ops(name: str) -> dict[str, int]:
    return {o["name"]: o["code"] for o in iface(name)["op"]}


def tag(name: str, context: str, tag_name: str) -> int:
    return iface(name)["tlv"][context][tag_name]


def tags(name: str, context: str) -> dict[str, int]:
    return dict(iface(name)["tlv"][context])


def enum(name: str, enum_name: str) -> dict[str, int]:
    return dict(iface(name)["enum"][enum_name])


def names(name: str, list_name: str) -> list[str]:
    """A named list's standard names in the registry's order (core §7.7)."""
    return list(iface(name)["names"][list_name])
