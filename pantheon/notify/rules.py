"""The rules table itself. Pure functions only:
no files, no subprocesses, no clock of its own except an injectable `now` -- the same discipline
`governor/policy.py` follows, for the same reason (unit-testable without a running agent or a
real tmux server; `runner.py` is the only place any of this touches the real world).

Three producers of `Notification`, all edge- or event-triggered so nothing fires per raw hook
event:
  `transitions`   -- one `Transition` per `AgentState` row whose status just changed.
  `decide`        -- one `Transition` -> at most one `Notification`, applying the rules table.
  `budget_events` -- HUD band crossings, independent of any session.
Two more cover the governor events that never show up as an `AgentState` transition at all
(the old row is deliberately left alone -- see their own docstrings for why):
  `park_refused_notifications`, `handoff_notifications`.

Dedupe and the nudge ladder both live in `history`, a plain `dict` keyed by `Notification.key`
that `runner.py` persists to `state/notify/last.json` and updates after an actual send -- this
module only reads it, never mutates it, so a test can hand it a literal dict and assert on what
comes back.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from ..models import AgentState, format_age, format_clock, parse_ts
from . import presence as presence_mod

TIER_SILENT = "silent"
TIER_CAUTION = "caution"
TIER_WARNING = "warning"

# "row enters blocked-permission or waiting-input" (rules table rows 1-2): the two AgentStatus
# values that mean a human is wanted, checked with a delay rather than the instant they appear
#.
NEEDS_YOU_STATUSES = ("blocked-permission", "waiting-input")
_NEED_WORD = {"blocked-permission": "permission", "waiting-input": "waiting"}


@dataclass(frozen=True)
class Transition:
    """One session's status, as of one tick: what it was, what it is now, and how long it has
    been that way. `age_seconds` is "time in `to_status`", not "time since the last event" --
    `transitions()` sets it to 0 at the moment of a real edge; `runner.py` re-derives it on
    later ticks (from its own bookkeeping of when the state was entered) so `decide()` can be
    asked again with a growing age for the two delayed-threshold statuses above, without ever
    needing to know how it is being called."""

    session_id: str
    to_status: str
    project: Optional[str] = None
    session_name: str = "?"
    window: Optional[str] = None          # tmux window index as text, e.g. "3"; None off-tmux
    from_status: Optional[str] = None
    age_seconds: float = 0.0
    tracker_id: Optional[str] = None
    job_id: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Notification:
    """`title`/`body` are what actually leaves the machine (ntfy, Telegram) or sits on the lock
    screen (toast) -- OpenClaw's lock-screen-text rule: actor, need, project name,
    window number ONLY. Never a tool command, a file path, prompt text, or a session id --
    those stay on the deck, one `j` away. `detail` is the place for exactly that extra context
    (a log path, a session id) -- it is written to `state/notify/sent.jsonl` and, later, the
    deck's own status line, but `channels.py`'s three senders never see it."""

    title: str
    body: str
    tier: str                              # TIER_SILENT | TIER_CAUTION | TIER_WARNING
    key: str                               # the dedupe unit (state per session, band per window)
    channels: tuple[str, ...] = ()
    detail: str = ""                       # deck/log-only; never pushed to a channel


# --------------------------------------------------------------------------- small helpers


def _num(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt_clock(iso: Optional[str], clock: str = "24h") -> str:
    return format_clock(parse_ts(iso), clock)


def _project_word(t: "Transition") -> str:
    return t.project or t.session_name or "?"


# --------------------------------------------------------------------------- 1. transitions


def transition_for(row: AgentState, prev: dict[str, str], now: Optional[datetime] = None) -> Transition:
    """One `AgentState` row -> the `Transition` describing it right now, `from_status` filled in
    from `prev` (session_id -> its status as of the previous tick). Shared by `transitions()`
    (which keeps only the ones whose status actually changed) and `runner.py` (which also needs
    the SAME shape for rows that have not changed but are still `blocked-permission`/
    `waiting-input` -- the age has grown, and `decide()` must be asked again)."""
    sid = row.session_id
    new_status = row.status.value if hasattr(row.status, "value") else str(row.status)
    return Transition(
        session_id=sid,
        to_status=new_status,
        project=row.project,
        session_name=row.window_name or row.project or (sid[:8] if sid else "?"),
        window=str(row.window_index) if row.window_index is not None else None,
        from_status=(prev or {}).get(sid),
        age_seconds=row.age_seconds(now) or 0.0,
        tracker_id=row.tracker_id,
        job_id=row.job_id,
    )


def transitions(prev: dict[str, str], rows: Iterable[AgentState], now: Optional[datetime] = None) -> list[Transition]:
    """Compare each current row's status to `prev` (session_id -> its previous status string,
    as `runner.py` remembers it tick to tick). A session `runner.py` has never seen before
    counts as a transition from `None` -- a brand-new `failed`/`done` row must still notify."""
    out: list[Transition] = []
    for row in rows or ():
        if not row.session_id:
            continue
        t = transition_for(row, prev, now)
        if t.from_status == t.to_status:
            continue
        out.append(t)
    return out


# --------------------------------------------------------------------------- 2. decide


def decide(
    t: Transition,
    presence: presence_mod.Presence,
    settings: Any,
    history: dict,
    now: Optional[datetime] = None,
    clock: str = "24h",
) -> Optional[Notification]:
    """One `Transition` -> at most one `Notification`, per the rules table. Quiet hours are NOT
    decided here -- see `is_quiet_hours()` -- because a suppressed-by-quiet notification still
    gets logged (`suppressed: "quiet"`), so the caller needs the `Notification` either way and
    only `runner.py` knows the wall-clock policy."""
    now = now or datetime.now(timezone.utc)
    present = presence_mod.is_present(presence, getattr(settings, "present_seconds", 120), now)

    if t.to_status in NEEDS_YOU_STATUSES:
        return _decide_needs_you(t, present, settings, history, now)
    if t.to_status == "failed":
        return _once(_decide_failed(t), history)
    if t.to_status in ("done", "gone") and t.tracker_id:
        return _once(_decide_done(t, present), history)
    if t.to_status == "parked":
        return _once(_decide_parked(t, clock), history)
    if t.to_status == "working" and t.from_status == "parked":
        return _once(_decide_resumed(t), history)
    if t.to_status == "resume-failed":
        return _once(_decide_resume_failed(t), history)
    return None


def _once(n: Optional[Notification], history: dict) -> Optional[Notification]:
    """One-shot dedupe for every edge-triggered branch below (failed/done/parked/resumed/
    resume-failed): if `history` already has this exact key, do not re-fire. This is what makes
    restart safety (acceptance 6) hold even though `runner.py` re-derives `prev_status` from
    scratch on every restart -- a lost "was it already a transition" memory would otherwise look
    like a brand-new transition and re-notify for a state the user already saw and answered. The
    needs-you branch (`_decide_needs_you`) does its own equivalent check because it also needs
    to distinguish "first fire" from "a nudge is due"."""
    if n is None or (history or {}).get(n.key):
        return None
    return n


def _decide_needs_you(t: Transition, present: bool, settings: Any, history: dict, now: datetime) -> Optional[Notification]:
    key = f"{t.session_id}:{t.to_status}"
    entry = (history or {}).get(key) or {}
    needs_after = float(getattr(settings, "needs_you_after_seconds", 60) or 60)

    if not entry:
        if present or t.age_seconds < needs_after:
            return None
        word = _NEED_WORD.get(t.to_status, "waiting")
        window_bit = f" (window {t.window})" if t.window else ""
        body = f"{word} in {_project_word(t)}{window_bit}, {format_age(t.age_seconds)}"
        return Notification(title="Claude needs you", body=body, tier=TIER_CAUTION, key=key, channels=("toast", "ntfy"))

    # Already notified once for this state: at most `max_nudges` follow-ups, `nudge_minutes` apart.
    if present:
        return None
    max_nudges = int(getattr(settings, "max_nudges", 2) or 0)
    if int(entry.get("nudges_sent", 0)) >= max_nudges:
        return None
    last_at = parse_ts(entry.get("last_notified_at"))
    nudge_minutes = float(getattr(settings, "nudge_minutes", 15) or 15)
    if last_at is None or (now - last_at).total_seconds() < nudge_minutes * 60:
        return None
    body = f"{_project_word(t)}, {format_age(t.age_seconds)}"
    return Notification(title="Still waiting", body=body, tier=TIER_CAUTION, key=key, channels=("ntfy",))


def _decide_failed(t: Transition) -> Notification:
    key = f"{t.session_id}:failed"
    exit_code = t.extra.get("exit_code", "?")
    log_path = t.extra.get("log_path") or (f"state/dispatch/{t.job_id}.log" if t.job_id else "the job log")
    # Lock-screen text only (rules table's OpenClaw note): the pushed body names the project and
    # the exit code, never the log path -- that detail is deck/log-only.
    body = f"{_project_word(t)}, exit code {exit_code}"
    return Notification(title="Codex job failed", body=body, tier=TIER_WARNING, key=key,
                        channels=("toast", "ntfy", "telegram"), detail=f"log in {log_path}")


def _decide_done(t: Transition, present: bool) -> Optional[Notification]:
    if present:
        return None
    key = f"{t.session_id}:{t.to_status}"
    summary = t.extra.get("summary") or t.session_name
    body = f"{summary} ({_project_word(t)})"
    return Notification(title="Done", body=body, tier=TIER_SILENT, key=key, channels=("ntfy",))


def _decide_parked(t: Transition, clock: str = "24h") -> Notification:
    key = f"{t.session_id}:parked"
    body = f"{_project_word(t)}, resumes ~{_fmt_clock(t.extra.get('resume_after'), clock)}"
    return Notification(title="Parked", body=body, tier=TIER_SILENT, key=key, channels=("ntfy",))


def _decide_resumed(t: Transition) -> Notification:
    key = f"{t.session_id}:resumed"
    return Notification(title="Resumed", body=_project_word(t), tier=TIER_SILENT, key=key, channels=("ntfy",))


def _decide_resume_failed(t: Transition) -> Notification:
    key = f"{t.session_id}:resume-failed"
    return Notification(title="Resume failed - needs you", body=_project_word(t), tier=TIER_CAUTION, key=key,
                        channels=("toast", "ntfy"))


# --------------------------------------------------------------------------- 3. budget crossings

_BUDGET_WINDOWS = (("five_hour", "5-hour"), ("seven_day", "weekly"))


def _band(pct: Optional[float]) -> Optional[int]:
    """Which qualitative band a percentage sits in, S-UX R3's 70/90 lines: `None` under 70 (no
    band, nothing to notify), `90` from 70 up to 100, `100` at or past it."""
    if pct is None:
        return None
    if pct >= 90:
        return 100 if pct >= 100 else 90
    if pct >= 70:
        return 70
    return None


def budget_events(prev_hud: Optional[dict], hud: Optional[dict], history: dict,
                  clock: str = "24h") -> list[Notification]:
    """HUD band crossings (rules table row 5): once per band per reset period. The reset time is
    folded straight into the dedupe key, the same trick `governor/policy.check_windows` uses for
    its own `acted` cache -- a fresh reset period is a fresh key, so nothing needs to be cleared
    by hand when the window turns over."""
    out: list[Notification] = []
    prev_claude = (prev_hud or {}).get("claude") or {}
    cur_claude = (hud or {}).get("claude") or {}
    for window, window_word in _BUDGET_WINDOWS:
        prev_band = _band(_num(prev_claude.get(f"{window}_pct")))
        cur_band = _band(_num(cur_claude.get(f"{window}_pct")))
        if cur_band is None or cur_band == prev_band:
            continue
        resets_at = cur_claude.get(f"{window}_resets_at") or ""
        key = f"budget:claude:{window}:{cur_band}:{resets_at}"
        if (history or {}).get(key):
            continue
        tier = TIER_WARNING if cur_band >= 100 else TIER_CAUTION
        reset_clock = _fmt_clock(resets_at, clock)
        title = f"Claude {window_word} budget at {cur_band}%"
        body = f"resets {reset_clock}" if reset_clock != "?" else "reset time unknown"
        out.append(Notification(title=title, body=body, tier=tier, key=key, channels=("toast", "ntfy")))
    return out


# --------------------------------------------------------------------------- 4. governor events with no AgentState transition


def park_refused_notifications(refusals: Iterable[dict], history: dict) -> list[Notification]:
    """`refusals`: raw `park_refused` lines out of `state/limits/parked.jsonl` --
    `max_parked` was hit, so a session that should have parked is still live. This never shows
    up as an `AgentState` transition (parking was REFUSED -- nothing about the row changed),
    which is why `decide` cannot see it and it gets its own function."""
    out: list[Notification] = []
    for line in refusals or ():
        sid = (line or {}).get("session_id")
        if not sid:
            continue
        at = line.get("at") or ""
        key = f"park_refused:{sid}:{at}"
        if (history or {}).get(key):
            continue
        # Lock-screen text only: no session id and no "/" ratio (reads like a path fragment) in
        # the pushed body -- the deck names the session, one `j` away.
        body = f"{line.get('parked_count', '?')} of {line.get('max_parked', '?')} already parked, needs you"
        out.append(Notification(title="Parking refused", body=body, tier=TIER_CAUTION, key=key,
                                detail=f"session {sid}",
                                channels=("toast", "ntfy")))
    return out


def handoff_notifications(events: Iterable[Any], history: dict) -> list[Notification]:
    """`events`: raw `Event` objects with `event == "handoff"` -- the dying row
    is left exactly as it was, so a hand-off never shows up as an `AgentState` transition either;
    read straight off the event log instead, same reason as `park_refused_notifications`."""
    out: list[Notification] = []
    for e in events or ():
        if (getattr(e, "event", "") or "").lower() != "handoff":
            continue
        sid = getattr(e, "session_id", None)
        ts = getattr(e, "ts", "") or ""
        key = f"handoff:{sid}:{ts}"
        if (history or {}).get(key):
            continue
        extra = getattr(e, "extra", {}) or {}
        to_provider = extra.get("to_provider", "?")
        project = getattr(e, "project", None) or "?"
        out.append(Notification(title=f"Handed to {to_provider}", body=project, tier=TIER_SILENT, key=key,
                                channels=("ntfy",)))
    return out


# --------------------------------------------------------------------------- quiet hours


def is_quiet_hours(quiet_hours: Optional[list], now: Optional[datetime] = None) -> bool:
    """`quiet_hours` is `["HH:MM", "HH:MM"]` (start, end), local time, wrapping past midnight
    when start > end (the default `["23:00", "08:00"]` does). Malformed or missing config is
    never quiet -- a broken setting must not silently swallow every Warning-tier alert too."""
    if not quiet_hours or len(quiet_hours) != 2:
        return False
    try:
        sh, sm = (int(x) for x in str(quiet_hours[0]).split(":"))
        eh, em = (int(x) for x in str(quiet_hours[1]).split(":"))
    except (ValueError, AttributeError, TypeError):
        return False
    now = now or datetime.now(timezone.utc)
    local = now.astimezone()
    cur, start, end = local.hour * 60 + local.minute, sh * 60 + sm, eh * 60 + em
    if start == end:
        return False
    return (start <= cur < end) if start < end else (cur >= start or cur < end)


def allowed_during_quiet_hours(tier: str, quiet_hours: Optional[list], now: Optional[datetime] = None) -> bool:
    """quiet-hours rule: "only Warning tier goes out; the rest is written to sent.jsonl
    with suppressed: 'quiet'". `runner.py` calls this to decide whether to actually fire a
    `Notification` `decide`/`budget_events` already produced, or just log it as suppressed."""
    return tier == TIER_WARNING or not is_quiet_hours(quiet_hours, now)
