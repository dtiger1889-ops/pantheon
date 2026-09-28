"""`tmuxctl.list_windows_cached`: the supervisor and queue
panes both default to this instead of `list_windows` directly, so a combined-deck tick usually
spawns one `tmux list-windows` subprocess, not two. Only the caching wrapper is tested here --
`list_windows` itself is already exercised indirectly by every test that monkeypatches
`tmuxctl.run` (test_governor_runner.py, test_supervisor_app.py, ...).
"""
from __future__ import annotations

import time

from pantheon import tmuxctl
from pantheon.models import TmuxWindow


class _CountingList:
    """Stands in for `tmuxctl.list_windows`: records how many times it was actually called."""

    def __init__(self, windows):
        self.windows = list(windows)
        self.calls = 0

    def __call__(self, session=None, tmux=None):
        self.calls += 1
        return list(self.windows)


def test_repeated_calls_within_the_window_share_one_real_poll(monkeypatch):
    tmuxctl._WINDOWS_CACHE.clear()
    fake = _CountingList([TmuxWindow(1, "loom-os", "node", "C:/x", "%1", "pantheon")])
    monkeypatch.setattr(tmuxctl, "list_windows", fake)

    first = tmuxctl.list_windows_cached("pantheon", max_age_seconds=5.0)
    second = tmuxctl.list_windows_cached("pantheon", max_age_seconds=5.0)
    assert fake.calls == 1                    # the second call was served from the cache
    assert first == second == fake.windows


def test_a_different_session_or_tmux_path_is_not_served_from_the_others_cache(monkeypatch):
    tmuxctl._WINDOWS_CACHE.clear()
    fake = _CountingList([TmuxWindow(1, "loom-os", "node", "C:/x", "%1", "pantheon")])
    monkeypatch.setattr(tmuxctl, "list_windows", fake)

    tmuxctl.list_windows_cached("pantheon")
    tmuxctl.list_windows_cached("main")
    tmuxctl.list_windows_cached(None, tmux="/other/tmux")
    assert fake.calls == 3                    # three distinct cache keys, three real polls


def test_the_cache_expires_after_max_age_seconds(monkeypatch):
    tmuxctl._WINDOWS_CACHE.clear()
    fake = _CountingList([])
    monkeypatch.setattr(tmuxctl, "list_windows", fake)
    clock = {"t": 0.0}
    monkeypatch.setattr(time, "monotonic", lambda: clock["t"])

    tmuxctl.list_windows_cached("pantheon", max_age_seconds=1.0)
    clock["t"] = 2.0    # past the 1-second cache window
    tmuxctl.list_windows_cached("pantheon", max_age_seconds=1.0)
    assert fake.calls == 2
