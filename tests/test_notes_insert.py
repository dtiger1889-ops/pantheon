"""Insert into the selected agent:
`tmux send-keys -l`, never Enter -- the user presses send himself.
"""
from __future__ import annotations

from datetime import datetime, timezone

from pantheon import config as config_mod
from pantheon import events as events_mod
from pantheon import tmuxctl
from pantheon.models import Event, TmuxWindow
from pantheon.notes import app as notes_app


class RecordingTmux:
    """Records every `tmux` call; `send-keys` always succeeds."""

    def __init__(self):
        self.calls: list[tuple] = []

    def run(self, *args, tmux=None, check=False):
        import subprocess

        self.calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")


def test_insert_sends_each_line_literally_with_no_enter(monkeypatch):
    fake = RecordingTmux()
    monkeypatch.setattr(tmuxctl, "run", fake.run)

    ok = notes_app.insert_into_agent("pantheon:3", "first line\nsecond line")

    assert ok is True
    send_keys_calls = [c for c in fake.calls if c[0] == "send-keys"]
    # Every call is literal (`-l`); `Enter` never appears anywhere.
    assert all("-l" in c for c in send_keys_calls)
    assert not any("Enter" in c for c in fake.calls)
    # Both lines present, in order, with a literal newline between them (not inside one call).
    assert send_keys_calls == [
        ("send-keys", "-t", "pantheon:3", "-l", "--", "first line"),
        ("send-keys", "-t", "pantheon:3", "-l", "--", "\n"),
        ("send-keys", "-t", "pantheon:3", "-l", "--", "second line"),
    ]


def test_insert_of_a_single_line_sends_one_call_and_no_trailing_newline(monkeypatch):
    fake = RecordingTmux()
    monkeypatch.setattr(tmuxctl, "run", fake.run)

    notes_app.insert_into_agent("pantheon:3", "just one line")

    assert fake.calls == [("send-keys", "-t", "pantheon:3", "-l", "--", "just one line")]


def test_insert_reports_failure_when_any_call_fails(monkeypatch):
    def failing_run(*args, tmux=None, check=False):
        import subprocess

        return subprocess.CompletedProcess(args, 1, "", "tmux refused")

    monkeypatch.setattr(tmuxctl, "run", failing_run)
    assert notes_app.insert_into_agent("pantheon:3", "hello") is False


# ---------------------------------------------------------------- selectable_target


def _iso(dt) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


NOW = datetime(2026, 9, 5, 12, 0, 0, tzinfo=timezone.utc)


def _cfg(tmp_path):
    return config_mod.Config(state_dir=str(tmp_path / "state"), tmux_session="pantheon")


def test_selectable_target_picks_the_top_row_with_a_live_window_in_this_session(tmp_path):
    cfg = _cfg(tmp_path)
    events_mod.append_event(cfg.events_file, Event(
        ts=_iso(NOW), event="SessionStart", session_id="s1", cwd="C:/proj/a", tmux_pane="%1",
    ))
    windows = [TmuxWindow(3, "a", "node", "C:/proj/a", "%1", "pantheon")]
    row = notes_app.selectable_target(cfg, window_source=lambda: windows)
    assert row is not None
    assert row.window_index == 3
    assert row.in_pantheon is True


def test_selectable_target_is_none_when_no_agent_has_a_live_window(tmp_path):
    cfg = _cfg(tmp_path)
    row = notes_app.selectable_target(cfg, window_source=lambda: [])
    assert row is None


def test_insert_target_prefers_the_rows_own_tmux_session(tmp_path):
    cfg = _cfg(tmp_path)
    events_mod.append_event(cfg.events_file, Event(
        ts=_iso(NOW), event="SessionStart", session_id="s1", cwd="C:/proj/a", tmux_pane="%1",
    ))
    windows = [TmuxWindow(3, "a", "node", "C:/proj/a", "%1", "pantheon")]
    row = notes_app.selectable_target(cfg, window_source=lambda: windows)
    assert notes_app.insert_target(row, cfg) == "pantheon:3"
