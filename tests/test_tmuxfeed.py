"""The tmux feed (pantheon/tmuxfeed.py): one helper asks tmux, the deck reads a file."""
from __future__ import annotations

import json
import os
import time

from pantheon import config as config_mod
from pantheon import tmuxfeed
from pantheon.models import TmuxWindow
from pantheon.supervisor import approvals as approvals_mod


# conftest swaps `ensure_running` out for every test so no deck starts a real helper; this file
# tests the real one with an injected `popen`.
REAL_ENSURE = tmuxfeed.ensure_running


def make_cfg(tmp_path):
    return config_mod.Config(state_dir=str(tmp_path))


WINDOWS = [TmuxWindow(0, "DECK", "python", "/c/p", "%0", "pantheon"),
           TmuxWindow(3, "life-os", "claude", "/c/l", "%7", "pantheon"),
           TmuxWindow(4, "ASSISTANT", "claude", "/c/w", "%9", "pantheon")]


def test_a_pass_lists_windows_and_reads_every_agent_screen_but_not_the_decks(tmp_path):
    cfg = make_cfg(tmp_path)
    read = []
    caps: dict = {}
    tmuxfeed.collect_once(cfg, caps, list_windows=lambda s, t: list(WINDOWS),
                          capture=lambda target, tmux=None: read.append(target) or f"screen {target}")
    assert read == ["%7", "%9"]                               # DECK is never read
    data = json.loads(tmuxfeed.feed_path(cfg).read_text())
    assert [w["name"] for w in data["windows"]] == ["DECK", "life-os", "ASSISTANT"]
    assert data["captures"]["%7"]["text"] == "screen %7"


def test_a_screen_is_reread_only_when_due_and_dropped_when_its_pane_goes(tmp_path):
    cfg = make_cfg(tmp_path)
    clock = [1000.0]
    read = []
    caps: dict = {}
    run = lambda wins: tmuxfeed.collect_once(cfg, caps, now=lambda: clock[0], list_windows=lambda s, t: wins,
                                             capture=lambda target, tmux=None: read.append(target) or "x")
    run(WINDOWS)
    clock[0] += 1.0
    run(WINDOWS)
    assert read == ["%7", "%9"]                               # not due yet
    clock[0] += tmuxfeed.CAPTURE_SECONDS
    run(WINDOWS[:2])                                          # the Assistant's window closed
    assert read == ["%7", "%9", "%7"]
    assert set(caps) == {"%7"}


def test_readers_use_a_fresh_feed_and_fall_back_to_live_when_it_is_stale_or_missing(tmp_path):
    cfg = make_cfg(tmp_path)
    live_windows = [TmuxWindow(9, "live", "bash", "/c", "%99", "pantheon")]
    assert tmuxfeed.windows_or_live(cfg, lambda: live_windows) == live_windows      # no feed yet
    tmuxfeed.collect_once(cfg, {}, list_windows=lambda s, t: list(WINDOWS),
                          capture=lambda target, tmux=None: f"feed {target}")
    assert tmuxfeed.windows_or_live(cfg, lambda: live_windows) == WINDOWS
    capture = tmuxfeed.capture_or_live(cfg, live=lambda target, tmux=None: "live")
    assert capture("pantheon:3") == "feed %7"                  # session:index -> pane id
    assert capture("pantheon-4711:3") == "feed %7"             # a viewer's grouped session
    assert capture("%9") == "feed %9"
    assert capture("pantheon:0") == "live"                     # the deck's own window: not in the feed
    data = json.loads(tmuxfeed.feed_path(cfg).read_text())
    data["at"] -= tmuxfeed.FRESH_SECONDS + 1
    tmuxfeed.feed_path(cfg).write_text(json.dumps(data))
    assert tmuxfeed.windows_or_live(cfg, lambda: live_windows) == live_windows
    assert capture("pantheon:3") == "live"


def test_ensure_running_starts_one_helper_and_reuses_a_live_one(tmp_path):
    cfg = make_cfg(tmp_path)
    started = []

    class Proc:
        pid = os.getpid()                                     # a pid that is certainly alive

    def popen(argv, **kw):
        started.append(argv)
        return Proc()

    assert REAL_ENSURE(cfg, popen=popen) == os.getpid()
    assert REAL_ENSURE(cfg, popen=popen) == os.getpid()        # the pid file names a live one
    assert len(started) == 1
    assert started[0][1:4] == ["-m", "pantheon.tmuxfeed", "--parent"]
    assert started[0][4] == str(os.getpid())


def test_the_helper_leaves_when_the_deck_that_started_it_is_gone(tmp_path):
    cfg = make_cfg(tmp_path)
    passes = []
    n = tmuxfeed.loop(cfg, parent=2_147_000_000, sleep=lambda s: None,
                      collect=lambda *a, **k: passes.append(1),
                      watch=tmuxfeed.orphan.OrphanWatch.inert(), max_passes=5)
    assert n == 0 and passes == []
    n = tmuxfeed.loop(cfg, parent=os.getpid(), sleep=lambda s: None,
                      collect=lambda *a, **k: passes.append(1),
                      watch=tmuxfeed.orphan.OrphanWatch.inert(), max_passes=3)
    assert n == 3


def test_approvals_read_the_feed_each_tick_but_never_when_answering(tmp_path):
    """The answer path goes through `_capture` (live); only the per-tick read takes the feed."""
    import pantheon.tmuxctl as tmuxctl

    scans, lives = [], []
    ap = approvals_mod.Approvals(scan_capture=lambda t, tmux=None: scans.append(t) or "")
    orig = tmuxctl.capture
    try:
        tmuxctl.capture = lambda t, tmux=None: lives.append(t) or ""
        ap._capture("%7")
    finally:
        tmuxctl.capture = orig
    assert lives == ["%7"] and scans == []
