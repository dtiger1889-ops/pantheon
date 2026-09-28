"""What the last fresh session in a folder was started with.

The row toolbar's `H` (hand off to a fresh session in the same folder) asks the old session to
checkpoint, then opens the ordinary New-session steps for that folder. Those steps pre-highlight
the model / effort / mode the folder's newest `new_session` launch used, so the usual answer is
one Enter per screen. This module is the pure read behind that: one pass over the parsed event
log, no tmux, no files -- the same `new_session.extra.launch_options` that
`dispatch/projects.start_session` writes.


"""
from __future__ import annotations

from datetime import datetime
from typing import Iterable, Optional

from ..events import norm_path
from ..models import Event

# Only these three carry over; `resume` (an old session id, or "continue") is about that launch,
# never the next one.
CARRIED = ("model", "effort", "mode")


def last_options_for(cwd: Optional[str], events: Iterable[Event], provider: str = "claude") -> dict:
    """`{"model": ..., "effort": ..., "mode": ...}` (any subset, possibly empty) from the newest
    `new_session` event whose folder is `cwd` and whose provider is `provider`. Another folder's
    launches, a failed launch, and a launch with a different provider (a Codex model name would be
    nonsense on Claude's picker) are all ignored. Nothing found -> `{}`, never a guess."""
    want = norm_path(cwd)
    if not want:
        return {}
    newest: Optional[Event] = None
    newest_when = None
    for e in events:
        if e.event != "new_session" or norm_path(e.cwd) != want:
            continue
        if (e.extra.get("provider") or provider) != provider:
            continue
        when = e.when
        if newest is None or (when is not None and (newest_when is None or when >= newest_when)):
            newest, newest_when = e, when
    if newest is None:
        return {}
    chosen = newest.extra.get("launch_options") or {}
    if not isinstance(chosen, dict):
        return {}
    return {k: chosen[k] for k in CARRIED if chosen.get(k)}


def checkpoint_settled(session_id: Optional[str], since: datetime, events: Iterable[Event],
                       checkpoint_mtime: Optional[datetime] = None) -> Optional[str]:
    """Has the old session finished what `H` asked of it, since `since` (UTC, when `/checkpoint`
    was typed)? `"ended"` when its `SessionEnd` arrived (the spec's own signal); `"saved"` when
    the folder's `CHECKPOINT.md` was rewritten after `since` AND the session then finished a turn
    (`Stop`) -- typing `/checkpoint` never ends a Claude Code session by itself, so without this
    the wait would only ever end when the user closed the old window by hand. `None` = keep waiting.

    A `Stop` alone is not enough: on a row that was mid-turn, the first `Stop` after the typing is
    the turn it was already on, before the checkpoint ran. Times are compared to the whole second
    (the hook writes second-resolution timestamps)."""
    if not session_id:
        return None
    since = since.replace(microsecond=0)
    stops: list[datetime] = []
    for e in events:
        if e.session_id != session_id:
            continue
        when = e.when
        if when is None or when < since:
            continue
        if e.event == "SessionEnd":
            return "ended"
        if e.event == "Stop" and not e.agent_id:
            stops.append(when)
    if checkpoint_mtime is not None:
        saved_at = checkpoint_mtime.replace(microsecond=0)
        if saved_at >= since and any(w >= saved_at for w in stops):
            return "saved"
    return None
