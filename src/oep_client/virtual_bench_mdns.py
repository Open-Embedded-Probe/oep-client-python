"""The virtual bench announcing its TCP port: DNS-SD `_oep._tcp` over mDNS, as a probe listening on TCP does (oep-spec
transports §3), so another host's tests can find it the way they find a real probe (`virtual_bench_serve --announce`).

    ann = Announcement(unit_id="fafe00000003", port=7450, addresses=["127.0.0.1"])
    r = start(ann, engine="auto", interfaces=None)          # None: every IPv4 interface, and loopback where it can join
    ... select on r.watch(), r.poll(readable) ...
    r.close()                                              # goodbye (TTL 0) and the sockets closed

What is announced (the reference probe's form, with names of the virtual bench's own):
- PTR `_oep._tcp.local.` -> instance `OEP virtual <unit_id> <port>._oep._tcp.local.` (the port in it: two benches with the
  same unit_id on one host keep apart);
- SRV of the instance: the listening port, host `oep-virtual-<unit_id>-<port>.local.`;
- TXT of the instance: `unit_id=<unit_id>` (fn 0's describe's);
- A of the host: the address(es) the TCP port can be reached at;
- PTR `_services._dns-sd._udp.local.` -> `_oep._tcp.local.` (service type enumeration, RFC 6763 §9).

Two responders, the same records:
- python-zeroconf (the `mdns` extra) when installed: a full mDNS responder (probing, announcing, goodbye).
- otherwise a minimal one of this module's own (IPv4 only): one UDP socket on port 5353 (shared: SO_REUSEADDR /
  SO_REUSEPORT), the group 224.0.0.251 joined on each chosen interface. A query from a port other than 5353 is a legacy
  unicast query (RFC 6762 §6.7): the answer goes back to its sender's address and port, with the query's ID, its
  questions repeated, TTL 10 and no cache-flush bit - a one-shot resolver, a CI's own query, `dig -p 5353 @224.0.0.251`.
  A query from port 5353 (a full mDNS querier) is answered on the group, on every chosen interface (a QU question too:
  the multicast answer always reaches it, a unicast one to 5353 may land on another socket sharing the port). It sends
  one announcement at start and a goodbye at close; it does no probing nor conflict resolution, no known-answer
  suppression and no IPv6.
"""

from __future__ import annotations

import socket
import struct
import sys

from .discovery import (MDNS_GROUP, MDNS_PORT, SERVICE, T_A, T_PTR, T_SRV, T_TXT, QU, _read_name, encode_name,
                        interface_addresses)

T_ANY = 255
META = "_services._dns-sd._udp.local."
CACHE_FLUSH = 0x8000
TTL, LEGACY_TTL = 120, 10                  # host records' TTL (RFC 6762 §10); legacy unicast answers at most 10 s (§6.7)
_IP_MULTICAST_ALL = 49                     # Linux: only the groups this socket joined


