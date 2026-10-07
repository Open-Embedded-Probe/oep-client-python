"""Raw DMI groups over a debug link that drops (a CH32L103 behind an RVSWD probe, about 0.7 - 2 ms after a change of
hart state): writes are lost and reads give the last value read or all ones, with nothing in the answer to say so.

The fake drops the link at one DMI access (FakeTarget.drop_at; "glitch" / "glitch_parity": one access missed, the link up
again at once, a write missed with cmderr 6 set for "glitch_parity") and keeps it down to the end of that request (or for
drop_requests requests). Every access of each helper is hit in turn, stale and all ones: the code before the held
groups (copied here as old_*) returned a wrong value - or resumed into the application - without an error; the held
groups (RiscvDm.held) return the right value or raise LinkNotHeld, never a wrong one."""

import random

import pytest

from oep_client import ch32_flash, endpoint, fake, host, riscv, uiapduino

S0 = 0x1008
VALUE = 0x000EC8FE         # the register's value (the bench's a0)
STALE = 0x20000000         # what DATA0 held before (the value the bench read back for a0)
MODES = ("stale", "ones", "ones_line")
READ_ACCESSES = 19         # read_register's DMI accesses on a link that stays up, its DATA0 save included


class Clock:
    def __init__(self):
        self.ms = 0

    def __call__(self):
        return self.ms


def bench():
    ep = endpoint.Endpoint(fake.p4_x035(), Clock())
    hst = host.Host(ep.handle, rng=random.Random(1))
    hst.open(lease_ms=10000)
    conn, _ = riscv.Wire(hst).attach(halt=True)
    tg = ep.target
    tg.regs[S0] = VALUE
    tg.dmi[riscv.DATA0] = STALE
    d = riscv.RiscvDm(hst, conn)
    d.dmi([d.step_read(riscv.DATA0)])        # the last value read is DATA0's old word
    tg.dmi_accesses = 0
    return ep, hst, tg, d


@pytest.fixture(autouse=True)
def no_pause(monkeypatch):
    monkeypatch.setattr(riscv, "HELD_RETRY_S", 0)


# ---- the code before the held groups, as it was --------------------------------------------------------------------

def old_read_register(d: riscv.RiscvDm, regno: int) -> int:
    _, values = d.dmi([d.step_write(0x17, 0x00220000 | regno), d.step_poll(0x16, 1 << 12, 0, 100),
                       d.step_read(0x04)])
    cs, data0 = values[0], values[1]
    if (cs >> 8) & 7:
        d.dmi([d.step_write(0x16, 0x700)])
        raise RuntimeError(f"abstract command for register {regno:#x} failed (cmderr {(cs >> 8) & 7})")
    return data0


def old_run_payload_registers(d: riscv.RiscvDm) -> None:
    def write(regno, value):
        return (d.step_write(0x04, value) + d.step_write(0x17, 0x00230000 | regno)
                + d.step_poll(0x16, 1 << 12, 0, 100))
    d.dmi(write(uiapduino.MSTATUS, 0) + write(uiapduino.DPC, uiapduino.PAYLOAD_BASE)
          + d.step_write(0x10, 0x40000001) + d.step_write(0x10, 0x40000001) + d.step_write(0x10, 0x00000001))


def new_run_payload_registers(d: riscv.RiscvDm) -> None:
    """uiapduino.run_payload past its attach and payload placing (those are probe block ops, held by the probe)."""
    d.write_register(uiapduino.MSTATUS, 0)
    d.write_register(uiapduino.DPC, uiapduino.PAYLOAD_BASE)
    mstatus, dpc = d.read_register(uiapduino.MSTATUS), d.read_register(uiapduino.DPC)
    if mstatus & uiapduino.MSTATUS_MIE or dpc != uiapduino.PAYLOAD_BASE:
        raise RuntimeError("the payload's registers did not take")
    d.dmi(d.step_write(0x10, 0x40000001) + d.step_write(0x10, 0x40000001) + d.step_write(0x10, 0x00000001))


