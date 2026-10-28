"""Round 5 review of debug §4-§7 (riscv-dm, arm-adi) and the console: code that required or did what a09fa45 does not
say."""
import struct

from oep_client import riscv, virtual_bench
from oep_client.conformance_attach import AttachChecks
from oep_client.conformance_debug import DebugChecks
from oep_client.conformance_run import RunChecks
from oep_client.virtual_core import tlv

from test_conformance_attach import setup as attach_setup
from test_conformance_debug import setup as debug_setup
from test_conformance_run import setup as run_setup
from test_host_rules_2026_10_06 import in_process


# ---- debug §4: "必須のopはdmiだけ。halt、resume、…は任意で、describeのopsで宣言する" -------------------------

def test_riscv_dm_declared_names_halt_and_resume_as_optional():
    """The client listed halt / resume as always there; a probe offering dmi only declares neither."""
    _, hst, _ = in_process(virtual_bench.esp32_v003())
    hst.open(3000)
    conn, _ = riscv.Wire(hst, "oep.wire.swio").attach(halt=True)
    dm = riscv.RiscvDm(hst, conn)
    assert {"halt", "resume"} <= dm.declared()
    dm.ops = lambda: {riscv.RiscvDm.DMI}
    assert dm.declared() == set()
    dm.ops = lambda: {riscv.RiscvDm.DMI, riscv.RiscvDm.HALT}
    assert dm.declared() == {"halt"}


def test_debug_checker_takes_a_dmi_only_fixture():
    """The checker required halt and resume (17, 18) as mandatory; a dmi-only fixture is conforming and its halt /
    resume cases are not run."""
    model, c, a = debug_setup()
    install = model.install

    def dmi_only():
        install()
        model.extension.describe = lambda fn, d=model.extension.describe: (tlv(7, b'\x10\x01'),) if fn == 5 else d(fn)
    model.install = dmi_only
    model.install()
    r = DebugChecks(c, a).run()
    assert r['status'] == 'passed', [(row['id'], row.get('error')) for row in r['checks'] if row['status'] != 'passed']
    ids = {row['id'] for row in r['checks']}
    assert 'OEP-DEBUG-CONTEXT' in ids and 'OEP-DEBUG-MIXED' in ids and 'OEP-DEBUG-HARTSEL-RAW' in ids
    assert not ids & {'OEP-DEBUG-HALT-RESUME', 'OEP-DEBUG-HALT-FAILURE', 'OEP-DEBUG-HARTSEL-HALT'}


# ---- console §1: "最小はopen/read/close/streamsで" (marks / clear / mark / write 任意) --------------------------

def test_attach_checker_takes_a_minimal_console():
    """The attach checker required every console op; marks, clear, mark and write are optional."""
    model, c, a = attach_setup()
    install = model.install

    def minimal():
        install()
        d = model.extension.describe
        # open 0x10, read 0x11, close 0x16, streams 0x17
        model.extension.describe = lambda fn: (tlv(7, b'\x10\xc3'),) + d(fn)[1:] if fn == 4 else d(fn)
    model.install = minimal
    model.install()
    r = AttachChecks(c, a).run()
    assert r['status'] == 'passed', [(row['id'], row.get('error')) for row in r['checks'] if row['status'] != 'passed']


# ---- debug §4 run row: "abstractauto と haltreq は戻す" (no order between DATA1 and DATA0) ----------------------

def test_run_checker_takes_data0_restored_before_data1():
    """The run checker required DATA1 to be written back before DATA0; the run row names no order."""
    model, c, a = run_setup()
    install = model.install

    def reversed_restore():
        install()
        e = model.extension
        dispatch = e.dispatch_request

        def run(fn, op, payload, tags):
            value = dispatch(fn, op, payload, tags)
            if fn == 5 and op == 22:
                rows = [i for i, row in enumerate(e.restore) if row[0] == 'run-restore' and row[2] in (4, 5)][-2:]
                e.restore[rows[0]], e.restore[rows[1]] = e.restore[rows[1]], e.restore[rows[0]]
            return value
        e.dispatch_request = run
    model.install = reversed_restore
    r = RunChecks(c, a).run()
    assert r['status'] == 'passed', [(row['id'], row.get('error')) for row in r['checks'] if row['status'] != 'passed']


# ---- debug §4: "host は、dmi の要求で…DATA0 / DATA1 を変えたら、hart を走らせる前に、変える前の値を書き戻す" ------

def test_reset_run_writes_data0_back_before_the_hart_runs():
    """reset mode 0 runs the hart: the DATA0 that read_register changed is written back first (it was left changed, and
    a later resume wrote the pre-reset value over the restarted firmware's mailbox)."""
    ep, hst, _ = in_process(virtual_bench.esp32_v003())
    hst.open(3000)
    conn, _ = riscv.Wire(hst, "oep.wire.swio").attach(halt=True)
    tg = ep._target_of(conn)
    tg.dmi[0x04] = 0x0000CAFE                      # the console's mailbox word
    tg.regs[0x100A] = 0x12345678
    dm = riscv.RiscvDm(hst, conn)
    assert dm.read_register(0x100A) == 0x12345678 and tg.dmi[0x04] == 0x12345678
    dm.reset()
    assert tg.dmi[0x04] == 0x0000CAFE and dm.data_saved is None
