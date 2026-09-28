"""The typing layer, driven with a fake `tmuxctl.run` --
no real tmux server is ever touched. What matters: only `/model`, `/effort` and `BTab` are ever
sent; the text and Enter are two separate tmux calls (never combined); a window not running Claude
Code is refused before anything is typed; and Shift+Tab is pressed exactly the right number of
times to walk the permission-mode cycle.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from pantheon import session_ctl, tmuxctl


@dataclass
class FakeRun:
    """Records every tmux invocation `session_ctl` makes and answers `pane_command`'s lookup."""

    pane_command: str = "claude"
    calls: list[tuple] = field(default_factory=list)
    fail_on: int = -1   # the call index (0-based) that should report failure, or -1 for none

    def __call__(self, *args, tmux=None, check=False):
        self.calls.append(args)
        rc = 1 if len(self.calls) - 1 == self.fail_on else 0

        class _Result:
            pass

        r = _Result()
        r.returncode = rc
        r.stdout = self.pane_command if args and args[0] == "display-message" else ""
        return r


def _naps(sink: list[float]):
    return lambda seconds: sink.append(seconds)


def test_model_types_the_slash_command_then_enter_as_two_calls(monkeypatch):
    fake = FakeRun(pane_command="claude")
    monkeypatch.setattr(tmuxctl, "run", fake)
    naps: list[float] = []
    refusal = session_ctl.set_model("pantheon:3", "sonnet", sleep=_naps(naps))
    assert refusal is None
    # display-message (pane_command), then send-keys -l, then send-keys Enter.
    assert fake.calls[0][0] == "display-message"
    assert fake.calls[1] == ("send-keys", "-t", "pantheon:3", "-l", "/model sonnet")
    assert fake.calls[2] == ("send-keys", "-t", "pantheon:3", "Enter")
    assert len(fake.calls) == 3                      # never combined into one call
    assert naps == [0.3]                              # paused before Enter


def test_effort_types_the_slash_command(monkeypatch):
    fake = FakeRun(pane_command="node")               # Claude Code is a Node app
    monkeypatch.setattr(tmuxctl, "run", fake)
    refusal = session_ctl.set_effort("pantheon:4", "high", sleep=lambda s: None)
    assert refusal is None
    assert fake.calls[1] == ("send-keys", "-t", "pantheon:4", "-l", "/effort high")


def test_a_window_not_running_claude_is_refused_before_anything_is_typed(monkeypatch):
    fake = FakeRun(pane_command="bash")
    monkeypatch.setattr(tmuxctl, "run", fake)
    refusal = session_ctl.set_model("pantheon:5", "opus", sleep=lambda s: None)
    assert refusal == session_ctl.REFUSED_NOT_CLAUDE
    assert len(fake.calls) == 1                       # only the pane_command lookup happened


def test_a_full_windows_path_still_counts_as_claude(monkeypatch):
    fake = FakeRun(pane_command="C:\\Home\\x\\.local\\bin\\claude.exe")
    monkeypatch.setattr(tmuxctl, "run", fake)
    assert session_ctl.set_effort("pantheon:3", "low", sleep=lambda s: None) is None


def test_mode_presses_shift_tab_the_right_number_of_times(monkeypatch):
    fake = FakeRun(pane_command="claude")
    monkeypatch.setattr(tmuxctl, "run", fake)
    refusal = session_ctl.set_mode("pantheon:3", "auto", "plan", sleep=lambda s: None)
    assert refusal is None
    btabs = [c for c in fake.calls if c[-1] == "BTab"]
    assert len(btabs) == 3                            # auto -> default -> acceptEdits -> plan
    assert session_ctl.mode_steps("auto", "plan") == 3
    assert session_ctl.mode_steps("plan", "auto") == 1
    assert session_ctl.mode_steps("default", "default") == 0


def test_mode_refuses_when_the_current_mode_was_never_recorded(monkeypatch):
    fake = FakeRun(pane_command="claude")
    monkeypatch.setattr(tmuxctl, "run", fake)
    refusal = session_ctl.set_mode("pantheon:3", None, "plan", sleep=lambda s: None)
    assert refusal == session_ctl.REFUSED_UNKNOWN_MODE
    assert not any(c[-1] == "BTab" for c in fake.calls)


def test_mode_refuses_an_unknown_target_mode(monkeypatch):
    fake = FakeRun(pane_command="claude")
    monkeypatch.setattr(tmuxctl, "run", fake)
    refusal = session_ctl.set_mode("pantheon:3", "auto", "yolo", sleep=lambda s: None)
    assert refusal == "'yolo' is not a permission mode this deck knows"


def test_a_failed_send_keys_is_reported_not_silently_swallowed(monkeypatch):
    fake = FakeRun(pane_command="claude", fail_on=1)   # the -l send-keys call fails
    monkeypatch.setattr(tmuxctl, "run", fake)
    refusal = session_ctl.set_model("pantheon:3", "opus", sleep=lambda s: None)
    assert refusal == "tmux would not accept that command"