def accesses(op) -> int:
    """The DMI accesses `op` makes over a link that stays up (and that it gets the right result then)."""
    ep, hst, tg, d = bench()
    op(d, tg)
    return tg.dmi_accesses


def sweep(op, check, modes=MODES, drop_requests=1, skip_last=0) -> dict:
    """Drop the link at each access of `op` in turn, in each mode. -> {"right": [...], "wrong": [...], "raised": [...]}
    of (mode, position[, what]); `check(tg, result)` says whether a result that came back is right."""
    n = accesses(op) - skip_last
    out = {"right": [], "wrong": [], "raised": []}
    for mode in modes:
        for at in range(n):
            ep, hst, tg, d = bench()
            tg.drop_at, tg.drop_requests = {at: mode}, drop_requests
            try:
                result = op(d, tg)
            except (host.OepError, RuntimeError) as e:
                out["raised"].append((mode, at, type(e).__name__))
                continue
            out["right" if check(tg, result) else "wrong"].append((mode, at, result))
    return out


# ---- read_register ---------------------------------------------------------------------------------------------------

def test_a_link_that_stays_up_reads_the_register_both_ways():
    ep, hst, tg, d = bench()
    assert old_read_register(d, S0) == VALUE and d.read_register(S0) == VALUE
    assert d.read_register(d.DPC) == tg.dpc


def test_the_old_read_register_returned_a_stale_or_all_ones_value_as_the_register():
    got = sweep(lambda d, tg: old_read_register(d, S0), lambda tg, v: v == VALUE)
    wrong = {(mode, v) for mode, _, v in got["wrong"]}
    assert ("stale", STALE) in wrong                         # the command was lost: DATA0's old word, no error
    assert ("ones", 0xFFFFFFFF) in wrong                     # the DATA0 read met the drop
    assert ("ones_line", 0xFFFFFFFF) in wrong                # only a DMSTATUS read fails on the line: none here


def test_a_held_read_register_never_returns_a_value_it_could_not_confirm():
    got = sweep(lambda d, tg: d.read_register(S0), lambda tg, v: v == VALUE)
    # DATA0 saved first, held (look 2, DATA0, look 2: debug §4, restored before the hart runs); then look 2, cmderr
    # clear, (DATA0 sentinel, command, poll 1, DATA0) twice with a DMSTATUS read before the second DATA0, look 2
    assert accesses(lambda d, tg: d.read_register(S0)) == READ_ACCESSES == 5 + 14
    assert not got["wrong"] and not got["raised"]            # every drop met, and the next try got the register
    assert len(got["right"]) == READ_ACCESSES * len(MODES)


def held_once_read_register(d: riscv.RiscvDm, regno: int) -> int:
    """read_register as af3789a had it: one command between the looks."""
    cs, data0 = d.held(d._abstract(0x00220000 | regno) + d.step_read(riscv.DATA0), f"read_register {regno:#x}")
    d._cmderr(cs, "read_register")
    return data0


def test_one_missed_access_between_passing_looks_never_gives_a_wrong_register():
    """A link coming back from a drop through a flicker: one access missed - a write lost, or a read giving the value
    read before it - with the looks around it passing (bench, oep-probe-arduino 0.0.29-dev+bd19b00, tests/hw test_wire
    on a CH32L103 through an RVSWD probe: a0 read as s1's value after a read_block). The one-command group took the old
    DATA0 or the poll's ABSTRACTCS for the register; reading it twice over two sentinels does not."""
    old = sweep(lambda d, tg: held_once_read_register(d, S0), lambda tg, v: v == VALUE, modes=("glitch",))
    assert {v for _, _, v in old["wrong"]} >= {STALE}         # the command lost: DATA0's old word, looks passing
    new = sweep(lambda d, tg: d.read_register(S0), lambda tg, v: v == VALUE, modes=("glitch",))
    assert not new["wrong"] and not new["raised"] and len(new["right"]) == READ_ACCESSES
    # two misses in one group, at every pair of accesses: never a wrong register (agreeing twice by chance aside -
    # a sentinel and the register, or ABSTRACTCS and DMSTATUS, would have to be the same value)
    wrong = []
    for a in range(READ_ACCESSES):
        for b in range(a + 1, READ_ACCESSES):
            ep, hst, tg, d = bench()
            tg.drop_at = {a: "glitch", b: "glitch"}
            try:
                v = d.read_register(S0)
            except (host.OepError, RuntimeError):
                continue
            if v != VALUE:
                wrong.append((a, b, v))
    assert not wrong


