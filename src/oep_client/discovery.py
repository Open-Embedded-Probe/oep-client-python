"""Finding probes that serve OEP over TCP on the local network: DNS-SD service `_oep._tcp` over mDNS.

    for p in discovery.browse(timeout=2.0):
        print(p.unit_id, p.host, p.port, p.addresses)
    found = discovery.find_unit("fafe00000003")          # one probe by its unit_id (TXT unit_id=...)

A probe listening on TCP advertises a DNS-SD (RFC 6763) instance of `_oep._tcp` over mDNS (RFC 6762) while it listens
(oep-spec transports §3): the port is the SRV record's (none is fixed), the TXT record carries `unit_id=<unit_id>`
(fn 0's describe's; other keys are ignored here), the instance and host names are the probe's. A host uses a named
probe only when describe's unit_id after opening is the one it named (`link.open_host("tcp:UNIT_ID")` checks it; host
guide §4.1). The reference probe (oep-probe-arduino) announces host `oep-<unit_id>.local`, instance `OEP <unit_id>`,
port 7450 - an example, not a rule.

Two ways to ask, the same answer:
- python-zeroconf, when installed (`pip install 'oep-client-python[mdns]'`): a full mDNS browser.
- otherwise a minimal one-shot query of this module's own: PTR `_oep._tcp.local` (then SRV / TXT / A for what the
  answers left out), sent to 224.0.0.251:5353 from an ephemeral port with the unicast-response bit, so responders
  answer this socket directly (RFC 6762 §5.4, §6.7); a second socket on 5353 also listens for multicast answers when the
  port can be shared. The query goes out of every IPv4 interface (IP_MULTICAST_IF per interface address,
  `interface_addresses`): a multicast sent once leaves by one interface only (on Windows often a virtual adapter's, and
  a probe on the Wi-Fi adapter never hears it). The 5353 socket joins the group on each of them. IPv4 only.

mDNS stays on the local link: behind a NAT (WSL 2's default network, a VM) or across subnets nothing is found - name the
probe as tcp://HOST:PORT then (the address is also in `oep config state` over another transport: the wifi state's ip).
"""

from __future__ import annotations

import secrets
import select
import socket
import struct
import sys
import time
from dataclasses import dataclass, field

SERVICE = "_oep._tcp.local."
MDNS_GROUP, MDNS_PORT = "224.0.0.251", 5353
T_A, T_PTR, T_TXT, T_SRV = 1, 12, 16, 33
QU = 0x8000                    # a question's class: unicast response wanted (RFC 6762 §5.4)


@dataclass
class Found:
    """One announced probe: the DNS-SD instance, its unit_id (TXT; None when absent), the SRV host and port, the IPv4
    addresses of that host."""
    instance: str
    unit_id: str | None = None
    host: str = ""
    port: int = 0
    addresses: list[str] = field(default_factory=list)
    txt: dict[str, str] = field(default_factory=dict)

    @property
    def target(self) -> str:
        """The open_host target: tcp://ADDRESS:PORT (the host name when no address came; "" without an SRV port)."""
        where = self.addresses[0] if self.addresses else self.host.rstrip(".")
        return f"tcp://{where}:{self.port}" if self.port and where else ""


# ---- the DNS message form (RFC 1035 §4), only what a browse needs ----------------------------------------------------

def encode_name(name: str) -> bytes:
    out = b""
    for label in name.rstrip(".").split("."):
        raw = label.encode()
        if not 1 <= len(raw) <= 63:
            raise ValueError(f"DNS label {label!r}: 1 to 63 bytes")
        out += bytes([len(raw)]) + raw
    return out + b"\x00"


def query(questions: list[tuple[str, int]], qid: int = 0, unicast: bool = True) -> bytes:
    """A query with these (name, type) questions, class IN (with the QU bit when `unicast`)."""
    body = b"".join(encode_name(n) + struct.pack(">HH", t, 1 | (QU if unicast else 0)) for n, t in questions)
    return struct.pack(">HHHHHH", qid, 0, len(questions), 0, 0, 0) + body


def _read_name(data: bytes, at: int) -> tuple[str, int]:
    """A name at `at` (compression pointers followed, RFC 1035 §4.1.4) -> (name with a trailing dot, the offset past
    it where it was read)."""
    labels, end, jumps = [], None, 0
    while True:
        if at >= len(data):
            raise ValueError("name past the end")
        n = data[at]
        if n & 0xC0 == 0xC0:
            if at + 1 >= len(data) or jumps > 32:
                raise ValueError("bad compression pointer")
            if end is None:
                end = at + 2
            at = ((n & 0x3F) << 8) | data[at + 1]
            jumps += 1
            continue
        if n & 0xC0:
            raise ValueError("unknown label type")
        if n == 0:
            return ".".join(labels) + ".", (end if end is not None else at + 1)
        labels.append(data[at + 1:at + 1 + n].decode("utf-8", "replace"))
        at += 1 + n


