"""tests/hw's own bookkeeping on the in-process virtual bench (no hardware, no `hw` marker: it runs in the ordinary suite):
Run.restore_settings puts the probe's settings back as they were before tests/hw changed any - after a test that
failed half-way, and after a probe that went away (opened again; while it stays away, what is left is recorded and the
summary prints the commands that remove it)."""
from __future__ import annotations

import pytest

from oep_client import config, core, endpoint, virtual_bench, host as h

from . import boards, record

VENDOR = 1


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
    return record._by_key(config.ProbeConfig(hst).get()[1])


def test_a_failed_test_leaves_nothing():
    """The user's label on 5 (saved) stays; the test's label over it, its disable 7 and its save are undone."""
    ep, hst, r = _bench()
    cfg = config.ProbeConfig(hst)
    cfg.set([config.Label(channel=5, text="MINE")])
    saved0 = cfg.save()
    r.settings_before(cfg)
    r.settings_changing("config", "label", 5)
    r.settings_changing("config", "disable", 7)
    cfg.set([config.Label(channel=5, text="HW-A"), config.Disable(channel=7)])
    r.settings_saving()
    cfg.save()                                                  # ... and the test fails here
    assert r.restore_settings("config")
    now = _items(hst)
    assert ("disable", 7) not in now and config.decode(*now[("label", 5)]).text == "MINE"
    st = cfg.state()
    assert st.storage == "applied" and st.saved_hash == saved0 and not r.settings_pending and not r.settings_saved
    assert r.restore_settings("again")                          # nothing more to do
    assert r.tests["_settings"]["restored"][0]["removed"] == ["disable 7"]


def test_nothing_saved_before_is_erased_again():
    ep, hst, r = _bench()
    cfg = config.ProbeConfig(hst)
    r.settings_before(cfg)
    assert r.settings_baseline["storage"] == "none"
    r.settings_changing("config", "disable", 7)
    cfg.set([config.Disable(channel=7)])
    r.settings_saving()
    cfg.save()
    assert r.restore_settings("config")
    assert cfg.state().storage == "none" and ("disable", 7) not in _items(hst)


def test_a_probe_that_went_away(monkeypatch):
    """Not back (connect times out): False, what is left recorded with the commands, and require() skips naming the
    loss. Back later (it rebooted with the saved disable): the next try removes it and erases the storage."""
    ep, hst, r = _bench()
    cfg = config.ProbeConfig(hst)
    r.settings_before(cfg)
    r.settings_changing("config", "disable", 28)
    cfg.set([config.Disable(channel=28)])
    r.settings_saving()
    cfg.save()
    r.lost = "not back after oep.probe.restart"
    tries = []

    def gone(timeout_s=30.0):
        tries.append(timeout_s)
        raise TimeoutError("no confirm")
    monkeypatch.setattr(r, "connect", gone)
    assert not r.restore_settings("config")
    assert r.settings_left == ["oep config remove /dev/ttyTEST disable 28", "oep config erase /dev/ttyTEST"]
    assert tries == [record.RESTORE_CONNECT_S] and r.settings_pending
    assert any("SETTINGS LEFT ON THE PROBE" in line for line in r.summary())
    assert any("oep config remove /dev/ttyTEST disable 28" in line for line in r.summary())
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
    assert ("disable", 28) in _items(h.Host(lambda b: ep.handle(b, VENDOR)))
    assert r.require() is r.hst and r.lost is None                # reconnected: the settings put back first
    assert ("disable", 28) not in _items(r.hst) and config.ProbeConfig(r.hst).state().storage == "none"
    assert not r.settings_left and not any("SETTINGS LEFT" in line for line in r.summary())