def test_a_drop_that_does_not_end_raises_link_not_held():
    got = sweep(lambda d, tg: d.read_register(S0), lambda tg, v: v == VALUE, drop_requests=riscv.HELD_TRIES)
    assert not got["wrong"] and not got["right"]
    assert {name for _, _, name in got["raised"]} == {"LinkNotHeld"}


def test_link_not_held_says_what_and_how_often():
    ep, hst, tg, d = bench()
    tg.drop_at, tg.drop_requests = {0: "stale"}, 99
    with pytest.raises(riscv.LinkNotHeld) as e:
        d.read_register(S0)
    assert e.value.tries == riscv.HELD_TRIES and "saving DATA0" in str(e.value)   # its first group: DATA0's save
    ep, hst, tg, d = bench()
    d.read_register(S0)                                          # DATA0 saved: the next read is the command group alone
    tg.dmi_accesses = 0
    tg.drop_at, tg.drop_requests = {0: "stale"}, 99
    n = len(ep.requests)
    with pytest.raises(riscv.LinkNotHeld) as e:
        d.read_register(S0)
    assert e.value.tries == riscv.HELD_TRIES and "read_register 0x1008" in str(e.value)
    dmi = [r for r in ep.requests[n:] if (r.fn, r.op) == (d.fn, riscv.RiscvDm.DMI)]
    assert len(dmi) == riscv.HELD_TRIES                          # every try, nothing more


def test_a_drop_lasting_fewer_requests_than_the_tries_is_ridden_out():
    ep, hst, tg, d = bench()
    tg.drop_at, tg.drop_requests = {3: "ones"}, riscv.HELD_TRIES - 1
    assert d.read_register(S0) == VALUE


def test_the_looks_tell_a_dropped_link_from_a_held_one():
    assert riscv.link_held(0x00000382, 0x00000001)
    assert riscv.link_held(0x00400383, 0x80000001)              # version 3, haltreq kept
    assert not riscv.link_held(0xFFFFFFFF, 0xFFFFFFFF)          # all ones
    assert not riscv.link_held(0x00000382, 0x00000382)          # stale: DMSTATUS's value read again for DMCONTROL
    assert not riscv.link_held(0x00000001, 0x00000001)          # stale: DMCONTROL's value read again for DMSTATUS
    assert not riscv.link_held(0x00000302, 0x00000001)          # not authenticated
    assert not riscv.link_held(0x00000381, 0x00000001)          # version 1: no module scan would find
    assert not riscv.link_held(0x00000382, 0x00000041)          # hart 1 selected
    assert not riscv.link_held(0x00000382, 0x00000000)          # not active


def test_a_cmderr_is_cleared_and_raised_and_one_left_behind_does_not_stop_the_command():
    ep, hst, tg, d = bench()
    tg.dmi[riscv.ABSTRACTCS] = 2 << 8                            # an earlier session's cmderr: the old code's command
    with pytest.raises(RuntimeError, match="cmderr 2"):          # was ignored and it raised
        old_read_register(d, S0)
    tg.dmi[riscv.ABSTRACTCS] = 2 << 8
    assert d.read_register(S0) == VALUE                          # the group clears it first: a redo runs the same
    tg.halted = False                                            # a running hart: cmderr 4
    with pytest.raises(RuntimeError, match="cmderr 4"):
        d.read_register(S0)
    assert tg.dmi[riscv.ABSTRACTCS] >> 8 & 7 == 0                # cleared before raising


