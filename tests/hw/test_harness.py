"""tests/hw's own bookkeeping on the in-process virtual bench (no hardware, no `hw` marker: it runs in the ordinary suite):
Run.restore_settings puts the probe's settings back as they were before tests/hw changed any - after a test that
failed half-way, and after a probe that went away (opened again; while it stays away, what is left is recorded and the
summary prints the commands that remove it)."""
from __future__ import annotations
import struct

import pytest

from oep_client import core, endpoint, settings, virtual_bench, host as h

from . import boards, record

VENDOR = 1
CORE = settings.CORE_CATEGORY
LABEL5, DISABLE7, DISABLE28 = "channel.GPIO5.label", "channel.GPIO7.disable", "channel.GPIO28.disable"


class Clock:
    t = 0

    def __call__(self):
        return self.t


def _bench():
    ep = endpoint.Endpoint(virtual_bench.p4_x035(), Clock())
    hst = h.Host(lambda b: ep.handle(b, VENDOR))
    r = record.Run(boards.Board("virtual-harness", "virtual", "esp32", "esp32", ""), client={"version": "test"})
    r.hst, r.port = hst, "/dev/ttyTEST"
    core.take(hst, 30000, owner="oep tests/hw")
    return ep, hst, r


def _items(hst) -> dict:
    return record._by_key(settings.Settings(hst))


def test_a_failed_test_leaves_nothing():
    """The user's label on 5 (saved) stays; the test's label over it, its disable 7 and its save are undone."""
    ep, hst, r = _bench()
    st = settings.Settings(hst)
    st.set_named(CORE, {LABEL5: "MINE"})
    st.save()
    r.settings_before(st)
    r.settings_changing("config", CORE, LABEL5)
    r.settings_changing("config", CORE, DISABLE7)
    st.set_named(CORE, {LABEL5: "HW-A", DISABLE7: True})
    r.settings_saving()
    st.save()                                                   # ... and the test fails here
    assert r.restore_settings("config")
    now = _items(hst)
    assert (CORE, DISABLE7) not in now and now[(CORE, LABEL5)] == "MINE"
    state = st.state()
    assert state.storage == "applied" and not st.needs_save() and not r.settings_pending and not r.settings_saved
    assert ep.saved == {("core", "channel.{}.label", 5): b"MINE"}      # stored as before the test
    assert r.restore_settings("again")                          # nothing more to do
    assert r.tests["_settings"]["restored"][0]["removed"] == [f"{CORE} {DISABLE7}"]


def test_nothing_saved_before_is_saved_empty_again():
    """No erase (core §13.7): a probe that had nothing saved gets every user value unset and an empty save."""
    ep, hst, r = _bench()
    st = settings.Settings(hst)
    r.settings_before(st)
    assert r.settings_baseline["storage"] == "none"
    r.settings_changing("config", CORE, DISABLE7)
    st.set_named(CORE, {DISABLE7: True})
    r.settings_saving()
    st.save()
    assert r.restore_settings("config")
    assert ep.saved == {} and (CORE, DISABLE7) not in _items(hst)


def test_a_probe_that_went_away(monkeypatch):
    """Not back (connect times out): False, what is left recorded with the commands, and require() skips naming the
    loss. Back later (it rebooted with the saved disable): the next try removes it and saves the storage empty."""
    ep, hst, r = _bench()
    st = settings.Settings(hst)
    r.settings_before(st)
    r.settings_changing("config", CORE, DISABLE28)
    st.set_named(CORE, {DISABLE28: True})
    r.settings_saving()
    st.save()
    r.lost = "not back after oep.probe.restart"
    tries = []

    def gone(timeout_s=30.0):
        tries.append(timeout_s)
        raise TimeoutError("no confirm")
    monkeypatch.setattr(r, "connect", gone)
    assert not r.restore_settings("config")
    assert r.settings_left == [f"oep settings unset /dev/ttyTEST {CORE} {DISABLE28}",
                               "oep settings unset --all /dev/ttyTEST --save"]
    assert tries == [record.RESTORE_CONNECT_S] and r.settings_pending
    assert any("SETTINGS LEFT ON THE PROBE" in line for line in r.summary())
    assert any(f"oep settings unset /dev/ttyTEST {CORE} {DISABLE28}" in line for line in r.summary())
    with pytest.raises(pytest.skip.Exception, match="the probe is gone: not back"):
        r.require()
    monkeypatch.setenv("OEP_HW_REOPEN_S", "40")
    tries.clear()
    assert not r.restore_settings("end of run") and tries == [40.0]

    ep.reboot()                                                 # back on a new boot: the saved disable applied
    def back(timeout_s=30.0):
        r.hst = h.Host(lambda b: ep.handle(b, VENDOR))
        r.hst.confirm()
        return r.hst
    monkeypatch.setattr(r, "connect", back)
    assert (CORE, DISABLE28) in _items(h.Host(lambda b: ep.handle(b, VENDOR)))
    assert r.require() is r.hst and r.lost is None                # reconnected: the settings put back first
    assert (CORE, DISABLE28) not in _items(r.hst) and ep.saved == {}
    assert not r.settings_left and not any("SETTINGS LEFT" in line for line in r.summary())
