"""Review 6 (capture, console): checker divergences from oep-spec a09fa45 interfaces/oep-if-capture.ja.md.

- §3.3: segments outside mode 2 and a pretrigger without a trigger are unsupported "値によらず", and "断りが 2 つ以上当たるとき、
  probe は当たったどれか 1 つで答える" - the WIDTH case may not require malformed for such a TLV of a wrong length.
- §1.2: "アナログのチャネルは start で空きの状態を離れる（pad がデジタルの機能を離れる）" - the analog, group and timeline checkers
  may not require an analog channel's pin to keep its free state while the track waits or captures (they still do once
  acquisition stops)."""

import struct

from oep_client.conformance_analog import AnalogChecks
from oep_client.conformance_analog_sample import SampleAnalogAdapter
from oep_client.conformance_capture_group import CaptureGroupChecks
from oep_client.conformance_capture_group_sample import SampleCaptureGroupAdapter
from oep_client.conformance_capture_timeline import CaptureTimelineChecks
from oep_client.conformance_capture_timeline_sample import SampleCaptureTimelineAdapter
from oep_client.conformance_logic import LogicChecks
from oep_client.conformance_logic_sample import SampleLogicAdapter
from oep_client.virtual_analog_model import Analog
from oep_client.virtual_core import Core, Reject
from oep_client.virtual_logic_model import Logic

import test_conformance_analog as analog_tests
import test_conformance_capture_group as group_tests
import test_conformance_capture_timeline as timeline_tests
import test_conformance_logic as logic_tests


def contract_first(base):
    """A track that applies §3.3's "値によらず" refusals before it looks at the values' lengths (both apply: either answer)."""
    class ContractFirst(base):
        def request_tags(self, fn, op, data):
            if fn == 2 and op in (16, 24):
                rows, rest = [], data
                while len(rest) >= 3:
                    raw, size = struct.unpack_from('<BH', rest)
                    rows.append((raw, rest[3:3 + size]))
                    rest = rest[3 + size:]
                first = {}
                for raw, value in rows:
                    first.setdefault(raw & 127, value)
                mode, trigger = first.get(64, b''), first.get(69, b'')
                for raw, _ in rows:
                    if raw & 127 == 68 and mode != b'\2':
                        raise Reject(11, bytes((raw,)))            # segments outside mode 2
                    if raw & 127 == 70 and not trigger[:1].strip(b'\0'):
                        raise Reject(11, bytes((raw,)))            # a pretrigger with no trigger (or type 0)
            return super().request_tags(fn, op, data)
    return ContractFirst


def install(model, cls):
    def go():
        model.extension = cls(model.ep)
        model.ep.current_core = Core(model.ep, model.extension)
    model.install = go
    go()


def test_logic_width_case_accepts_the_contract_refusal_for_segments_and_pretrigger():
    model, c = logic_tests.setup()
    install(model, contract_first(Logic))
    checker = LogicChecks(c, SampleLogicAdapter(model))
    checker.cases = ('WIDTH', 'CONTRACT')
    r = checker.run()
    assert r['status'] == 'passed', [(row['id'], row.get('error')) for row in r['checks'] if row['status'] != 'passed']


def test_analog_width_case_accepts_the_contract_refusal_for_segments_and_pretrigger():
    model, c = analog_tests.setup()
    install(model, contract_first(Analog))
    checker = AnalogChecks(c, SampleAnalogAdapter(model))
    checker.cases = ('WIDTH', 'CONTRACT')
    r = checker.run()
    assert r['status'] == 'passed', [(row['id'], row.get('error')) for row in r['checks'] if row['status'] != 'passed']


ANALOG_PAD = 0xA0   # how these fixtures' state shows a pad on its analog function


def analog_pads(state, tracks):
    """`state` with the analog track's planned channels on their analog function while it waits or captures."""
    def view():
        s = state()
        for fn, track in tracks(s):
            if track['state'] in (2, 3):
                for ch in s['plans'].get(fn, ()):
                    s['pins'][ch] = ANALOG_PAD
        return s
    return view


def test_analog_checker_lets_the_pads_leave_their_free_state_while_acquiring():
    model, c = analog_tests.setup()
    model.analog_state = analog_pads(model.analog_state, lambda s: [(2, s)])
    checker = AnalogChecks(c, SampleAnalogAdapter(model))
    checker.cases = ('ONE-SHOT', 'GENERATION', 'READ-GAP', 'REPEAT', 'QUERY-STATE-2', 'QUERY-STATE-3', 'QUERY-STATE-5')
    r = checker.run()
    assert r['status'] == 'passed', [(row['id'], row.get('error')) for row in r['checks'] if row['status'] != 'passed']


def test_analog_checker_still_wants_the_pads_back_when_acquisition_stops():
    model, c = analog_tests.setup()
    state = model.analog_state
    model.analog_state = analog_pads(state, lambda s: [(2, dict(s, state=3 if s['state'] == 4 else s['state']))])
    checker = AnalogChecks(c, SampleAnalogAdapter(model))
    checker.cases = ('ONE-SHOT',)
    r = checker.run()
    assert next(row for row in r['checks'] if row['id'] == 'OEP-ANALOG-ONE-SHOT')['status'] == 'failed'


def test_group_checker_lets_the_analog_pads_leave_their_free_state_while_acquiring():
    model, c = group_tests.setup()
    model.group_state = analog_pads(model.group_state, lambda s: [(2, s['tracks'][2])])
    checker = CaptureGroupChecks(c, SampleCaptureGroupAdapter(model))
    checker.cases = ('START', 'QUERY', 'REPEAT', 'ERROR-ADC', 'STOP')
    r = checker.run()
    assert r['status'] == 'passed', [(row['id'], row.get('error')) for row in r['checks'] if row['status'] != 'passed']


def test_timeline_checker_lets_the_analog_pads_leave_their_free_state_while_acquiring():
    model, c = timeline_tests.setup()
    model.timeline_state = analog_pads(model.timeline_state, lambda s: [(2, s['tracks'][2])])
    checker = CaptureTimelineChecks(c, SampleCaptureTimelineAdapter(model))
    checker.scenarios = (CaptureTimelineChecks.scenarios[0], CaptureTimelineChecks.scenarios[-1])
    r = checker.run()
    assert r['status'] == 'passed', [(row['id'], row.get('error')) for row in r['checks'] if row['status'] != 'passed']
