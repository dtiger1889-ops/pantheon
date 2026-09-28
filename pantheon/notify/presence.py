"""Is the user actually looking at the deck right now?.

Two independent signals, both cheap, neither a Win32 call (the deck runs under MSYS2 Python --
`GetForegroundWindow`/`GetLastInputInfo` are not portable here, so this is the portable
equivalent the research file settled on, Part A / A2 / A4):

  1. `state/presence` -- a file the deck touches on every key or mouse event (mirrors Claude
     Code's own `CLAUDE_CLIENT_PRESENCE_FILE` trick: a stat check, not a socket).
  2. `tmux list-clients -F '#{client_tty}|#{client_activity}'` -- whether any client is attached
     to the pantheon session at all, and when it last did something.

`Presence` is pure data; `is_present` is a pure function of that data plus a threshold and a
clock, so the rules in `rules.py` can decide presence without ever touching a file or a
subprocess themselves. `touch` and `read_client_activity` below are the two functions that
actually do I/O, called once per tick by `runner.py` (and `touch` also by the deck itself --
see its docstring).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..models import parse_ts, utcnow_iso


@dataclass(frozen=True)
class Presence:
    """What is known about whether a human is at the controls right now.

    The spec names this `Presence(deck_touched_at, client_attached, phone_attached)`; the third
    field there is described as "when [the client] last did something", which is a timestamp,
    not a boolean -- so this build names it `client_activity_at` to match what it actually holds
    rather than carry a field whose name contradicts its type. Nothing else about the shape
    changes: two signals in, one presence verdict out (`is_present`).
    """

    deck_touched_at: Optional[str] = None       # ISO-8601 UTC; last state/presence touch
    client_attached: bool = False               # any tmux client attached to the pantheon session
    client_activity_at: Optional[str] = None    # ISO-8601 UTC; that client's last activity


def is_present(p: Presence, present_seconds: int, now: Optional[datetime] = None) -> bool:
    """step 2's rule, verbatim: "a client is attached AND (presence touched within
    present_seconds OR client_activity within present_seconds)". No client attached at all is
    never present, however recent the other timestamps look."""
    if p is None or not p.client_attached:
        return False
    now = now or datetime.now(timezone.utc)
    window = max(0, int(present_seconds or 0))
    for ts in (p.deck_touched_at, p.client_activity_at):
        when = parse_ts(ts)
        if when is not None and (now - when).total_seconds() <= window:
            return True
    return False


# --------------------------------------------------------------------------- I/O (runner + deck)


def touch(cfg) -> None:
    """Record "a human just did something at the deck" by writing the current time to
    `state/presence`. Called from `deck/app.py` on every key or mouse event
    and every 30s while the deck has focus -- wired there by, which owns that file; this
    function is exposed here so that wiring is a one-line call, not a re-implementation. Never
    raises: a presence file Pantheon could not write just means the deck-touch signal is
    unavailable this tick, and `is_present` still has tmux client activity to fall back on."""
    try:
        path = Path(cfg.presence_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(utcnow_iso(), encoding="utf-8")
    except OSError:
        pass


def read_deck_touch(cfg) -> Optional[str]:
    """The ISO timestamp last written by `touch()`, or None if the file is missing/unreadable.
    Reading the file's own content (not its mtime) means a copied or restored file cannot claim
    a presence it never had, and a test can write it directly without touching the filesystem
    clock."""
    try:
        text = Path(cfg.presence_file).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    return text or None


def read_client_activity(session: str, tmux: Optional[str] = None) -> tuple[bool, Optional[str]]:
    """(client_attached, client_activity_at) for the pantheon tmux session, off
    `tmuxctl.list_clients_with_activity()`. `client_activity` is tmux's own epoch-seconds field;
    converted to ISO-8601 UTC here so `Presence` never carries a raw epoch. No client attached to
    `session` -> `(False, None)`, never raised."""
    from .. import tmuxctl

    latest: Optional[float] = None
    attached = False
    for _tty, sess, activity in tmuxctl.list_clients_with_activity(tmux=tmux):
        if sess != session:
            continue
        attached = True
        try:
            epoch = float(activity)
        except ValueError:
            continue
        if latest is None or epoch > latest:
            latest = epoch
    if not attached:
        return False, None
    iso = None
    if latest is not None:
        try:
            iso = datetime.fromtimestamp(latest, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        except (OSError, OverflowError, ValueError):
            iso = None
    return True, iso


def current(cfg, tmux: Optional[str] = None) -> Presence:
    """The `Presence` snapshot for this tick: read the deck-touch file and the tmux client
    list, and hand back plain data. Never raises -- a failing signal just leaves its field None,
    and `is_present()` treats that as "not present" for that signal, never a crash."""
    attached, activity_at = read_client_activity(cfg.tmux_session, tmux=tmux)
    return Presence(
        deck_touched_at=read_deck_touch(cfg),
        client_attached=attached,
        client_activity_at=activity_at,
    )