class Announcement:
    """The records of one announced probe."""

    def __init__(self, unit_id: str, port: int, addresses: list[str], instance: str | None = None,
                 host: str | None = None, service: str = SERVICE):
        self.unit_id, self.port, self.addresses = unit_id, port, list(addresses)
        self.service = service if service.endswith(".") else service + "."
        self.label = instance or f"OEP virtual {unit_id} {port}"
        self.instance = f"{self.label}.{self.service}"
        self.host = host or f"oep-virtual-{unit_id}-{port}.local."
        if not self.host.endswith("."):
            self.host += "."

    def txt(self) -> bytes:
        entry = f"unit_id={self.unit_id}".encode()
        return bytes([len(entry)]) + entry

    def ptr(self) -> tuple:
        return (self.service, T_PTR, encode_name(self.instance), False)

    def srv(self) -> tuple:
        return (self.instance, T_SRV, struct.pack(">HHH", 0, 0, self.port) + encode_name(self.host), True)

    def txt_rr(self) -> tuple:
        return (self.instance, T_TXT, self.txt(), True)

    def a(self) -> list[tuple]:
        return [(self.host, T_A, socket.inet_aton(ip), True) for ip in self.addresses]

    def meta(self) -> tuple:
        return (META, T_PTR, encode_name(self.service), False)

    def all(self) -> list[tuple]:
        return [self.ptr(), self.srv(), self.txt_rr(), *self.a()]

    def answer(self, questions: list[tuple[str, int, bool]]) -> tuple[list[tuple], list[tuple]]:
        """(answers, additional records) for these (name, type, unicast) questions; both empty when none is ours."""
        answers, extra = [], []
        for name, qtype, _ in questions:
            key = name.lower()
            if key == self.service.lower() and qtype in (T_PTR, T_ANY):
                answers.append(self.ptr())
                extra += [self.srv(), self.txt_rr(), *self.a()]
            elif key == META and qtype in (T_PTR, T_ANY):
                answers.append(self.meta())
            elif key == self.instance.lower():
                if qtype in (T_SRV, T_ANY):
                    answers.append(self.srv())
                    extra += self.a()
                if qtype in (T_TXT, T_ANY):
                    answers.append(self.txt_rr())
            elif key == self.host.lower() and qtype in (T_A, T_ANY):
                answers += self.a()
        answers = list(dict.fromkeys(answers))
        extra = [r for r in dict.fromkeys(extra) if r not in answers]
        return answers, extra


def parse_query(packet: bytes) -> tuple[int, list[tuple[str, int, bool]], int] | None:
    """A query -> (ID, [(name, type, unicast wanted)], the offset past the questions); None for a response or a
    packet that is no query of class IN / ANY."""
    if len(packet) < 12:
        return None
    qid, flags, qd = struct.unpack_from(">HHH", packet)
    if flags & 0x8000 or flags & 0x7800:                      # a response, or an opcode other than QUERY
        return None
    at, out = 12, []
    try:
        for _ in range(qd):
            name, at = _read_name(packet, at)
            qtype, qclass = struct.unpack_from(">HH", packet, at)
            at += 4
            if qclass & 0x7FFF in (1, 255):
                out.append((name, qtype, bool(qclass & QU)))
    except (ValueError, struct.error):
        return None
    return qid, out, at


def response(answers: list[tuple], extra: list[tuple], qid: int = 0, question: bytes = b"", qdcount: int = 0,
             legacy: bool = False, ttl: int | None = None) -> bytes:
    """A response packet: multicast form (ID 0, no questions, cache-flush on unique records) or, `legacy`, the legacy
    unicast form (the query's ID and questions, TTL at most 10, no cache-flush bit). `ttl` 0 is a goodbye."""
    def rr(rec):
        name, rtype, rdata, unique = rec
        t = ttl if ttl is not None else (min(TTL, LEGACY_TTL) if legacy else TTL)
        cls = 1 | (CACHE_FLUSH if unique and not legacy else 0)
        return encode_name(name) + struct.pack(">HHIH", rtype, cls, t, len(rdata)) + rdata
    head = struct.pack(">HHHHHH", qid if legacy else 0, 0x8400, qdcount if legacy else 0, len(answers), 0, len(extra))
    return head + (question if legacy else b"") + b"".join(rr(r) for r in answers + extra)


def reachable_addresses(listen: str, interfaces: list[str]) -> list[str]:
    """The A records for a TCP port listening on `listen`: that address, or for a wildcard listen the chosen
    interfaces' addresses, loopback last (a host on another machine takes the first)."""
    if listen not in ("", "0.0.0.0"):
        return [listen]
    return sorted(interfaces, key=lambda ip: ip.startswith("127."))


# ---- the minimal responder -------------------------------------------------------------------------------------------

