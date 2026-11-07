"""acquire's stream wait: wait.beside(fn) runs fn in another thread and keeps collecting until it returns."""
import threading
import time
import types

import pytest

from oep_client import acquire


def make(on_loss="continue", lose_after=None):
    polls = []

    def poll(seconds):
        polls.append(threading.current_thread().name)
        time.sleep(min(seconds, 0.01))

    def lost():
        return lose_after is not None and len(polls) >= lose_after
    return acquire._Wait(types.SimpleNamespace(on_loss=on_loss), poll, lost), polls


def test_beside_collects_while_fn_runs_and_returns_its_value():
    wait, polls = make()
    done = threading.Event()

    def slow():
        time.sleep(0.2)
        done.set()
        return 42
    assert wait.beside(slow) == 42 and done.is_set()
    assert len(polls) >= 5                                    # collected the whole time fn ran
    assert set(polls) == {threading.current_thread().name}    # collecting stayed on the caller's thread


def test_beside_raises_what_fn_raised():
    wait, _ = make()

    def boom():
        raise RuntimeError("flash failed")
    with pytest.raises(RuntimeError, match="flash failed"):
        wait.beside(boom)


def test_a_loss_with_stop_is_raised_after_fn_returns():
    wait, _ = make(on_loss="stop", lose_after=1)
    finished = []
    with pytest.raises(acquire.StreamStopped):
        wait.beside(lambda: (time.sleep(0.05), finished.append(1)))
    assert finished == [1]


def test_drive_hands_during_the_wait_with_beside():
    seen = []

    def during(wait):
        seen.append(wait.beside(lambda: "ok"))
        wait(0.01)
    req = types.SimpleNamespace(on_loss="continue", during=during, seconds=None)
    acquire._drive(req, lambda s: time.sleep(min(s, 0.005)), lambda: False)
    assert seen == ["ok"]