def test_a_group_that_keeps_failing_on_the_line_is_tried_then_raised():
    ep, hst, tg, d = bench()
    tg.fail_write.add(riscv.COMMAND)                             # line: a drop can cause it, so it is tried again
    with pytest.raises(riscv.LinkNotHeld) as e:
        d.read_register(S0)
    assert isinstance(e.value.last, riscv.StepListError) and e.value.last.status == riscv.STATUS["line"]


# ---- cmderr 6: a frame the module took for one with a bad parity (QingKe) - a missed access, not a failed command ----

def old_cmderr_read_register(d: riscv.RiscvDm, regno: int) -> int:
    """read_register before cmderr 6 counted as a failed try: any cmderr cleared and raised."""
    what = f"read_register {regno:#x}"
    command = 0x00220000 | regno
    poll = d.step_poll(riscv.ABSTRACTCS, 1 << 12, 0, 100)
    steps = (d.step_write(riscv.ABSTRACTCS, 0x700)
             + d.step_write(riscv.DATA0, riscv.READ_SENTINELS[0]) + d.step_write(riscv.COMMAND, command) + poll
             + d.step_read(riscv.DATA0)
             + d.step_write(riscv.DATA0, riscv.READ_SENTINELS[1]) + d.step_write(riscv.COMMAND, command) + poll
             + d.step_read(riscv.DMSTATUS) + d.step_read(riscv.DATA0))
    cs1, data0, cs2, _, _ = d.held(steps, what, agree=lambda v: bool((v[0] | v[2]) & 0x700) or v[1] == v[4])
    d._cmderr(cs1 | cs2, what)
    return data0


def _no_cmderr(tg) -> bool:
    return not (tg.dmi.get(riscv.ABSTRACTCS, 0) >> 8) & 7


def test_cmderr_6_is_a_failed_try_and_the_group_is_run_again():
    """A write missed with cmderr 6 set (QingKe's "parity bit error": the module ignored the frame) at every access of
    read_register and write_register (bench, oep-probe-arduino 0.0.29-dev+f594f04, tests/hw test_wire on a CH32L103
    through an RVSWD probe: "read_register 0x100a / 0x1009 / 0x100b failed (cmderr 6)", 3 of 8 runs). It raised
    RuntimeError; now the group is cleared and run again within the tries: the right value, no cmderr left."""
    old = sweep(lambda d, tg: old_cmderr_read_register(d, S0), lambda tg, v: v == VALUE, modes=("glitch_parity",))
    assert ("RuntimeError" in {name for _, _, name in old["raised"]})
    new = sweep(lambda d, tg: (d.read_register(S0), _no_cmderr(tg)), lambda tg, v: v == (VALUE, True),
                modes=("glitch_parity",))
    assert not new["wrong"] and not new["raised"] and new["right"]

    def write(d, tg):
        d.write_register(S0, 0x13572468)
        return tg.regs[S0], _no_cmderr(tg)
    got = sweep(write, lambda tg, v: v == (0x13572468, True), modes=("glitch_parity",))
    assert not got["wrong"] and not got["raised"] and got["right"]


def test_cmderr_6_twice_in_a_read_never_gives_a_wrong_register():
    n = accesses(lambda d, tg: d.read_register(S0))
    wrong, raised = [], []
    for a in range(n):
        for b in range(a + 1, n):
            ep, hst, tg, d = bench()
            tg.drop_at = {a: "glitch_parity", b: "glitch_parity"}
            try:
                v = d.read_register(S0)
            except (host.OepError, RuntimeError) as e:
                raised.append((a, b, type(e).__name__))
                continue
            if v != VALUE:
                wrong.append((a, b, v))
    # never a wrong value nor an error (two misses may hide each other's cmderr 6 - a sentinel write lost and the poll
    # after it read stale - with DATA0 still the first command's right value; the next group clears it first)
    assert not wrong and not raised