class MinimalResponder:
    """Answers mDNS / DNS-SD queries for one Announcement on the given interfaces (IPv4 addresses; None: every one
    this machine has, loopback included where the OS lets it join)."""

    engine = "minimal"

    def __init__(self, ann: Announcement, interfaces: list[str] | None = None):
        self.ann = ann
        s = self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
        if sys.platform.startswith("linux"):
            try:
                s.setsockopt(socket.IPPROTO_IP, getattr(socket, "IP_MULTICAST_ALL", _IP_MULTICAST_ALL), 0)
            except OSError:
                pass
        try:
            s.bind(("", MDNS_PORT))
        except OSError as e:
            s.close()
            raise OSError(e.errno, f"UDP port {MDNS_PORT} cannot be shared ({e.strerror}): another mDNS responder holds "
                                   "it alone - install the mdns extra (python-zeroconf) or stop that responder") from None
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
        self.joined: list[str] = []
        for ip in (interface_addresses() if interfaces is None else interfaces):
            try:
                s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                             socket.inet_aton(MDNS_GROUP) + socket.inet_aton(ip))
                self.joined.append(ip)
            except OSError:
                if interfaces is not None:
                    s.close()
                    raise OSError(f"cannot join {MDNS_GROUP} on {ip}") from None
        if not self.joined:
            s.close()
            raise OSError(f"no interface joined {MDNS_GROUP}: multicast is unavailable here")
        s.setblocking(False)
        self.answered = 0
        self._multicast(response(ann.all(), []))             # one announcement (RFC 6762 §8.3)

    def watch(self) -> list:
        return [self.sock]

    def poll(self, readable) -> None:
        if self.sock not in readable:
            return
        while True:
            try:
                packet, (addr, port) = self.sock.recvfrom(9000)
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                return
            self.handle(packet, addr, port)

    def handle(self, packet: bytes, addr: str, port: int) -> None:
        q = parse_query(packet)
        if q is None:
            return
        qid, questions, end = q
        answers, extra = self.ann.answer(questions)
        if not answers:
            return
        self.answered += 1
        if port != MDNS_PORT:                                 # legacy unicast (RFC 6762 §6.7)
            qd = struct.unpack_from(">H", packet, 4)[0]
            out = response(answers, extra, qid, packet[12:end], qd, legacy=True)
            try:
                self.sock.sendto(out, (addr, port))
            except OSError:
                pass
        else:
            self._multicast(response(answers, extra))

    def _multicast(self, packet: bytes) -> None:
        for ip in self.joined:
            try:
                self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(ip))
                self.sock.sendto(packet, (MDNS_GROUP, MDNS_PORT))
            except OSError:
                pass

    def close(self) -> None:
        if self.sock.fileno() < 0:
            return
        self._multicast(response(self.ann.all(), [], ttl=0))  # goodbye (RFC 6762 §10.1)
        self.sock.close()


# ---- python-zeroconf -------------------------------------------------------------------------------------------------

class ZeroconfResponder:
    """The same records registered with python-zeroconf (its own threads answer; watch / poll do nothing)."""

    engine = "zeroconf"

    def __init__(self, ann: Announcement, interfaces: list[str] | None = None):
        import zeroconf as zc_mod
        self.ann = ann
        self.joined = list(interfaces) if interfaces is not None else interface_addresses()
        self.zc = zc_mod.Zeroconf(interfaces=interfaces if interfaces is not None else zc_mod.InterfaceChoice.All,
                                  ip_version=zc_mod.IPVersion.V4Only)
        self.info = zc_mod.ServiceInfo(ann.service, ann.instance, port=ann.port,
                                       properties={"unit_id": ann.unit_id}, server=ann.host,
                                       addresses=[socket.inet_aton(ip) for ip in ann.addresses])
        try:
            self.zc.register_service(self.info, allow_name_change=True)
        except Exception:
            self.zc.close()
            raise
        ann.label = self.info.name[:-len(ann.service) - 1]   # a name change on a conflict
        ann.instance = self.info.name

    def watch(self) -> list:
        return []

    def poll(self, readable) -> None:
        pass

    def close(self) -> None:
        if self.zc is None:
            return
        try:
            self.zc.unregister_service(self.info)
        finally:
            self.zc.close()
            self.zc = None


def start(ann: Announcement, engine: str = "auto", interfaces: list[str] | None = None):
    """A running responder: engine "zeroconf", "minimal" or "auto" (zeroconf when installed)."""
    from .discovery import have_zeroconf
    if engine == "zeroconf" or (engine == "auto" and have_zeroconf()):
        return ZeroconfResponder(ann, interfaces)
    return MinimalResponder(ann, interfaces)
