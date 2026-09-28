"""The pit: tmux-native "every agent side by side".

Built on `join-pane`/`select-layout`/`break-pane` -- real terminals, tmux-drawn, never a Python
terminal-emulation widget. `enter_pit` moves every live agent window's pane into
one tiled `PIT` window; `leave_pit` moves every pane back out to its own window, unharmed, same
name, same running process -- nothing is duplicated or killed either way.

Because this moves panes (never copies them), an agent is never shown twice; `tmuxctl.list_windows`
already collapses the grouped-session duplicate a per-client attach produces (`dedupe_grouped`),
so `agent_windows` inherits that guard for free and never joins the same pane under two names.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from .. import config as config_mod
from .. import tmuxctl

PIT_WINDOW = "PIT"

# Never joined into the pit -- the deck's own fixed windows, and
# the pinned Assistant, which stays in its own window.
RESERVED_WINDOWS = {"DECK", "QUEUE", "BUDGET", "NOTES", PIT_WINDOW, "ASSISTANT"}


def _state_file(cfg: config_mod.Config) -> Path:
    """Where the pit remembers each pane's original window name, across the separate `--pit` /
    `--unpit` shell invocations (two different process runs, so this cannot just live in memory).
    Runtime state, like everything else under `cfg.state_dir` -- gitignored, never read as config."""
    return Path(cfg.state_dir) / "pit.json"


def agent_windows(session: str, tmux: Optional[str] = None) -> list:
    """Every window in `session` that is not one of the deck's own fixed windows or the pit
    itself."""
    return [w for w in tmuxctl.list_windows(session, tmux=tmux) if w.name not in RESERVED_WINDOWS]


def is_active(cfg: config_mod.Config) -> bool:
    """Whether the pit is currently entered (the deck's `p` key and `pantheon --pit`/`--unpit`
    both need to know this before acting again)."""
    return _state_file(cfg).exists()


def enter_pit(cfg: config_mod.Config, tmux: Optional[str] = None) -> dict:
    """Join every agent window's pane into one tiled `PIT` window.

    Returns `{"joined": [names], "pit_index": index or None, "already": bool}`. Never raises --
    a join that fails for one window just leaves that window where it was; the caller reports
    what actually happened rather than assuming success.
    """
    session = cfg.tmux_session
    if is_active(cfg):
        return {"joined": [], "pit_index": tmuxctl.window_index_by_name(session, PIT_WINDOW, tmux=tmux),
                "already": True}
    windows = agent_windows(session, tmux)
    if not windows:
        return {"joined": [], "pit_index": None, "already": False}

    # Pane ids first, before anything is renamed or joined -- tmux reuses window indices the
    # moment a window disappears (join-pane destroys a source window once its last pane leaves),
    # so an index captured after the first join would already point at something else.
    panes = [(w, tmuxctl.pane_id(f"{session}:{w.index}", tmux=tmux)) for w in windows]
    original: dict[str, str] = {}   # pane_id -> the window name it should get back

    first_window, first_pane = panes[0]
    original[first_pane] = first_window.name
    # The first agent window becomes PIT itself -- join-pane needs a real window to join INTO,
    # and renaming this one (reversible in leave_pit()) avoids creating an extra empty window.
    tmuxctl.run("rename-window", "-t", f"{session}:{first_window.index}", PIT_WINDOW, tmux=tmux)
    pit_target = f"{session}:{PIT_WINDOW}"

    joined = [first_window.name]
    for window, pane in panes[1:]:
        if tmuxctl.join_pane(pane, pit_target, tmux=tmux):
            original[pane] = window.name
            joined.append(window.name)
        # A failed join leaves that window exactly where it was -- nothing else to undo for it.

    tmuxctl.select_layout(pit_target, "tiled", tmux=tmux)
    tmuxctl.run("display-panes", "-t", pit_target, tmux=tmux)   # briefly numbers each pane on screen

    pit_index = tmuxctl.window_index_by_name(session, PIT_WINDOW, tmux=tmux)
    _state_file(cfg).parent.mkdir(parents=True, exist_ok=True)
    _state_file(cfg).write_text(json.dumps(original), encoding="utf-8")
    return {"joined": joined, "pit_index": pit_index, "already": False}


def leave_pit(cfg: config_mod.Config, tmux: Optional[str] = None) -> list[str]:
    """Break every pane in `PIT` back out into its own window, named what it was named before
    `enter_pit()` (falling back to the pane id if that record is somehow missing). Returns the
    names restored, in tmux's pane order. A no-op (returns `[]`) when the pit is not active."""
    session = cfg.tmux_session
    state_path = _state_file(cfg)
    if not state_path.exists():
        return []
    try:
        original: dict[str, str] = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        original = {}

    restored: list[str] = []
    pit_index = tmuxctl.window_index_by_name(session, PIT_WINDOW, tmux=tmux)
    if pit_index is not None:
        target = f"{session}:{pit_index}"
        pane_ids = tmuxctl.list_panes(target, tmux=tmux)
        # `break-pane` needs at least one pane to remain in the source window, so the LAST pane
        # simply keeps window `pit_index` and gets its old name back via `rename-window`; every
        # other pane moves out first.
        for pane_id in pane_ids[:-1]:
            name = original.get(pane_id) or pane_id
            if tmuxctl.break_pane(pane_id, dst_name=name, tmux=tmux) is not None:
                restored.append(name)
        if pane_ids:
            last_id = pane_ids[-1]
            name = original.get(last_id) or last_id
            tmuxctl.run("rename-window", "-t", target, name, tmux=tmux)
            restored.append(name)

    try:
        state_path.unlink()
    except OSError:
        pass
    return restored
