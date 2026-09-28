"""Test-wide guard: no test may reach a real tmux server.

Why: the deck's panes fall back to the real `tmux` when no window source is injected,
and the queue pane asks tmux on every redraw. Run from a Claude Desktop session (Windows session 1)
against the user's live server (started over SSH, Windows session 0), each such call spawned a tmux
client that hung forever instead of failing; the 5-second cap in `tmuxctl.run` killed nothing (the
hung client outlives the kill), the clients piled up (five in an hour), and the live server wedged
-- the deck restart's `kill-session` hung behind them. So every test gets a tmux that answers
"not available" instantly. A test that genuinely wants the real thing marks itself
`@pytest.mark.real_tmux` (none do today)."""
from __future__ import annotations

import subprocess

import pytest

from pantheon import tmuxctl


def _no_tmux(*args, tmux=None, check=False):
    argv = [tmuxctl.tmux_path(tmux), *args]
    if check:
        raise subprocess.CalledProcessError(1, argv, "", "tmux is disabled under the test suite")
    return subprocess.CompletedProcess(argv, 1, "", "tmux is disabled under the test suite")


def pytest_configure(config):
    config.addinivalue_line("markers", "real_tmux: let this test talk to a real tmux server")


@pytest.fixture(autouse=True)
def _tmux_is_never_real(request, monkeypatch):
    if request.node.get_closest_marker("real_tmux"):
        yield
        return
    monkeypatch.setattr(tmuxctl, "run", _no_tmux)
    tmuxctl._WINDOWS_CACHE.clear()
    yield
    tmuxctl._WINDOWS_CACHE.clear()


@pytest.fixture(autouse=True)
def _dispatch_memory_is_never_real(monkeypatch, tmp_path):
    """The Assistant auto-finds Claude Desktop's Dispatch memory under %APPDATA%; tests must not
    see the user's real notes (a test that wants memory passes `[assistant] memory` explicitly)."""
    from pantheon import assistant
    monkeypatch.setattr(assistant, "DISPATCH_SESSION_ROOTS", [tmp_path / "no-dispatch-here"])
    yield


@pytest.fixture(autouse=True)
def _stage_joins_in_tests(monkeypatch):
    """`stage.JOIN_PANES` is off on the live deck since 2026-09-27 (a join wedged tmux); the
    tests keep covering the join path against the fake tmux, so they turn it back on."""
    from pantheon import stage
    monkeypatch.setattr(stage, "JOIN_PANES", True)
    yield


@pytest.fixture(autouse=True)
def _model_usage_is_never_real(request, monkeypatch):
    """The model picker ranks by when each model was last used, read from the live
    `~/.claude/projects` transcripts by default (`Config.claude_home`). A test must never see
    The user's real history, or the order on screen would change from one day to the next: every
    test starts with no usage at all, and a test that wants some patches `load_usage` itself."""
    from pantheon import model_picker

    monkeypatch.setattr(model_picker, "load_usage", lambda cfg: {"claude": {}, "codex": {}})
    model_picker._MEMO.clear()
    yield


@pytest.fixture(autouse=True)
def _account_usage_is_never_real(monkeypatch, tmp_path_factory):
    """The usage collector may read the account's usage from Anthropic with Claude Code's login
. No test may touch the network or
    the real login on this PC: the default login path points at a folder that does not exist, and
    the default HTTP layer fails the test. Tests of the reader inject a fake login file and a fake
    HTTP function of their own."""
    from pantheon.hud import account

    missing = tmp_path_factory.mktemp("no-login") / "absent" / "login.json"
    monkeypatch.setattr(account, "default_credentials_path", lambda cfg: missing)

    def _no_network(*args, **kwargs):
        raise AssertionError("a test tried to reach the real usage endpoint")

    monkeypatch.setattr(account, "http_get", _no_network)
    yield
