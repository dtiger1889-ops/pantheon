"""Which queue rows currently have an agent on them.

Pure functions over the event log and the live tmux window list; the queue pane calls
`dispatch_marks` on every refresh and looks rows up by id.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from ..models import Event, TmuxWindow, format_age, parse_ts

ENDS_CLAUDE = {"sessionend", "kill"}
ENDS_CODEX = {"done", "failed"}


@dataclass
class DispatchMark:
    tracker_id: str
    provider: str
    ts: str
    where: str = "tmux"
    window_index: Optional[int] = None
    tmux_pane: Optional[str] = None
    job_id: Optional[str] = None

    def age_text(self, now=None) -> str:
        from datetime import datetime, timezone

        t = parse_ts(self.ts)
        if t is None:
            return "?"
        now = now or datetime.now(timezone.utc)
        return format_age(max(0.0, (now - t).total_seconds()))

    def label(self, now=None) -> str:
        """`dispatched to claude, window 3, 4m ago` -- one line under the row."""
        place = f"window {self.window_index}" if self.window_index is not None else self.where.replace("-", " ")
        return f"dispatched to {self.provider}, {place}, {self.age_text(now)} ago"


def _latest_ends(events: list[Event]) -> tuple[dict[str, str], dict[str, str]]:
    """One pass over the event log: the latest END-event timestamp seen for each tmux pane
    (Claude SessionEnd/kill) and each job id (Codex done/failed). `dispatch_marks` used to
    re-scan the *entire* event list once per open mark (`_ended_after`, O(marks x events));
    building these two lookups once and comparing timestamps against them is O(events) total,
    then O(1) per mark -- perf audit 2026-09-05."""
    by_pane: dict[str, str] = {}
    by_job: dict[str, str] = {}
    for e in events:
        name = (e.event or "").lower()
        ts = e.ts or ""
        if name in ENDS_CLAUDE and e.tmux_pane:
            if ts > by_pane.get(e.tmux_pane, ""):
                by_pane[e.tmux_pane] = ts
        if name in ENDS_CODEX and e.job_id:
            if ts > by_job.get(e.job_id, ""):
                by_job[e.job_id] = ts
    return by_pane, by_job


def _ended_after(mark: DispatchMark, by_pane: dict[str, str], by_job: dict[str, str]) -> bool:
    if mark.job_id and by_job.get(mark.job_id, "") > mark.ts:
        return True
    if mark.tmux_pane and by_pane.get(mark.tmux_pane, "") > mark.ts:
        return True
    return False


def dispatch_marks(events: Iterable[Event], windows: Iterable[TmuxWindow]) -> dict[str, DispatchMark]:
    """row id -> its most recent dispatch, with whether it is still active worked out. Inactive
    marks are dropped rather than kept with a flag -- the dict key is the row id."""
    events = list(events or [])
    live_panes = {w.pane_id for w in (windows or []) if w.pane_id}
    latest: dict[str, DispatchMark] = {}
    for e in events:
        if (e.source or "") != "pantheon" or (e.event or "") != "dispatch":
            continue
        row_id = str(e.extra.get("row_id") or "")
        if not row_id:
            continue
        mark = DispatchMark(
            tracker_id=str(e.extra.get("tracker_id") or ""),
            provider=str(e.extra.get("provider") or "?"),
            ts=e.ts or "",
            where=str(e.extra.get("where") or "tmux"),
            window_index=e.extra.get("window_index"),
            tmux_pane=e.tmux_pane,
            job_id=e.job_id,
        )
        if row_id not in latest or mark.ts >= latest[row_id].ts:
            latest[row_id] = mark
    by_pane, by_job = _latest_ends(events)
    out: dict[str, DispatchMark] = {}
    for row_id, mark in latest.items():
        if mark.where == "pc-window":
            continue  # a native Codex window on the PC cannot be observed; never claim it is live
        if _ended_after(mark, by_pane, by_job):
            continue
        if mark.where == "tmux" and mark.tmux_pane and live_panes and mark.tmux_pane not in live_panes:
            continue  # the window is gone, so the agent is too
        out[row_id] = mark
    return out
