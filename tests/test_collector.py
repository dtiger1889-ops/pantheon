"""The usage collector process (pantheon/hud/collector.py): a pass at once, then on a timer, at
once again on a poke, and it leaves when the tmux server is gone -- all without sleeping or forking."""
from __future__ import annotations

from pantheon import config, orphan
from pantheon.hud import collector


def _cfg(tmp_path):
    return config.Config(state_dir=str(tmp_path / "state"))


def test_first_pass_runs_at_once_then_waits_for_the_interval(tmp_path):
    calls, sleeps = [], []
    passes = collector.loop(
        _cfg(tmp_path), collect=lambda c: calls.append(1), interval=999, sleep=sleeps.append,
        watch=orphan.OrphanWatch.inert(), stop=lambda: len(sleeps) >= 3,
    )
    assert passes == 1 and calls == [1]
    assert sleeps == [collector.SLEEP_STEP_SECONDS] * 3


def test_a_poke_file_triggers_a_pass_now_and_is_removed(tmp_path):
    cfg = _cfg(tmp_path)
    calls = []

    def sleep(_seconds):
        if len(calls) == 1:
            collector.poke(cfg)          # the budget window's `r`, between two ticks

    passes = collector.loop(cfg, collect=lambda c: calls.append(1), interval=999, sleep=sleep,
                            watch=orphan.OrphanWatch.inert(), max_passes=2)
    assert passes == 2 and calls == [1, 1]
    assert not collector.poke_path(cfg).exists()


def test_a_failing_pass_is_logged_not_fatal(tmp_path):
    def boom(_cfg):
        raise RuntimeError("ccusage fell over")

    passes = collector.loop(_cfg(tmp_path), collect=boom, interval=999, sleep=lambda s: None,
                            watch=orphan.OrphanWatch.inert(), max_passes=1)
    assert passes == 1


def test_the_loop_leaves_when_the_tmux_server_is_gone(tmp_path):
    left = []
    watch = orphan.OrphanWatch(pid=4242, leave=lambda: left.append(1), alive=lambda pid: False)
    passes = collector.loop(_cfg(tmp_path), collect=lambda c: None, interval=999,
                            sleep=lambda s: None, watch=watch)
    assert left == [1] and passes == 1


def test_poke_never_raises_when_the_state_folder_is_unwritable(tmp_path):
    cfg = config.Config(state_dir=str(tmp_path / "a-file"))
    (tmp_path / "a-file").write_text("not a folder", encoding="utf-8")
    collector.poke(cfg)      # no exception is the assertion


def test_the_loop_leaves_when_its_own_window_is_gone(tmp_path):
    """`pantheon --restart` kills the budget window's bash but not this background child; the
    collector must notice it was orphaned instead of running on beside the new one."""
    sleeps = []
    passes = collector.loop(_cfg(tmp_path), collect=lambda c: None, interval=999,
                            sleep=sleeps.append, watch=orphan.OrphanWatch.inert(),
                            orphaned=lambda: len(sleeps) >= 2)
    assert passes == 1 and len(sleeps) == 2
