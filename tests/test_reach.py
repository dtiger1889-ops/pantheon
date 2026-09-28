"""step 1/5: the reachability probe never hangs, and reads a clean exit 0 as reachable --
anything else (non-zero exit, timeout, missing binary) as not reachable. Every case is driven
through a fake runner; no real ssh/tmux call happens here."""
from __future__ import annotations

import subprocess

from pantheon.dispatch import reach


def test_reachable_on_exit_zero():
    def fake_run(argv, **kwargs):
        joined = " ".join(argv)
        assert "has-session -t pantheon" in joined
        assert "BatchMode=yes" in argv
        return subprocess.CompletedProcess(argv, 0, "", "")

    assert reach.pantheon_tmux_reachable(runner=fake_run) is True


def test_not_reachable_on_nonzero_exit():
    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, "", "can't find session pantheon")

    assert reach.pantheon_tmux_reachable(runner=fake_run) is False


def test_not_reachable_on_timeout_never_raises():
    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout", 5))

    assert reach.pantheon_tmux_reachable(runner=fake_run) is False


def test_not_reachable_on_missing_ssh_binary():
    def fake_run(argv, **kwargs):
        raise OSError("no such file or directory: ssh")

    assert reach.pantheon_tmux_reachable(runner=fake_run) is False


def test_probe_command_sets_connect_timeout_and_session_name():
    argv = reach.probe_command("my-session", connect_timeout=3)
    joined = " ".join(argv)
    assert "ConnectTimeout=3" in joined
    assert "has-session -t my-session" in joined
    assert "BatchMode=yes" in argv
    assert argv[-2] == "localhost"


def test_probe_uses_a_different_session_name_when_asked():
    argv = reach.probe_command("throwaway")
    assert "has-session -t throwaway" in " ".join(argv)
