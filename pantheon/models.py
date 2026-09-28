"""Shared data shapes. Every pane, adapter and the governor import from here, never redefine."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional


class AgentStatus(str, Enum):
    """What a row on the supervisor says. Text label is ALWAYS printed."""

    WORKING = "working"
    WAITING_INPUT = "waiting-input"            # Stop: turn finished, wants the user   (Caution)
    BLOCKED_PERMISSION = "blocked-permission"  # Notification/permission_prompt      (Caution, `!!`)
    IDLE = "idle"                              # Notification/idle_prompt (60s+ at prompt)
    QUIET = "quiet"                            # no fresh sign of life past the staleness threshold
                                                # (`supervisor/state.py` QUIET_AFTER_SECONDS): a row
                                                # that LOOKED like it was working or needed the user
                                                # but has said nothing since -- neither any more
    GONE = "gone"                              # SessionEnd
    UNKNOWN = "unknown"                        # live pane, no event yet
    # Codex headless jobs
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"                          # Warning tier
    # Governor
    WINDING_DOWN = "winding-down"
    PARKED = "parked"
    RESUME_FAILED = "resume-failed"            # resume ladder exhausted  (Caution)

    @property
    def needs_human(self) -> bool:
        return self in (AgentStatus.WAITING_INPUT, AgentStatus.BLOCKED_PERMISSION, AgentStatus.RESUME_FAILED)

    @property
    def label(self) -> str:
        """Plain-words label for the screen (never a code)."""
        return {
            AgentStatus.WAITING_INPUT: "waiting - needs input",
            AgentStatus.BLOCKED_PERMISSION: "blocked - permission",
            AgentStatus.WINDING_DOWN: "winding down",
            AgentStatus.QUIET: "quiet - no activity",
            # Short enough for the 23-wide state column behind its `!!` marker;
            # the amber, the marker and the top sort carry "needs you".
            AgentStatus.RESUME_FAILED: "resume failed",
        }.get(self, self.value)


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _clock_mode(cfg_or_mode: Any) -> str:
    """Pull `"24h"`/`"12h"` out of whatever a caller already has at hand -- a `Config` (reads
    `appearance_settings().clock`), an `Appearance` (reads `.clock` directly), or a bare mode
    string. Never raises: a bad object just gets the default."""
    if isinstance(cfg_or_mode, str):
        return cfg_or_mode
    settings_fn = getattr(cfg_or_mode, "appearance_settings", None)
    if callable(settings_fn):
        try:
            return settings_fn().clock
        except Exception:
            return "24h"
    return getattr(cfg_or_mode, "clock", None) or "24h"


def format_clock(when: Optional[datetime], cfg_or_mode: Any = "24h") -> str:
    """The one wall-clock formatter every pane uses. `[appearance] clock` picks `"19:11"` (`"24h"`, the
    default) or `"7:11 pm"` (`"12h"`); `?` when `when` is None -- Pantheon never guesses a time
   ."""
    if when is None:
        return "?"
    try:
        local = when.astimezone()
    except (ValueError, OSError):
        return "?"
    if _clock_mode(cfg_or_mode) == "12h":
        hour = local.strftime("%I").lstrip("0") or "12"
        return f"{hour}:{local.strftime('%M')} {local.strftime('%p').lower()}"
    return local.strftime("%H:%M")


def parse_ts(ts: str | None) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass
class Event:
    """One line of `state/agents/events.jsonl`.

    `source`: "claude" (hook-written), "codex", "pantheon" (the deck itself).
    `event`: SessionStart | PostToolUse | Stop | Notification | SessionEnd  (claude)
             queued | running | done | failed                               (codex)
             kill | dispatch | wind_down | park | resume                    (pantheon)
    """

    ts: str
    event: str
    source: str = "claude"
    session_id: Optional[str] = None
    cwd: Optional[str] = None
    tool_name: Optional[str] = None
    notification_type: Optional[str] = None
    message: Optional[str] = None
    detail: Optional[str] = None       # SessionStart.source / SessionEnd.reason / exit code text
    agent_id: Optional[str] = None     # set when a subagent (not the main session) fired the hook
    tmux_pane: Optional[str] = None    # `$TMUX_PANE` inherited by the hook, e.g. "%3"
    project: Optional[str] = None
    job_id: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Event":
        known = set(cls.__dataclass_fields__) - {"extra"}
        kwargs = {k: d[k] for k in known if k in d}
        extra = {k: v for k, v in d.items() if k not in known}
        kwargs.setdefault("ts", utcnow_iso())
        kwargs.setdefault("event", "unknown")
        return cls(extra=extra, **kwargs)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        extra = d.pop("extra") or {}
        d.update(extra)
        return {k: v for k, v in d.items() if v is not None}

    @property
    def when(self) -> Optional[datetime]:
        return parse_ts(self.ts)


@dataclass
class TmuxWindow:
    index: int
    name: str
    command: str       # pane_current_command, e.g. "node", "claude", "codex", "bash"
    path: str          # pane_current_path (forward slashes, as tmux reports)
    pane_id: str = ""  # "%3"
    session: str = ""


@dataclass
class AgentState:
    """One supervisor row."""

    session_id: str
    provider: str                      # "claude" | "codex" | "ollama" | ...
    status: AgentStatus = AgentStatus.UNKNOWN
    project: Optional[str] = None
    cwd: Optional[str] = None
    last_action: str = ""
    last_event_ts: Optional[str] = None
    window_index: Optional[int] = None
    window_name: Optional[str] = None
    tmux_pane: Optional[str] = None
    tmux_session: Optional[str] = None # name of the tmux session the window lives in, if any
    in_pantheon: bool = False          # matched to a window in the pantheon tmux session
    job_id: Optional[str] = None
    tracker_id: Optional[str] = None   # set when the deck dispatched this agent from a queue row
    parse_errors: int = 0
    mode: Optional[str] = None         # last `permission_mode` the hook recorded: "auto" |
                                        # "default" | "acceptEdits" | "plan"; None = never reported

    @property
    def needs_human(self) -> bool:
        return self.status.needs_human

    @property
    def where(self) -> str:
        """Where this agent lives, in words a stranger can act on: `pantheon:3` (window 3 of the
        deck's tmux session), `main:0` (another tmux session), `headless` (a Codex job with no
        window), or `desktop` (not in any tmux -- the Claude Desktop app or a plain terminal)."""
        if self.window_index is not None:
            return f"{self.tmux_session or '?'}:{self.window_index}"
        if self.provider == "codex" and self.job_id:
            return "headless"
        return "desktop"

    def age_seconds(self, now: Optional[datetime] = None) -> Optional[float]:
        t = parse_ts(self.last_event_ts)
        if t is None:
            return None
        now = now or datetime.now(timezone.utc)
        return max(0.0, (now - t).total_seconds())


def format_age(seconds: Optional[float]) -> str:
    """`<1m`, `4m`, `1h12m`."""
    if seconds is None:
        return "?"
    m = int(seconds // 60)
    if m < 1:
        return "<1m"
    if m < 60:
        return f"{m}m"
    return f"{m // 60}h{m % 60:02d}m"


@dataclass
class TaskRow:
    """One task, whatever system it came from. Field names follow the vault Sprints schema because that is the first source; other sources map onto these.
    Booleans are real booleans (sources normalize "true"/"false" strings). Missing = None."""

    id: str                                # stable id: file stem for Obsidian, row id for standalone
    summary: str = ""
    project: Optional[str] = None
    tier: Optional[str] = None             # now | soon | someday
    status: Optional[str] = None           # open | in-progress | blocked | verify | done
    complexity: Optional[str] = None       # quick | moderate | heavy
    est_context: Optional[str] = None      # small | medium | large (plate rows only)
    agent: Optional[bool] = None           # on the Agent's plate; the vault field was `fable` until 2026-09-23
    done: Optional[bool] = None
    next: Optional[bool] = None
    picked: Optional[str] = None           # ISO date
    due: Optional[str] = None
    created: Optional[str] = None
    source: Optional[str] = None           # "[[Note title]]" or a URL
    note: Optional[str] = None             # The user -> Claude
    reply: Optional[str] = None            # Claude -> the user
    project_assignment_log: list[str] = field(default_factory=list)
    body: str = ""                         # text under the frontmatter
    path: Optional[str] = None             # where it lives (file path), for open/edit
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def fable(self) -> Optional[bool]:
        """Old name for `agent`. Kept read-only so a Base filter or sort still naming
        `fable` evaluates against the same value."""
        return self.agent


@dataclass
class ProviderUsage:
    """Budget numbers for one provider. `None` means unknown; never fabricated."""

    provider: str
    five_hour_pct: Optional[float] = None
    seven_day_pct: Optional[float] = None
    five_hour_resets_at: Optional[str] = None   # ISO-8601 UTC
    seven_day_resets_at: Optional[str] = None
    burn_pct_per_hour: Optional[float] = None
    burn_cost_per_hour: Optional[float] = None
    cost_today_usd: Optional[float] = None
    tokens_today: Optional[int] = None
    context_pct: Optional[float] = None         # focused session only
    source: str = "unknown"                      # "statusline" | "ccusage" | "codex-log" | "unknown"
    fetched_at: Optional[str] = None             # when Pantheon read it
    reported_at: Optional[str] = None            # when the source itself wrote the numbers (the
                                                 # Codex log line's time); None for a live feed
    note: str = ""                               # e.g. "from history", "ollama not running"


@dataclass
class LaunchResult:
    ok: bool
    where: str = "tmux"            # "tmux" | "pc-window" | "headless"
    window_index: Optional[int] = None
    window_name: Optional[str] = None
    job_id: Optional[str] = None
    tmux_pane: Optional[str] = None  # "%7": the pane the agent was started in, for matching later
    message: str = ""              # a sentence for the footer, never a traceback
