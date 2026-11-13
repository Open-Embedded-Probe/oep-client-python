"""Finding a broker that serves a probe, and opening a session that shares its lock (OEP host guide §7.1, §7.2).

A broker publishes itself in the user's directory `oep/brokers/<unit_id>.json`: where to connect (`endpoints`, each with
a `kind`; `tcp` is transports §1's TCP form) and the sharing extensions it takes (`share`). A tool tries, in order:

1. the environment variable `OEP_BROKER` (`<host>:<port>`), for a broker on another machine or a test;
2. the file: each endpoint of a kind it knows, checking that fn 0's describe names the file's unit_id (a dead broker's
   port may now be another probe's broker);
3. the probe itself.

The tool joins with the sharing extension `oep.probe.share` (critical): sessions and resources are the client's, the
broker holds the probe's lock and lease. A tool never deletes a broker's files: only the broker holding
`<unit_id>.lock` removes or rewrites `<unit_id>.json`, and `<unit_id>.lock` stays.

    from oep_client import brokers
    hst = brokers.open_host("30eda0e31108", owner="my-tool")   # through the broker when one serves it, else directly
"""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

SHARE = "oep.probe.share"             # host guide §7.1
ENV = "OEP_BROKER"                    # host guide §7.2: <host>:<port>, before the file
LEASE_MS = 60000


def directory() -> Path:
    """The user's broker directory (host guide §7.2): Linux $XDG_RUNTIME_DIR/oep/brokers, else
    $TMPDIR/oep-<uid>/brokers (/tmp without TMPDIR); macOS $TMPDIR/oep/brokers; Windows %LOCALAPPDATA%\\oep\\brokers."""
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "oep" / "brokers"
    if sys.platform == "darwin":
        return Path(os.environ.get("TMPDIR") or tempfile.gettempdir()) / "oep" / "brokers"
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return Path(runtime) / "oep" / "brokers"
    return Path(os.environ.get("TMPDIR") or "/tmp") / f"oep-{os.getuid()}" / "brokers"


def _private(path: Path) -> bool:
    """The directory is the user's own and closed to others (0700); not checked on Windows (its ACLs)."""
    if sys.platform == "win32":
        return path.is_dir()
    try:
        st = path.stat()
    except OSError:
        return False
    return stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid() and stat.S_IMODE(st.st_mode) & 0o077 == 0


@dataclass
class Broker:
    """One broker as its file (or OEP_BROKER) names it."""
    unit_id: str
    endpoints: list[dict] = field(default_factory=list)
    share: list[str] = field(default_factory=list)
    pid: int | None = None
    implementation: dict = field(default_factory=dict)

    def tcp(self) -> list[tuple[str, int]]:
        """The endpoints this client can use: kind tcp, `host:port`. Other kinds are skipped."""
        out = []
        for e in self.endpoints:
            if isinstance(e, dict) and e.get("kind") == "tcp" and isinstance(e.get("address"), str):
                host, _, port = e["address"].rpartition(":")
                if host and port.isdigit():
                    out.append((host, int(port)))
        return out


def find(unit_id: str, *, environ=None, base: Path | None = None) -> Broker | None:
    """The broker for this probe: OEP_BROKER, else its file in the user's directory, else None. Not connected yet."""
    environ = os.environ if environ is None else environ
    unit = unit_id.lower()
    forced = environ.get(ENV)
    if forced:
        return Broker(unit, [{"kind": "tcp", "address": forced}], [SHARE])
    if unit.startswith("x-"):
        return None                               # not unique: never keyed by (core §7.5)
    folder = base or directory()
    if not _private(folder):
        return None
    try:
        doc = json.loads((folder / f"{unit}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict) or str(doc.get("unit_id", "")).lower() != unit:
        return None
    share = [s for s in doc.get("share") or [] if isinstance(s, str)]
    endpoints = [e for e in doc.get("endpoints") or [] if isinstance(e, dict)]
    pid = doc.get("pid") if isinstance(doc.get("pid"), int) else None
    impl = doc.get("implementation") if isinstance(doc.get("implementation"), dict) else {}
    return Broker(unit, endpoints, share, pid, impl)


def connect(broker: Broker, *, timeout: float = 3.0):
    """A Host on the first endpoint that answers with the broker's unit_id in fn 0's describe, not opened; None when
    none does (a dead broker, or its port reused by another probe's broker)."""
    from . import core, host as oh, link
    for address, port in broker.tcp():
        try:
            lk = link.SerialLink.on_stream(link.TcpStream(address, port), "length", timeout)
        except OSError:
            continue
        lk.resend = False                         # the broker retries towards the probe itself (link.open_host)
        lk.transport = "tcp"
        h = oh.Host(lk.send)
        lk.attach_host(h)
        h.link = lk
        try:
            same = (core.unit_id(h) or "").lower() == broker.unit_id
        except Exception:
            same = False
        if same:
            return h
        lk.close()
    return None


def open_host(unit_id: str, owner: str, *, lease_ms: int = LEASE_MS, direct: str | None = None, environ=None):
    """An opened Host on the probe: sharing the broker's lock when one serves it (host guide §7.2), else the probe
    itself (`direct`, default oep://<unit_id>). A broker that does not take oep.probe.share is used without it."""
    from . import host as oh, link, message as m
    broker = find(unit_id, environ=environ)
    h = connect(broker) if broker else None
    if h is not None:
        try:
            if SHARE in broker.share:
                try:
                    h.open(lease_ms, owner=owner, extensions=[SHARE])
                    return h
                except oh.Unsupported as e:
                    if e.tag != m.TAG_NAMED_EXTENSION | m.TAG_CRITICAL:
                        raise
            h.open(lease_ms, owner=owner)
            return h
        except Exception:
            h.link.close()
            raise
    h = link.open_host(direct or f"oep://{unit_id}")
    h.open(lease_ms, owner=owner)
    return h
