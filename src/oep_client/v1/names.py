"""Interface names (oep-spec docs/capability-identification-comparison.ja.md, draft).

A name is dot-separated labels of lowercase ASCII letters, digits and '-', at most 48 bytes. The
first label says which kind of namespace it is:

  oep.                      the OEP standard (reserved; `oep` is not a real top-level domain)
  local.                    bench-only experiments, never published, no interoperability promise
  uuid.<32 hex>.            an author with no domain who still wants a unique namespace
  <tld>.<domain>...         reverse DNS of a domain the author owns, including hosting domains
                            such as io.github.<name> (GitHub gives <name>.github.io to one owner)
"""

from __future__ import annotations

import re

MAX_NAME = 48
_LABEL = re.compile(r"[a-z0-9-]+\Z")
_TLD = re.compile(r"[a-z]{2,63}\Z")
_UUID = re.compile(r"[0-9a-f]{32}\Z")


class InvalidName(ValueError):
    pass


def kind(name: str) -> str:
    """'standard', 'local', 'uuid' or 'domain' - after validate()."""
    first = name.split(".", 1)[0]
    return {"oep": "standard", "local": "local", "uuid": "uuid"}.get(first, "domain")


def validate(name: str) -> str:
    """Return the name, or raise InvalidName saying which rule it breaks."""
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
            raise InvalidName(f"{name!r}: label {label!r} is not [a-z0-9-]+")
    first = labels[0]
    if first == "uuid":
        if len(labels) < 3 or not _UUID.match(labels[1]):
            raise InvalidName(f"{name!r}: uuid. must be followed by 32 lowercase hex digits and a name")
    elif first not in ("oep", "local"):
        if not _TLD.match(first):
            raise InvalidName(f"{name!r}: {first!r} is not a top-level domain (reverse DNS expected)")
        if len(labels) < 3:
            raise InvalidName(f"{name!r}: reverse DNS needs <tld>.<domain>.<name>")
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