def records(packet: bytes) -> list[tuple[str, int, object]]:
    """Every resource record of a response (answers, authority, additional) as (name, type, data): PTR -> name, SRV ->
    (target, port), TXT -> {key: value}, A -> dotted address; other types are left out. A query (QR 0) gives none."""
    if len(packet) < 12:
        return []
    _, flags, qd, an, ns, ar = struct.unpack_from(">HHHHHH", packet)
    if not flags & 0x8000:
        return []
    at, out = 12, []
    try:
        for _ in range(qd):
            _, at = _read_name(packet, at)
            at += 4
        for _ in range(an + ns + ar):
            name, at = _read_name(packet, at)
            rtype, _, _, rdlen = struct.unpack_from(">HHIH", packet, at)
            at += 10
            rdata_at, at = at, at + rdlen
            if at > len(packet):
                break
            if rtype == T_PTR:
                out.append((name, rtype, _read_name(packet, rdata_at)[0]))
            elif rtype == T_SRV and rdlen >= 7:
                _, _, port = struct.unpack_from(">HHH", packet, rdata_at)
                out.append((name, rtype, (_read_name(packet, rdata_at + 6)[0], port)))
            elif rtype == T_TXT:
                txt, i = {}, rdata_at
                while i < at:
                    n = packet[i]
                    entry = packet[i + 1:i + 1 + n].decode("utf-8", "replace")
                    i += 1 + n
                    if entry:
                        k, _, v = entry.partition("=")
                        txt.setdefault(k.lower(), v)
                out.append((name, rtype, txt))
            elif rtype == T_A and rdlen == 4:
                out.append((name, rtype, socket.inet_ntoa(packet[rdata_at:at])))
    except (ValueError, struct.error, IndexError):
        pass                                                   # a broken record ends the packet: what came before counts
    return out


class Browser:
    """What the answers have said so far, and the questions still open (the minimal query's state; `feed` takes a
    response packet)."""

    def __init__(self, service: str = SERVICE):
        self.service = service.lower()
        self.instances: set[str] = set()
        self.srv: dict[str, tuple[str, int]] = {}
        self.txt: dict[str, dict[str, str]] = {}
        self.a: dict[str, list[str]] = {}

    def feed(self, packet: bytes) -> None:
        for name, rtype, data in records(packet):
            key = name.lower()
            if rtype == T_PTR and key == self.service:
                self.instances.add(data)
            elif rtype == T_SRV:
                self.srv[key] = data
            elif rtype == T_TXT:
                self.txt[key] = data
            elif rtype == T_A and data not in self.a.setdefault(key, []):
                self.a[key].append(data)

    def questions(self) -> list[tuple[str, int]]:
        """The PTR question, and SRV / TXT / A for what is still missing."""
        out = [(self.service, T_PTR)]
        for inst in sorted(self.instances):
            if inst.lower() not in self.srv:
                out.append((inst, T_SRV))
            if inst.lower() not in self.txt:
                out.append((inst, T_TXT))
            host = self.srv.get(inst.lower(), ("", 0))[0]
            if host and host.lower() not in self.a:
                out.append((host, T_A))
        return out

    def found(self) -> list[Found]:
        out = []
        for inst in sorted(self.instances):
            host, port = self.srv.get(inst.lower(), ("", 0))
            txt = self.txt.get(inst.lower(), {})
            label = inst[:-len(self.service) - 1] if inst.lower().endswith("." + self.service) else inst
            out.append(Found(instance=label.replace("\\032", " "), unit_id=txt.get("unit_id") or None, host=host,
                             port=port, addresses=list(self.a.get(host.lower(), [])), txt=txt))
        return out


_SIOCGIFADDR = 0x8915                      # Linux


def interface_addresses() -> list[str]:
    """This machine's IPv4 interface addresses, loopback included (one per interface where it can tell): ifaddr when
    installed (python-zeroconf's dependency: the mdns extra), else on Linux each interface's address (SIOCGIFADDR over
    socket.if_nameindex), else the addresses the host name resolves to (Windows lists every adapter's there) and
    127.0.0.1."""
    out: list[str] = []

    def add(ip: str) -> None:
        if ip and ip not in out and not ip.startswith("169.254."):   # link-local fallback: no DHCP, nothing there
            out.append(ip)
    try:
        import ifaddr
        for adapter in ifaddr.get_adapters():
            for ip in adapter.ips:
                if ip.is_IPv4:
                    add(ip.ip)
    except ImportError:
        pass
    if not out and sys.platform.startswith("linux"):
        import fcntl
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            for _, name in socket.if_nameindex():
                try:
                    raw = fcntl.ioctl(s.fileno(), _SIOCGIFADDR, struct.pack("256s", name.encode()[:15]))
                except OSError:
                    continue                                   # no IPv4 address
                add(socket.inet_ntoa(raw[20:24]))
        finally:
            s.close()
    if not out:
        try:
            for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
                add(ip)
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                add(info[4][0])
        except OSError:
            pass
    add("127.0.0.1")
    return out


