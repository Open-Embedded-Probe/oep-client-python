"""Round 8 review of core §0-§3 and oep-if-restart (oep-spec a09fa45): divergences found and fixed."""
import struct

from oep_client import capture as c, message as m, riscv
from oep_client.conformance_restart import RestartChecks
from oep_client.conformance_restart_sample import SampleRestartAdapter

from test_conformance_restart import setup as restart_setup


def test_an_unknown_reference_how_is_shown_as_it_is():
    """core §2.4: 応答の enum の知らない値で失敗を知らせないもの（cause、scan の entry の kind）は、知らない値として見せる。
    capture §3.3's reference how (0 nominal, 1 measured): an unknown 2 was shown as nominal (False)."""
    def cfg(how):
        return c._config(m.tlv(c.REFERENCE, struct.pack("<BIB", 1, 1100, how)), analog=True).reference
    assert cfg(0) == ("internal", 1100, False) and cfg(1) == ("internal", 1100, True)
    assert cfg(2) == ("internal", 1100, "2") and cfg(0xFF)[2] == "255"


def test_an_unknown_run_stopped_is_shown_as_it_is():
    """core §2.4 (as above): debug §4.4's run stopped (0 timeout_halted, 1 stopped, 2 not_halted, 3 not_run): an unknown
    4 read as timeout_halted - its dpc taken as valid."""
    payload = struct.pack("<BBIIB", riscv.STATUS["timeout"], 4, 0x20000010, 1000, 0)
    res = riscv.RiscvDm.run_result(m.Result(1, m.COMPLETED, m.FAILED, payload))
    assert res.unknown_stopped == 4 and not res.stopped and not res.not_halted and not res.not_run
    assert not res.dpc_valid and "0x04" in res.where()
    known = riscv.RiscvDm.run_result(m.Result(1, m.COMPLETED, m.FAILED, payload[:1] + b"\0" + payload[2:]))
    assert known.unknown_stopped is None and known.dpc_valid


def test_the_restart_checker_takes_any_uptime_since_the_answer():
    """oep-if-restart §2: 再起動の後は、OEP については電源を入れたときの起動と同じである; core §2.7: probe の時計は 1 つ:
    起動からの ns. Nothing says the new boot's clock reads 0 when it first answers: a probe whose clock started 1 ms
    into its 50 ms restart (counting its own start-up) was failed for 'uptime differs'."""
    model, checks = restart_setup()
    boot = model.complete_boot

    def early_clock():
        boot()
        model.ep._origin_ns -= 1_000_000                 # the clock began 1 ms before the boot completed
    model.complete_boot = early_clock
    r = RestartChecks(checks, SampleRestartAdapter(model)).run()
    assert r["status"] == "passed", r["checks"]
