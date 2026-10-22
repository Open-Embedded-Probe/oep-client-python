"""Review 7, state machines and resource lifetimes (oep-spec a09fa45): each test fails on the code before its fix."""

import struct

from oep_client import capture, core, settings, endpoint, host as h, message as m, virtual_bench
from oep_client.virtual_analog_model import AnalogModel
from oep_client.virtual_capture_group_model import CaptureGroupModel
from oep_client.virtual_capture_timeline_model import CaptureTimelineModel
from oep_client.virtual_core import Reject, tlv

GPIO, UART, LOGIC, CONFIG = 4, 5, 6, 9                     # esp32_v003


class Clock:
    t = 0

    def __call__(self):
        return self.t


def bench():
    ep = endpoint.Endpoint(virtual_bench.esp32_v003(), Clock())
    hst = h.Host(lambda b: ep.handle(b, 0))
    hst.open(3000)
    return ep, hst


def test_a_channel_moving_to_another_fn_is_released_to_the_idle_the_settings_give_it_now():
    """plan §2.1 / core §8: only a (fn, role, channel) that stays is kept; a channel leaving gpio for the UART's RX in
    one plan_apply is released - to its idle state as the settings say now (an idle set while it was in use) - and
    taking it changes nothing."""
    ep, hst = bench()
    core.plan_apply(hst, [(GPIO, 1, 4)])
    settings.Settings(hst).set_named(settings.CORE_CATEGORY, {"channel.GPIO4.idle": "pull-up"})   # in use: waits for the release
    assert ep.pin_state(4) == "idle hi-z"
    core.plan_apply(hst, [(GPIO, 1, 5), (UART, 1, 4)])
    assert ep.pin_state(4) == "idle pull-up"


def test_an_idle_change_on_a_channel_a_logic_plan_holds_waits_for_its_release():
    """core §13.8: a channel in use takes its new idle when it is next free; a logic plan holds its channel too
    (core §8: taken, only listened to), so its pin keeps the old idle state until the plan goes."""
    ep, hst = bench()
    core.plan_apply(hst, [(LOGIC, 0, 13)])
    settings.Settings(hst).set_named(settings.CORE_CATEGORY, {"channel.GPIO13.idle": "pull-up"})
    assert ep.pin_state(13) == "idle hi-z"
    core.plan_release(hst, [LOGIC])
    assert ep.pin_state(13) == "idle pull-up"


def test_the_session_end_drops_what_its_subscriptions_had_not_sent():
    """core §9: subscriptions end with the session; §10.3: an ended subscription's unsent notifications are dropped
    (the stop's stopped event, queued but not started, does not go out after end)."""
    ep, hst = bench()
    core.plan_apply(hst, [(LOGIC, 0, 13)])
    cap = capture.LogicCapture(hst, fn=LOGIC)
    cap.configure(rate=1_000_000, samples=100)
    hst.call(LOGIC, m.OP_SUBSCRIBE, struct.pack("<HI", 0, 0))
    cap.start()
    cap.stop()
    assert ep.outbox                                           # the stopped event waits to go out
    hst.end()
    assert ep.pushes() == []


def _plan(model, sid, rows, corr=[100]):
    corr[0] += 1
    body = b"".join(tlv(0x10, struct.pack("<HBH", fn, role, ch)) for fn, role, ch in rows)
    return model.handle(struct.pack("<BHHBI", 1, corr[0], 1, 0x10, sid) + body)


def _open(model, sid=7):
    model.handle(struct.pack("<BHHBI", 1, 1, 0, 0x10, sid) + struct.pack("<IB", 3000, 0))
    return sid


def test_analog_model_refuses_a_plan_onto_an_output_idle_with_cause_5():
    """capture §1.2: an analog plan onto a channel whose idle is an output (mode 3 / 4) is rejected unavailable
    cause 5; the fixture's analog channels idle as no output, so the usual plan is taken."""
    model = AnalogModel(boot_id=17)
    sid = _open(model)
    assert _plan(model, sid, [(2, 1, 0), (2, 3, 1)])[3:5] == b"\1\0"   # completed success
    model.extension.idle[0] = 4
    before = dict(model.extension.plans)
    reply = _plan(model, sid, [(2, 1, 0)])
    assert reply[3:5] == b"\0\4" and tlv(1, b"\5") in reply[5:] and tlv(2, b"\0\0") in reply[5:]
    assert model.extension.plans == before


def test_group_and_timeline_models_refuse_an_analog_plan_onto_an_output_idle():
    for model in (CaptureGroupModel(), CaptureTimelineModel()):
        sid = _open(model)
        model.extension.idle[1] = 3
        reply = _plan(model, sid, [(2, 1, 0), (2, 3, 1)])
        assert reply[3:5] == b"\0\4" and tlv(1, b"\5") in reply[5:], type(model)
        assert not model.extension.plans


def test_group_model_refuses_changing_a_running_tracks_plan_with_cause_6():
    """capture §3.2: a plan_apply changing a track's (role, channel) set is refused unavailable cause 6 in state 2/3/5
    (the track not bound to a group too); nothing changes."""
    model = CaptureGroupModel()
    sid = _open(model)
    e = model.extension
    assert _plan(model, sid, [(3, 0, 2), (3, 1, 3)])[3:5] == b"\1\0"
    t = e.tracks[3]
    t.state = 3                                                # the track runs (capture §3.2)
    reply = _plan(model, sid, [(3, 0, 2)])
    assert reply[3:5] == b"\0\4" and tlv(1, b"\6") in reply[5:]
    assert e.plans[3] == [2, 3] and t.state == 3