def test_cmderr_6_at_every_try_raises_link_not_held_and_leaves_no_cmderr():
    ep, hst, tg, d = bench()

    def parity(command):                                         # every command's frame taken for a bad one
        tg.dmi[riscv.ABSTRACTCS] = tg.dmi.get(riscv.ABSTRACTCS, 0) | (riscv.CMDERR_PARITY << 8)
    tg.abstract = parity
    with pytest.raises(riscv.LinkNotHeld) as e:
        d.read_register(S0)
    assert e.value.tries == riscv.HELD_TRIES and _no_cmderr(tg)
    with pytest.raises(riscv.LinkNotHeld):
        d.write_register(S0, 1)
    assert _no_cmderr(tg)


# ---- write_register and uiapduino.run_payload's registers --------------------------------------------------------------

def _payload_right(tg, _):
    return tg.dpc == uiapduino.PAYLOAD_BASE and tg.regs.get(uiapduino.MSTATUS, 1) == 0 and not tg.halted


def _with_app_mstatus(start):
    def op(d, tg):
        tg.regs[uiapduino.MSTATUS] = 0x88                        # MIE set, as the halted application had it
        return start(d)
    return op


def test_the_old_payload_start_left_the_hart_off_the_payload_without_an_error():
    got = sweep(_with_app_mstatus(old_run_payload_registers), _payload_right, skip_last=3)
    assert [mode for mode, _, _ in got["wrong"]].count("stale") >= 2    # a lost write, a stale "done", no error


def test_held_payload_registers_are_right_or_raise_at_every_drop():
    op = _with_app_mstatus(new_run_payload_registers)
    # the last three accesses are the resume's DMCONTROL writes: a change of hart state, not held (a look after it
    # could not pass on such a link), left out of the sweep
    got = sweep(op, _payload_right, skip_last=3)
    assert not got["wrong"] and got["right"]
    got = sweep(op, _payload_right, skip_last=3, drop_requests=99)
    assert not got["wrong"] and {name for _, _, name in got["raised"]} == {"LinkNotHeld"}


def test_run_payload_places_and_starts_the_payload_on_the_fake():
    ep, hst, tg, d = bench()
    wire = riscv.Wire(hst)
    tg.regs[uiapduino.MSTATUS] = 0x88
    tg.drop_at = {tg.dmi_accesses + 9: "stale"}                  # inside the dpc write's group
    uiapduino.run_payload(hst, wire, [0x0000006F])
    assert tg.dpc == uiapduino.PAYLOAD_BASE and tg.regs[uiapduino.MSTATUS] == 0 and not tg.halted
    assert tg.lost_writes                                        # the drop was met, and the group run again


# ---- ch32_flash.resume reads dpc right after a resume (a change of state) --------------------------------------------

def test_ch32_resume_does_not_take_a_dropped_dpc_read_for_a_move(monkeypatch):
    def op(d, tg):
        tg.resume_misses = 99                                    # it never goes: resume must say False
        return ch32_flash.resume(d, tries=1)

    def right(tg, went):
        return went is False and tg.halted

    with monkeypatch.context() as mp:
        mp.setattr(riscv.RiscvDm, "read_register", old_read_register)
        first = accesses(lambda d, tg: d.read_register(d.DPC))  # the dpc read before the resume; then the one after
        old = sweep(op, right)
    assert [at for mode, at, went in old["wrong"] if mode == "ones" and went is True and at >= first]
    new = sweep(op, right)                                       # (all ones read for dpc: "it moved", so it went)
    assert not new["wrong"] and not new["raised"] and len(new["right"]) == accesses(op) * len(MODES)
