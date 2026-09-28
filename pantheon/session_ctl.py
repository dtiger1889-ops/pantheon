"""The typing layer: turns a control action the user picked on
the deck into keystrokes sent to one Claude Code window in the pantheon tmux session.

Safety rule this whole module exists to enforce: the deck types only `/model <alias>`,
`/effort <level>`, `BTab` (Shift+Tab), and `/checkpoint` -- never a prompt, never a file path -- and only into a
tmux window whose pane is actually running Claude Code. Every write is `send-keys -l <text>` and
then `Enter` as a SEPARATE tmux call, the same shape `tmuxctl.send_text` already uses, with a short
pause between them (short commands do not hit the paste-then-Enter swallow `providers/claude.py`
works around for long briefings, but the pause is cheap insurance).

Pure `tmuxctl.run` calls, no state of its own, so a test can hand this a fake `run` (via a
`tmux=` path is not enough on its own -- tests monkeypatch `pantheon.tmuxctl.run`) and a fake
`sleep`, and never touch a real tmux server.
"""
from __future__ import annotations

import time
from typing import Optional

from . import tmuxctl

# Shift+Tab's cycle, in the order it actually cycles.
MODE_CYCLE = ["auto", "default", "acceptEdits", "plan"]

# The two program names tmux reports for a Claude Code pane -- it is a Node app (`supervisor/state.py`
# `AGENT_COMMANDS` has the same pair for the same reason).
_CLAUDE_COMMANDS = {"node", "claude"}

REFUSED_NOT_CLAUDE = "that window is not running Claude Code in this tmux - nothing was typed"
REFUSED_UNKNOWN_MODE = "current mode unknown; press p inside that window once"

_TYPE_PAUSE_SECONDS = 0.3
_STEP_PAUSE_SECONDS = 0.05


def _bare(command: str) -> str:
    """`C:\\...\\claude.exe` -> `claude`; tmux reports a full Windows path for a dispatched
    window (the same normalisation `supervisor/state.command_name` does)."""
    base = (command or "").replace("\\", "/").rsplit("/", 1)[-1].lower()
    return base[:-4] if base.endswith(".exe") else base


def is_claude_window(target: str, tmux: Optional[str] = None) -> bool:
    """True only when `target` (`"pantheon:3"`) is a live pane running Claude Code right now."""
    return _bare(tmuxctl.pane_command(target, tmux=tmux)) in _CLAUDE_COMMANDS


def _type_and_enter(target: str, text: str, tmux: Optional[str] = None, sleep=None) -> bool:
    sleep = sleep or time.sleep
    if tmuxctl.run("send-keys", "-t", target, "-l", text, tmux=tmux).returncode != 0:
        return False
    sleep(_TYPE_PAUSE_SECONDS)
    return tmuxctl.run("send-keys", "-t", target, "Enter", tmux=tmux).returncode == 0


def set_model(target: str, alias: str, tmux: Optional[str] = None, sleep=None) -> Optional[str]:
    """Types `/model <alias>` into `target`. Returns a refusal sentence for the footer, or `None`
    on success (the caller reports success in its own words, S-UI section 6.4)."""
    if not is_claude_window(target, tmux):
        return REFUSED_NOT_CLAUDE
    if not _type_and_enter(target, f"/model {alias}", tmux=tmux, sleep=sleep):
        return "tmux would not accept that command"
    return None


def set_effort(target: str, level: str, tmux: Optional[str] = None, sleep=None) -> Optional[str]:
    """Types `/effort <level>` into `target`. Returns a refusal sentence, or `None` on success."""
    if not is_claude_window(target, tmux):
        return REFUSED_NOT_CLAUDE
    if not _type_and_enter(target, f"/effort {level}", tmux=tmux, sleep=sleep):
        return "tmux would not accept that command"
    return None


def request_checkpoint(target: str, tmux: Optional[str] = None, sleep=None) -> Optional[str]:
    """Types `/checkpoint` into `target`. Only ever types --
    never closes anything. Returns a refusal sentence, or `None` on success."""
    if not is_claude_window(target, tmux):
        return REFUSED_NOT_CLAUDE
    if not _type_and_enter(target, "/checkpoint", tmux=tmux, sleep=sleep):
        return "tmux would not accept that command"
    return None


def mode_steps(current: str, wanted: str) -> int:
    """How many `Shift+Tab` presses walk `current` to `wanted` around the cycle."""
    return (MODE_CYCLE.index(wanted) - MODE_CYCLE.index(current)) % len(MODE_CYCLE)


def set_mode(
    target: str, current: Optional[str], wanted: str, tmux: Optional[str] = None, sleep=None
) -> Optional[str]:
    """Presses `BTab` (Shift+Tab) the distance from `current` to `wanted` around
    `auto -> default -> acceptEdits -> plan -> auto`. Refuses rather than guess when `current`
    was never recorded."""
    if not is_claude_window(target, tmux):
        return REFUSED_NOT_CLAUDE
    if current not in MODE_CYCLE:
        return REFUSED_UNKNOWN_MODE
    if wanted not in MODE_CYCLE:
        return f"'{wanted}' is not a permission mode this deck knows"
    steps = mode_steps(current, wanted)
    sleep = sleep or time.sleep
    for _ in range(steps):
        if tmuxctl.run("send-keys", "-t", target, "BTab", tmux=tmux).returncode != 0:
            return "tmux would not send Shift+Tab"
        sleep(_STEP_PAUSE_SECONDS)
    return None
