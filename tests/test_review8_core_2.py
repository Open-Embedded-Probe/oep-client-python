"""Eighth review, core §4-§7 and oep.probe.plan: code divergences from oep-spec a09fa45 found and fixed.

- oep-if-plan §2.4 / core §7.4: the virtual bench took a uart's (or any non-i2c / spi fn's) role on a channel its
  role_channels offers for another role only ("bit i が立っていれば channel base+i をその role に使える";
  "channel が role_channels の候補に無い ... unsupported（受け取ったままの tag）").
- core §6.4 / §4.3: several checkers still took a held lock's remaining time 0 for a violation (lock_state: "ロックの有無と
  残り時間（ロックが空いていれば 0）"; locked: "残り時間 ms（u32）") - review 5 fixed it in Checks.exchange only.
"""

from __future__ import annotations

import inspect
import struct

from oep_client import (catalog, conformance, conformance_retention, conformance_session_smoke, conformance_tcp_peers,
                        core, endpoint, host as h, message as m, registry as reg, virtual_bench as vb)

PLAN = reg.PROBE_PLAN
APPLY = PLAN.op["plan_apply"]
ASSIGN = PLAN.tlv["plan_apply"]["role_assignment"]
UART, PLAN_FN = 5, 11                                     # esp32-v003's uart and plan fns


class Clock:
    t = 0

    def __call__(self):
        return self.t


# ---- plan §2.4 / core §7.4: a channel offered for another role only is unsupported ------------------------------------

def _uart_split_roles() -> vb.VirtualProbe:
    """esp32-v003 whose uart offers RX (role 1) on channel 4 only and TX (role 2) on channel 5 only."""
    p = vb.esp32_v003()
    offered = [vb._with(o, [t for t in o.tlvs if t[0] != catalog.ROLE_CHANNELS]
                        + [catalog.role_channels(1, [4]), catalog.role_channels(2, [5])]) if o.fn == UART else o
               for o in p.offered]
    return vb._again(p, offered)


def test_plan_apply_refuses_a_role_on_a_channel_role_channels_offers_another_role_only():
    ep = endpoint.Endpoint(_uart_split_roles(), Clock())
    hst = h.Host(lambda b: ep.handle(b, 0))
    hst.open(3000)
    for critical in (False, True):
        body = m.tlv(ASSIGN, struct.pack("<HBH", UART, 1, 5), critical=critical)   # RX on TX's channel
        r = m.Result.unpack(ep.handle(m.Request(hst.next_corr(hst.session), PLAN_FN, APPLY, body, hst.session).pack(), 0))
        assert (r.resolution, r.detail) == (m.REJECTED, m.UNSUPPORTED)
        assert r.payload[0] == (ASSIGN | m.TAG_CRITICAL if critical else ASSIGN)          # the tag as received
        assert not ep.plan
    core.plan_apply(hst, [(UART, 1, 4), (UART, 2, 5)])                                 # each role on its own: taken
    assert ep.plan == {(UART, 1, 4), (UART, 2, 5)}


# ---- core §6.4 / §4.3: a held lock's remaining time may read 0 -----------------------------------------------------------

class _Answers:
    """A checker stand-in answering lock_state with (1, 0): held, at the very end of its lease."""
    core = {"lock_state": reg.CORE.op["lock_state"]}

    def request(self, op, payload=b"", **_):
        return op

    def success(self, _request):
        return struct.pack("<BI", 1, 0)


def test_the_tcp_peer_checker_takes_lock_state_held_with_remaining_0():
    held, remaining, owner = conformance_tcp_peers.lock_state(_Answers())
    assert (held, remaining, owner) == (1, 0, b"")


class _SmokeCore:
    reg = {"reject_reasons": dict(reg.REJECT_REASONS)}
    observed: dict = {}

    def call(self, name, payload=b"", **_):
        return m.Result(1, m.COMPLETED, m.SUCCESS, struct.pack("<BI", 1, 0)).pack()

    def success(self, reply):
        return reply[5:]


def test_the_session_smoke_checker_takes_remaining_0_while_held():
    checks = conformance_session_smoke.SessionSmokeChecks(_SmokeCore(), wait_ms=lambda ms: None)
    checks.state(True)                                                       # lock_state (1, 0)
    checks.locked(m.Result(1, m.REJECTED, m.LOCKED, struct.pack("<I", 0)).pack())   # locked, 0 ms left


REG = {
    "roles": {"request": 1, "result": 2},
    "resolutions": {"rejected": 0, "completed": 1},
    "outcomes": {"success": 0, "failed": 1, "partial": 2},
    "reject_reasons": dict(reg.REJECT_REASONS),
    "describe_common": {"ops": 7},
    "limits": {"request_header_bytes": 10, "result_header_bytes": 5, "corr_max": 0xFFFF, "corr_half_range": 0x8000},
    "core": {"op": [{"name": k, "code": v} for k, v in reg.CORE.op.items()],
             "tlv": {"confirm_answer": {"transport": 1},
                     "describe": {"firmware": 64, "model": 65, "unit_id": 66, "channels": 67, "transport": 73,
                                  "chip": 76, "max_op_ms": 77}}},
    "interface": [],
}


def test_the_checkers_keepalive_case_takes_lock_state_held_with_remaining_0():
    ep = endpoint.Endpoint(vb.esp32_v003(), Clock())
    ep._remaining = lambda: 0                                 # every held lock read at the very end of its lease
    checks = conformance.Checks(lambda b: ep.handle(b, 0), REG, vb.unit_id_of(vb.esp32_v003()))
    checks.confirm()
    checks.keepalive()


def test_the_retention_owner_checks_take_remaining_0():
    src = inspect.getsource(conformance_retention.RetentionChecks.owner_lifecycle)
    assert "0 < remaining" not in src and "0 < struct.unpack_from('<I', refused)" not in src


# ---- core §4.4: a request whose bound is max_op_ms waits for max_op_ms, read first when not known yet ------------------

def test_a_lock_free_interface_request_waits_max_op_ms_even_before_the_host_read_it():
    """core §4.4: "host は要求ごとに少なくとも次を待つ: 要求全体の処理時間の上限 + 1000 ms + 転送の時間。処理時間の上限は
    max_op_ms" - core_quick_op_max_ms stands in only for fn 0's quick ops while max_op_ms is unknown. The host used it
    for any request (a gpio read before any session: 2 s instead of max_op_ms + 1 s)."""
    ep = endpoint.Endpoint(vb.esp32_v003(), Clock())
    seen = []

    def send(raw):
        req = m.Request.unpack(raw)
        seen.append((req.fn, req.op, hst.expect_ms))
        return ep.handle(raw, 0)

    hst = h.Host(send)
    hst.confirm()                                             # as a link does when it binds the host
    gpio = core.find(hst, "oep.fixture.gpio")                 # list: a quick fn 0 op
    read = reg.FIXTURE_GPIO.op["read"]
    try:
        hst.request(gpio, read, b"\x01\x04\x00", locked=False)   # unplanned: refused, but waited for as long
    except h.Unavailable:
        pass
    assert seen[-1] == (gpio, read, vb.MAX_OP_MS) and vb.MAX_OP_MS > h.QUICK_OP_MAX_MS
    assert [s[2] for s in seen if s[0] == m.CORE_FN] == [h.QUICK_OP_MAX_MS] * (len(seen) - 1)   # the quick ops' bound
