"""The orphan watch (pantheon/orphan.py): the pid comes out of `$TMUX`, liveness is asked without
spawning, and two misses in a row mean leave."""
from __future__ import annotations

import os

from pantheon import orphan, tmuxctl


def test_server_pid_parses_the_tmux_variable(monkeypatch):
    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,4242,0")
    assert tmuxctl.server_pid() == 4242
    monkeypatch.setenv("TMUX", "garbage")
    assert tmuxctl.server_pid() is None
    monkeypatch.delenv("TMUX", raising=False)
    assert tmuxctl.server_pid() is None


def test_server_alive_uses_the_pid_without_spawning():
    assert tmuxctl.server_alive(os.getpid()) is True
    assert tmuxctl.server_alive(None) is None
    assert tmuxctl.server_alive(999_999) is False      # far above any pid this box hands out


def test_watch_needs_two_misses_in_a_row_then_leaves_once():
    left = []
    answers = iter([True, False, True, False, False])
    watch = orphan.OrphanWatch(pid=7, leave=lambda: left.append(1), alive=lambda pid: next(answers))
    assert [watch.tick() for _ in range(5)] == [False, False, False, False, True]
    assert left == [1]


def test_watch_is_inert_outside_tmux(monkeypatch):
    monkeypatch.delenv("TMUX", raising=False)
    watch = orphan.OrphanWatch(leave=lambda: (_ for _ in ()).throw(AssertionError("must not leave")))
    assert not watch.active and watch.tick() is False
    assert not orphan.OrphanWatch.inert().active


def test_install_only_starts_the_watchdog_inside_tmux(monkeypatch):
    """The checks run on a thread, never on the app's timer: a frozen event loop stops its timers,
    which is exactly when the watch is needed (pantheon/orphan.py docstring)."""
    class FakeApp:
        def __init__(self):
            self.intervals = []

        def set_interval(self, seconds, callback):
            self.intervals.append((seconds, callback))

    started = []
    start = lambda watch, seconds: started.append((watch, seconds))
    monkeypatch.delenv("TMUX", raising=False)
    app = FakeApp()
    assert not orphan.install(app, start=start).active and started == []
    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,4242,0")
    app = FakeApp()
    watch = orphan.install(app, start=start)
    assert watch.active and started == [(watch, orphan.CHECK_SECONDS)]
    assert app.intervals == []          # nothing rides on the event loop any more


def test_watchdog_thread_ticks_until_it_leaves():
    left = []
    answers = iter([True, False, False])
    watch = orphan.OrphanWatch(pid=7, leave=lambda: left.append(1), alive=lambda pid: next(answers))
    slept = []
    thread = orphan.start_watchdog(watch, seconds=5, sleep=slept.append)
    thread.join(10)
    assert not thread.is_alive() and thread.daemon
    assert left == [1] and slept == [5, 5, 5]


def test_watchdog_survives_a_check_that_raises():
    left = []
    answers = iter([RuntimeError("odd"), False, False])

    def alive(pid):
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer
    watch = orphan.OrphanWatch(pid=7, leave=lambda: left.append(1), alive=alive)
    thread = orphan.start_watchdog(watch, seconds=0, sleep=lambda s: None)
    thread.join(10)
    assert left == [1]


def test_watchdog_is_not_started_outside_tmux():
    assert orphan.start_watchdog(orphan.OrphanWatch.inert(), sleep=lambda s: None) is None


def test_the_way_out_never_waits_on_a_held_logging_lock():
    """If a frozen thread holds a handler's lock, the leave path gives logging two seconds, not
    forever."""
    import threading
    import time

    gate = threading.Event()
    t0 = time.monotonic()
    orphan._bounded(gate.wait, seconds=0.2)
    gate.set()
    assert time.monotonic() - t0 < 2
