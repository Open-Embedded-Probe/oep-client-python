"""oep.fixture.pwm / counter / dac clients (fixture §7-§9): the request bodies and the answers of the spec's vectors
(ops.json), against a scripted host."""

import json
import struct
from pathlib import Path

from oep_client import fixture as et, registry as reg
from test_target_parts import ScriptedHost, ok

PWM, CNT, DAC = 17, 18, 19
REQUEST_HEADER, RESULT_HEADER = reg.LIMITS["request_header_bytes"], reg.LIMITS["result_header_bytes"]
CASES = {c["name"]: c for c in json.loads((Path(__file__).parent / "vectors/ops.json").read_text())["cases"]}


def vector(name):
    c = CASES[name]
    return bytes.fromhex(c["request_hex"])[REQUEST_HEADER:], bytes.fromhex(c["answer_hex"])[RESULT_HEADER:]


def host(handlers):
    hst = ScriptedHost(handlers)
    hst._fns.update({et.Pwm.NAME: PWM, et.Counter.NAME: CNT, et.Dac.NAME: DAC})
    hst._revisions.update({PWM: 1, CNT: 1, DAC: 1})
    return hst


def test_pwm_set_vector():
    request, answer = vector("pwm set: the nearest frequency and duty, and an output stopped")
    hst = host({(PWM, et.Pwm.SET): lambda p: ok(answer)})
    made = et.Pwm(hst).set([(0, 1000, 32767), (1, 0, 0)])
    assert hst.log[-1][2] == request
    assert made == [et.PwmOutput(role=0, num=1000, den=1, duty=32768), et.PwmOutput(role=1, num=0, den=1, duty=0)]
    assert made[0].hz == 1000.0 and made[1].hz == 0.0


def test_counter_measure_vector():
    request, answer = vector("counter measure: the edges in the window, every planned role")
    hst = host({(CNT, et.Counter.MEASURE): lambda p: ok(answer)})
    got = et.Counter(hst).measure(100000, et.Counter.RISING)
    assert hst.log[-1][2] == request
    assert got.window_ns == 100002000 and got.counts == {0: 100, 1: 0}
    assert abs(got.hz(0) - 999.98) < 0.01


def test_dac_status_vector_and_set_body():
    _, answer = vector("dac status: the value set, on")
    hst = host({(DAC, et.Dac.STATUS): lambda p: ok(answer), (DAC, et.Dac.SET): lambda p: ok()})
    d = et.Dac(hst)
    assert d.status() == [(0, 128, True), (1, 0, False)]
    d.set([(0, 100)])
    assert hst.log[-1][2] == struct.pack("<BBI", 1, 0, 100)
