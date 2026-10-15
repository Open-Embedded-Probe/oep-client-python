"""Interface names (oep-spec core §12 rule 1, §7.2).

A name is dot-separated labels of lowercase ASCII letters, digits and '-' - each label 1 or more of them, not starting
or ending with '-', at least two labels - 1 to 48 bytes (core §7.2, §12 rule 1). It is the defining party's reverse DNS
name (io.github.<owner>.<name>, ...); the one exception is the project's own interfaces, which use the reserved short
prefix `oep.` instead: oep.<layer>.<name>, the layer one of probe, wire, target, fixture (core §12 rule 1) - nothing else
sets them apart. Nothing more is asked of a name here: which domains exist (the IANA list) is not checked.
"""

from __future__ import annotations

import re

from . import registry as reg

MAX_NAME = reg.LIMITS["interface_name_max_bytes"]   # 48 (core §7.2: one list entry fits the smallest max_frame)
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\Z")   # no '-' at either end (core §12 rule 1)
OEP_LAYERS = ("probe", "wire", "target", "fixture")   # oep.<layer>.<name> (core §12 rule 1)


class InvalidName(ValueError):
    pass


def kind(name: str) -> str:
    """'oep' (the project's short oep. names) or 'domain' (a reverse DNS name) - after validate()."""
    return "oep" if name.split(".", 1)[0] == "oep" else "domain"


def validate(name: str) -> str:
    """Return the name, or raise InvalidName saying which rule of core §12 rule 1 it breaks."""
    raw = name.encode("ascii", "strict") if name.isascii() else None
    if raw is None:
        raise InvalidName(f"{name!r}: not ASCII")
    if len(raw) > MAX_NAME:
        raise InvalidName(f"{name!r}: {len(raw)} bytes, the limit is {MAX_NAME}")
    labels = name.split(".")
    if len(labels) < 2:
        raise InvalidName(f"{name!r}: needs a namespace and at least one more label")
    for label in labels:
        if not _LABEL.match(label):
            raise InvalidName(f"{name!r}: label {label!r} is not [a-z0-9-]+ without '-' at either end")
    if labels[0] == "oep" and (len(labels) < 3 or labels[1] not in OEP_LAYERS):
        raise InvalidName(f"{name!r}: an oep. name is oep.<layer>.<name>, the layer one of {', '.join(OEP_LAYERS)}")
    return name


# Hosting services whose domain is often written wrongly as a top level. Whether a first label
# is a real top-level domain is not checked (that needs the IANA list); these are the common slips.
_HOSTING = {"github": "io.github", "gitlab": "io.gitlab", "codeberg": "page.codeberg",
            "bitbucket": "io.bitbucket"}


def lint(name: str) -> list[str]:
    """Warnings for names that are well-formed but break the namespace rules in spirit."""
    labels = name.split(".")
    out = []
    if labels[0] in _HOSTING:
        out.append(f"{labels[0]!r} is not a top-level domain; a {labels[0]} account's namespace is "
                   f"{_HOSTING[labels[0]]}.{labels[1] if len(labels) > 1 else '<name>'}")
    if labels[0] == "com" and len(labels) > 1 and labels[1] in _HOSTING:
        out.append(f"com.{labels[1]} is not given to accounts; use {_HOSTING[labels[1]]}.<name>")
    return out


def matches(name: str, prefix: str, exact: bool) -> bool:
    """List filtering: an empty prefix matches everything; otherwise match on label boundaries.

    oep.fixture.uart matches oep.fixture.uart and oep.fixture.uart.stream, not oep.fixture.uart2;
    with exact, only oep.fixture.uart itself.
    """
    if not prefix:
        return not exact
    if exact:
        return name == prefix
    return name == prefix or name.startswith(prefix + ".")
