"""Pure decision functions for the session-limit governor. No files, no subprocesses, no tmux, no clock of its own: every function
takes what it needs (a usage picture, the settings, what was already done, `now`) and returns
data, so the whole policy is unit-testable without a running agent or a real tmux server.
`runner.py` is the only place any of this touches the real world.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from ..models import AgentState, AgentStatus, parse_ts

WINDOWS = ("five_hour", "seven_day")
_PCT_FIELD = {"five_hour": "five_hour_pct", "seven_day": "seven_day_pct"}
_RESET_FIELD = {"five_hour": "five_hour_resets_at", "seven_day": "seven_day_resets_at"}

# A session in one of these states is doing something the governor may interrupt with the
# wind-down message. Anything else (already winding down, parked, gone, a Codex job) is left
# alone.
WIND_DOWN_ELIGIBLE = (
    AgentStatus.WORKING,
    AgentStatus.WAITING_INPUT,
    AgentStatus.BLOCKED_PERMISSION,
    AgentStatus.IDLE,
)

# The resume ladder is rungs 1..4: 1 = `claude --resume <id>`, 2 = `claude -c`,
# 3 = a fresh session pointed at CHECKPOINT.md, 4 = give up.
MAX_RUNG = 4


def _get(usage: Any, field_name: str) -> Any:
    """Read a field off a `ProviderUsage` or a plain dict with the same field names (the
    `state/hud.json` picture round-trips through `vars()`, so both shapes show up)."""
    if usage is None:
        return None
    return usage.get(field_name) if isinstance(usage, dict) else getattr(usage, field_name, None)


# --------------------------------------------------------------------------- 1. threshold crossing


@dataclass(frozen=True)
class Crossing:
    """One usage window that just crossed its configured wind-down threshold and has not
    already been acted on for this reset period."""

    window: str  # "five_hour" | "seven_day"
    percent: float
    resets_at: Optional[str]


def hud_is_stale(fetched_at: Optional[str], now: Optional[datetime] = None, max_age_seconds: int = 120) -> bool:
    """True when `state/hud.json` is old enough that the runner must read the usage sources
    directly instead. A missing/unreadable
    timestamp counts as stale -- better to re-check than to trust nothing."""
    when = parse_ts(fetched_at)
    if when is None:
        return True
    now = now or datetime.now(timezone.utc)
    return (now - when).total_seconds() > max_age_seconds


def check_windows(usage: Any, thresholds: dict, acted: dict) -> list[Crossing]:
    """Which usage windows just crossed their configured threshold.

    `usage` is Claude's `ProviderUsage` (or the same-shaped dict out of `state/hud.json`).
    `thresholds` is `[governor.wind_down_at_percent]` (`{"five_hour": 85, "seven_day": 90}`).
    `acted` maps window name -> the `resets_at` the governor already wound down for; the same
    crossing does not fire again every tick until the window turns over. A missing/unknown
    percent NEVER crosses -- a failed usage read must never trigger an action."""
    out: list[Crossing] = []
    for window in WINDOWS:
        threshold = thresholds.get(window) if thresholds else None
        if threshold is None:
            continue
        pct = _get(usage, _PCT_FIELD[window])
        if pct is None:
            continue
        try:
            pct = float(pct)
        except (TypeError, ValueError):
            continue
        if pct < float(threshold):
            continue
        resets_at = _get(usage, _RESET_FIELD[window]) or ""
        if (acted or {}).get(window) == resets_at:
            continue
        out.append(Crossing(window=window, percent=pct, resets_at=resets_at or None))
    return out


def agents_to_wind_down(rows: Iterable[AgentState]) -> list[AgentState]:
    """Live Claude sessions the wind-down message goes to: never a
    Codex job, and never a row already winding down, parked, or gone."""
    return [r for r in rows if r.provider == "claude" and r.status in WIND_DOWN_ELIGIBLE]


# --------------------------------------------------------------------------- 2. parking


@dataclass(frozen=True)
class ParkDecision:
    session_id: str
    should_park: bool
    reason: str = ""  # "checkpointed" | "grace_expired" | "max_parked"


def parking_decisions(
    pending: dict, ended_session_ids: Iterable[str], now: datetime, already_parked: Iterable[str], max_parked: int
) -> list[ParkDecision]:
    """For every session with a grace clock running, decide whether it parks now.

    A session parks when EITHER its `SessionEnd` arrived (`ended_session_ids`, reason
    `checkpointed`) OR its grace clock ran out (reason `grace_expired`) -- section 6. Never the
    same session twice: anything already in `already_parked` is skipped outright. Past
    `max_parked`, no more decisions come back `should_park=True`; the caller writes one
    `park_refused` line for each (reason `max_parked`) instead of killing anything."""
    ended = set(ended_session_ids or ())
    parked_ids = set(already_parked or ())
    decisions: list[ParkDecision] = []
    parked_count = len(parked_ids)
    for session_id, entry in (pending or {}).items():
        if session_id in parked_ids:
            continue
        is_ended = session_id in ended
        deadline = parse_ts(entry.get("parked_at_deadline")) if isinstance(entry, dict) else None
        expired = deadline is not None and now >= deadline
        if not (is_ended or expired):
            continue
        if parked_count >= max_parked:
            decisions.append(ParkDecision(session_id, False, "max_parked"))
            continue
        reason = "checkpointed" if is_ended else "grace_expired"
        decisions.append(ParkDecision(session_id, True, reason))
        parked_count += 1
    return decisions


# --------------------------------------------------------------------------- 3. resume


def ready_to_resume(parked_row: dict, now: datetime) -> bool:
    """`now >= resume_after`."""
    when = parse_ts((parked_row or {}).get("resume_after"))
    return when is not None and now >= when


def already_resumed(
    session_id: str, cwd: Optional[str], windows: Iterable, builtin_events: Iterable
) -> Optional[str]:
    """The user or Claude Code's own wait beat the governor to it: a live
    tmux pane at the parked `cwd` running an agent, or a `SessionStart` / a
    `quota_auto_resume_fired` Notification for this session dated after it parked. Returns who
    did it (`"the user"` / `"claude-code-builtin"`), or None when the governor's own resume is
    still the only one coming."""
    from ..events import norm_path
    from ..supervisor.state import command_name

    target = norm_path(cwd) if cwd else ""
    if target:
        for w in windows or ():
            path = getattr(w, "path", None)
            command = getattr(w, "command", None)
            if norm_path(path) == target and command_name(command) in ("node", "claude", "codex"):
                return "the user"
    for e in builtin_events or ():
        if getattr(e, "session_id", None) != session_id:
            continue
        name = (getattr(e, "event", "") or "").lower()
        nt = (getattr(e, "notification_type", "") or "").lower()
        if name == "sessionstart" or nt == "quota_auto_resume_fired":
            return "claude-code-builtin"
    return None


def next_rung(current: int = 0) -> int:
    """One step down the resume ladder, clamped to `MAX_RUNG`; `current=0`
    (never tried) starts at rung 1."""
    return min(max(int(current or 0), 0) + 1, MAX_RUNG)


def resume_order(parked: Iterable[dict]) -> list[str]:
    """Oldest first."""
    ordered = sorted((parked or ()), key=lambda p: (p or {}).get("parked_at") or "")
    return [p["session_id"] for p in ordered if p.get("session_id")]