def send_query(sock: socket.socket, packet: bytes, interfaces: list[str]) -> int:
    """`packet` to the mDNS group out of each interface (IP_MULTICAST_IF = its address); how many sends went out. An
    interface that refuses (no multicast, down) is skipped; with none at all, one send by the default route."""
    sent = 0
    for ip in interfaces:
        try:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(ip))
            sock.sendto(packet, (MDNS_GROUP, MDNS_PORT))
            sent += 1
        except OSError:
            pass
    if not sent:
        try:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton("0.0.0.0"))
            sock.sendto(packet, (MDNS_GROUP, MDNS_PORT))
            sent = 1
        except OSError:
            pass                                               # no route for multicast: nothing to find
    return sent


def _sockets(interfaces: list[str]) -> list[socket.socket]:
    """The query socket (ephemeral port: legacy unicast answers come here) and, when 5353 can be shared, one on the
    group for multicast answers."""
    q = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    q.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
    q.bind(("", 0))
    socks, g = [q], None
    try:
        g = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        g.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                g.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
        g.bind(("", MDNS_PORT))
        joined = 0
        for ip in interfaces or ["0.0.0.0"]:
            try:
                g.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                             socket.inet_aton(MDNS_GROUP) + socket.inet_aton(ip))
                joined += 1
            except OSError:
                pass
        if not joined:
            raise OSError("no interface joined the group")
        socks.append(g)
    except OSError:                                            # 5353 taken (avahi without sharing): unicast answers only
        if g is not None:
            g.close()
    return socks


def _browse_minimal(timeout: float, service: str = SERVICE) -> list[Found]:
    b = Browser(service)
    interfaces = interface_addresses()
    socks = _sockets(interfaces)
    try:
        deadline = time.monotonic() + timeout
        next_send, gap = 0.0, 0.25
        while True:
            now = time.monotonic()
            if now >= deadline:
                break
            if now >= next_send:
                send_query(socks[0], query(b.questions(), secrets.randbits(16)), interfaces)
                next_send, gap = now + gap, min(gap * 2, 1.0)
            ready, _, _ = select.select(socks, [], [], max(0.0, min(next_send, deadline) - time.monotonic()))
            for s in ready:
                try:
                    b.feed(s.recv(9000))
                except OSError:
                    pass
    finally:
        for s in socks:
            s.close()
    return b.found()


def _browse_zeroconf(timeout: float, service: str = SERVICE) -> list[Found]:
    import zeroconf as zc_mod
    # every interface (InterfaceChoice.All, ifaddr's list): zeroconf joins and queries on each
    zc = zc_mod.Zeroconf(interfaces=zc_mod.InterfaceChoice.All, ip_version=zc_mod.IPVersion.V4Only)
    names: set[str] = set()

    class Listener(zc_mod.ServiceListener):
        def add_service(self, zc, type_, name):
            names.add(name)

        def update_service(self, zc, type_, name):
            names.add(name)

        def remove_service(self, zc, type_, name):
            names.discard(name)

    try:
        browser = zc_mod.ServiceBrowser(zc, service, Listener())
        time.sleep(timeout)
        browser.cancel()
        out = []
        for name in sorted(names):
            info = zc.get_service_info(service, name, timeout=int(max(500, timeout * 1000)))
            if info is None:
                continue
            txt = {k.decode("utf-8", "replace").lower(): (v or b"").decode("utf-8", "replace")
                   for k, v in (info.properties or {}).items()}
            label = name[:-len(service) - 1] if name.endswith("." + service) else name
            out.append(Found(instance=label, unit_id=txt.get("unit_id") or None, host=info.server or "",
                             port=info.port or 0, addresses=list(info.parsed_addresses()), txt=txt))
        return out
    finally:
        zc.close()


def have_zeroconf() -> bool:
    try:
        import zeroconf  # noqa: F401
    except ImportError:
        return False
    return True


def browse(timeout: float = 2.0, engine: str = "auto") -> list[Found]:
    """Every probe announcing `_oep._tcp` within `timeout` seconds. engine: "zeroconf" (python-zeroconf), "minimal"
    (this module's query) or "auto" (zeroconf when installed)."""
    if engine == "zeroconf" or (engine == "auto" and have_zeroconf()):
        return _browse_zeroconf(timeout)
    return _browse_minimal(timeout)


def find_unit(unit_id: str, timeout: float = 3.0, engine: str = "auto") -> Found:
    """The probe whose TXT unit_id is `unit_id` (ASCII case ignored). LookupError when none answers within `timeout`."""
    hits = [f for f in browse(timeout, engine) if (f.unit_id or "").lower() == unit_id.lower() and f.target]
    if not hits:
        raise LookupError(f"no probe with unit_id {unit_id} announces {SERVICE.rstrip('.')} on this network (DNS-SD "
                          f"over mDNS stays on the local link: behind a NAT name it as tcp://HOST:PORT)")
    return hits[0]


def port_of(host: str, timeout: float = 2.0, engine: str = "auto") -> int | None:
    """The SRV port of the probe announced on `host` (its host name, with or without .local, or one of its addresses);
    None when none is found."""
    want = host.lower().rstrip(".")
    for f in browse(timeout, engine):
        names = {f.host.lower().rstrip("."), f.host.lower().rstrip(".").removesuffix(".local")}
        if want in names or want in f.addresses:
            return f.port or None
    return None
