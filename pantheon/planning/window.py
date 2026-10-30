"""Open the usage-planning page from the deck, in its own tmux window named `PLAN`.

The deck's `U` key does the same job as `pantheon --plan` without leaving the deck: one reusable
window running `bin/plan`. Pressing `U` again while the page is
open just goes back to it; quitting the page (`q`) closes the window and F1 returns to the deck.
Never raises; the dict says what happened in plain words.
"""
from __future__ import annotations

from typing import Optional

from .. import config as config_mod
from .. import tmuxctl

PLAN_WINDOW = "PLAN"


def command(root: Optional[str] = None) -> str:
    """The shell line the new window runs. A failed start holds the window open so the message
    can be read, instead of the window vanishing."""
    root = (root or config_mod.PROJECT_ROOT.as_posix()).rstrip("/")
    return (f"bash -c '\"{root}/bin/plan\" || "
            "{ echo; echo \"the planning page did not start - details in state/pantheon.log; "
            "press Enter to close\"; read; }'")


def open_window(cfg: config_mod.Config, tmux: Optional[str] = None) -> dict:
    session = cfg.tmux_session
    try:
        index = tmuxctl.window_index_by_name(session, PLAN_WINDOW, tmux=tmux)
        if index is not None:
            ok = tmuxctl.select_window(session, index, tmux=tmux)
            return {"ok": ok, "action": "switched" if ok else "none",
                    "message": "back on the planning page (q closes it)" if ok
                    else "tmux would not switch to the planning page"}
        index = tmuxctl.new_window(session, PLAN_WINDOW, config_mod.PROJECT_ROOT.as_posix(),
                                   command=command(), tmux=tmux)
        if index is not None:
            # new-window selects it only in the base session; each attached client sits in its
            # own grouped session, so show it there too (same as open_or_reuse_shell).
            tmuxctl.select_window(session, index, tmux=tmux)
    except Exception:
        return {"ok": False, "action": "none", "message": "could not open the planning page"}
    if index is None:
        return {"ok": False, "action": "none",
                "message": "tmux would not open the planning page; nothing changed"}
    return {"ok": True, "action": "opened",
            "message": "planning page open (q closes it, F1 comes back to the deck)"}
