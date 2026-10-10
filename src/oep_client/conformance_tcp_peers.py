"""Explicit two-connection TCP core contracts; no session requests change route."""
from contextlib import contextmanager
import secrets
import struct

from .conformance import Checks, Violation, require, tlvs
from .conformance_tcp import frame
from .conformance_serial import WireError


def lock_state(check):
    payload = check.success(check.request(check.core['lock_state']))
    require(len(payload) >= 5, 'lock_state fixed fields')
    held, remaining = struct.unpack_from('<BI', payload)
    owner = dict(reversed(tlvs(payload[5:]))).get(1, b'')
    require(held in (0, 1) and (remaining > 0 if held else remaining == 0), 'lock_state value')
    return held, remaining, owner


class Peers:
    def __init__(self, primary):
        self.primary = primary

    @contextmanager
    def peer(self):
        primary = self.primary
        peer = None
        try:
            wire = primary.tcp_peer_open()
        except Exception:
            primary.abort = True
            raise
        record = {'tcp_connection': 'peer', 'exchanges': []}
        primary.trace.append(record)
        try:
            peer = Checks(wire.send, primary.reg, primary.unit)
            peer.tcp_wire = wire
            record['exchanges'] = peer.trace
            # Confirm-only discovery until the handshake succeeds. A wrong endpoint must
            # never get open/force/end requests from any of these tests.
            try:
                peer.confirm()
                require(peer.boot == primary.boot, 'peer boot differs from primary')
                peer.identity()
                peer.transport_declarations()
                require(peer.observed['core_describe'] == primary.observed['core_describe'],
                        'core describe differs between TCP connections')
                entries = [{k: v for k, v in entry.items() if k != 'describe'}
                           for entry in primary.observed.get('interfaces', [])]
                require(peer.list_snapshot() == entries, 'interface list differs between TCP connections')
            except Exception:
                primary.abort = True
                raise
            primary.observed['tcp_peer'] = peer.observed
            yield peer, wire
        except (OSError, TimeoutError, WireError):
            primary.abort = True
            raise
        finally:
            wire.close()

    def identity(self):
        with self.peer():
            pass

    def lock(self):
        primary = self.primary
        with self.peer() as (peer, _):
            owner = ('tcp-' + secrets.token_hex(8)).encode()
            with primary.holding(60000, b'\x01' + struct.pack('<H', len(owner)) + owner) as sid:
                held, remaining, actual = lock_state(peer)
                require(held and actual == owner, 'peer cannot see shared lock/owner')
                other = secrets.randbelow(0xffffffff) + 1
                while other == sid:
                    other = secrets.randbelow(0xffffffff) + 1
                payload = peer.rejected(peer.request(peer.core['open'], struct.pack('<IB', 60000, 0),
                                                      session=other, corr=1), 'locked')
                require(dict(reversed(tlvs(payload[4:]))).get(1) == owner, 'locked did not expose shared owner')
                tlvs(primary.success(primary.request(primary.core['keepalive'], session=sid)))

    def silence(self, check, wire):
        replies = wire.exchange([], 0, silence=0.1)
        check.trace.append({'tcp_silence': wire.last_exchange,
                            'responses_hex': [reply.hex() for reply in replies]})
        require(not replies, 'response leaked to the wrong TCP connection')

    def route(self):
        primary = self.primary
        with self.peer() as (peer, wire):
            req, _ = primary.clock()
            self.silence(peer, wire)
            corr = struct.unpack_from('<H', req, 1)[0]
            peer.zero_corr = max(peer.zero_corr, corr)
            payload = peer.success(peer.request(peer.core['clock'], corr=corr))
            require(len(payload) >= 12 and int.from_bytes(payload[:4], 'little') == primary.boot,
                    'peer clock result')
            tlvs(payload[12:])
            self.silence(primary, primary.tcp_wire)

    def partial(self):
        primary = self.primary
        with self.peer() as (peer, _):
            req = primary.request(primary.core['clock'])
            encoded = frame(req)
            wire = primary.tcp_wire
            replies = wire.exchange([encoded[:1]], 0, silence=0.3)
            primary.trace.append({'tcp_partial_prefix': wire.last_exchange,
                                  'responses_hex': [reply.hex() for reply in replies]})
            if replies:
                primary.abort = True  # input remains partial; no following request is safe
            require(not replies, 'partial TCP header produced a response')
            try:
                peer.clock()  # Must work while the primary has an incomplete length field.
            except Exception:
                primary.abort = True  # primary input is still partial; never hide it with resync
                raise
            replies = wire.exchange([encoded[1:]], 1)
            primary.trace.append({'tcp_partial_suffix': wire.last_exchange,
                                  'responses_hex': [reply.hex() for reply in replies]})
            require(len(replies) == 1, 'partial frame completion produced duplicate results')
            saved = primary.send
            try:
                primary.send = lambda _: replies[0]
                payload = primary.success(req)
                require(len(payload) >= 12 and int.from_bytes(payload[:4], 'little') == primary.boot,
                        'primary clock result after peer request')
                tlvs(payload[12:])
            finally:
                primary.send = saved

    def close(self):
        primary = self.primary
        with self.peer() as (peer, _):
            owner = ('tcp-' + secrets.token_hex(8)).encode()
            with primary.holding(60000, b'\x01' + struct.pack('<H', len(owner)) + owner) as sid:
                old_boot = primary.boot
                old_describe = primary.observed['core_describe']
                old_transport = primary.confirm_transport
                primary.tcp_wire.close()
                primary.trace.append({'tcp_primary_closed': True})
                # Observe with session 0 on the other connection. No request belonging
                # to S is sent on the peer or the new primary connection.
                try:
                    state = lock_state(peer)
                    primary.tcp_reopen()
                    primary.confirm()
                    require(primary.boot == old_boot, 'primary restarted during TCP reopen')
                    primary.identity()
                    require(primary.confirm_transport == old_transport, 'reopen changed TCP listener index')
                    require(primary.observed['core_describe'] == old_describe, 'reopen changed core describe')
                except Exception:
                    primary.session = None
                    primary.abort = True
                    primary.trace.append({'cleanup_deferred': 'owned session on closed TCP connection expires by lease'})
                    raise
                if state[0] and state[2] != owner:
                    primary.session = None
                    primary.abort = True
                    raise Violation('shared lock owner changed; no force sent')
                other = secrets.randbelow(0xffffffff) + 1
                while other == sid:
                    other = secrets.randbelow(0xffffffff) + 1
                req = primary.request(primary.core['open'], struct.pack('<IB', 60000, int(bool(state[0]))),
                                      session=other, corr=1)
                try:
                    reply = primary.exchange(req)
                except Exception:
                    primary.session = None
                    primary.abort = True
                    primary.trace.append({'cleanup_deferred': 'takeover result unknown; no session cleanup on another route'})
                    raise
                if reply[3:5] == b'\x01\0':
                    primary.session, primary.corr = other, 1
                else:
                    # S's connection is gone. Do not send S's cleanup on a different
                    # connection or force an unrelated session during recovery.
                    primary.session = None
                    primary.abort = True
                require(reply[3:5] == b'\x01\0', 'takeover of our disconnected session failed')
                require(len(reply) >= 13 and struct.unpack_from('<II', reply, 5) == (60000, old_boot),
                        'takeover lease/boot')
                tlvs(reply[13:])
                require(state[0] and state[2] == owner, 'TCP close released the session/owner')

    def oversized(self):
        primary = self.primary
        with self.peer() as (peer, _):
            with primary.holding(60000) as sid:
                peer.tcp_case('oversized')  # only the peer must close
                tlvs(primary.success(primary.request(primary.core['keepalive'], session=sid)))
                require(lock_state(primary)[0], 'peer framing fault released primary lock')
                primary.clock(sid)
