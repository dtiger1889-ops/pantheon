"""Type the picked model / effort / permission mode into a Claude Code window that just opened
.

The deck's New-session flow (`supervisor/pane.py`'s `_apply_launch_options`) and the shell's
`pantheon open ... --model M --effort E` both need exactly this, so it lives here once. Nothing in
it is Textual-specific: three `session_ctl` calls on a tmux target, each refusal turned into a
note for whoever reports back, never a crash (the session itself already started fine).
"""
from __future__ import annotations

from typing import Optional

from .. import session_ctl


def apply(target: str, options: dict, tmux: Optional[str] = None) -> list[str]:
    """`target` is `"pantheon:7"`; `options` is `{"model": ..., "effort": ..., "mode": ...}` with
    any of them missing or empty. Returns one note per setting it tried, in that order."""
    notes: list[str] = []
    model = options.get("model")
    if model:
        refusal = session_ctl.set_model(target, model, tmux=tmux)
        notes.append(refusal or f"model set to {model}")
    effort = options.get("effort")
    if effort:
        refusal = session_ctl.set_effort(target, effort, tmux=tmux)
        notes.append(refusal or f"effort set to {effort}")
    mode = options.get("mode")
    if mode:
        # A freshly launched window has no recorded mode yet -- `set_mode` needs a `current` to
        # count Shift+Tab presses from, so start it from the cycle's own first entry (`auto`), the
        # same assumption the deck's New-session flow has made since v2 step 2.
        refusal = session_ctl.set_mode(target, session_ctl.MODE_CYCLE[0], mode, tmux=tmux)
        notes.append(refusal or f"mode set toward {mode}")
    return notes
