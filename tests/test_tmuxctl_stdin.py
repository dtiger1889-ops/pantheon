"""Control clients must not inherit the pane terminal, including after an MSYS fork retry."""
import subprocess

import pytest

from pantheon import tmuxctl

# Keep the actual wrapper while conftest replaces the public entry point to protect live tmux.
_run_control = tmuxctl.run


@pytest.mark.parametrize("retry", [False, True])
def test_control_disconnects_stdin_and_preserves_result(monkeypatch, retry):
    calls = []
    expected = subprocess.CompletedProcess(["fake-tmux", "list-windows"], 0,
                                           "0|DECK\n", "diagnostic\n")

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is True
        assert kwargs["check"] is True
        assert kwargs["timeout"] == tmuxctl.TMUX_TIMEOUT_SECONDS
        if retry and len(calls) == 1:
            raise BlockingIOError("MSYS fork temporarily unavailable")
        return expected

    monkeypatch.setattr(tmuxctl, "tmux_path", lambda preferred: "fake-tmux")
    monkeypatch.setattr(tmuxctl.subprocess, "run", fake_run)
    monkeypatch.setattr("time.sleep", lambda _: None)

    result = _run_control("list-windows", tmux="preferred-tmux", check=True)

    assert len(calls) == (2 if retry else 1)
    assert all(argv == ["fake-tmux", "list-windows"] for argv, _ in calls)
    assert result is expected
    assert result.stdout == "0|DECK\n"
    assert result.stderr == "diagnostic\n"


@pytest.mark.parametrize("error", [BlockingIOError(11, "Resource temporarily unavailable"),
                                   OSError(12, "Cannot allocate memory")])
def test_a_fork_that_keeps_failing_is_a_failed_call_not_an_exception(monkeypatch, error):
    """2026-10-07: two EAGAINs in a row escaped `run` as an exception; under low memory any
    caller without its own guard would take the deck down."""
    def fake_run(argv, **kwargs):
        raise error

    monkeypatch.setattr(tmuxctl, "tmux_path", lambda preferred: "fake-tmux")
    monkeypatch.setattr(tmuxctl.subprocess, "run", fake_run)
    monkeypatch.setattr("time.sleep", lambda _: None)

    result = _run_control("list-windows")

    assert result.returncode == 126
    assert result.stdout == ""
    assert "could not start" in result.stderr
    with pytest.raises(subprocess.CalledProcessError):
        _run_control("list-windows", check=True)
