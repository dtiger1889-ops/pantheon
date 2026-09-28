"""Pure arithmetic for the usage HUD. No file reads, no subprocesses, no Textual -- so every
number on the screen can be unit-tested.

Vocabulary used on screen, in plain words rather than jargon:
  "budget"   = how much of a provider's 5-hour or weekly allowance is spent
  "burn"     = how fast the budget is being spent, in percent per hour
  "runs out" = when the budget would reach 100% if the burn kept up
  "memory"   = the session's context window; "compaction soon" = it is about to summarise itself
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, Sequence

from ..models import format_clock, parse_ts

# How far back a burn-rate reading looks. Spec the last 30 minutes, scaled to per hour.
BURN_WINDOW_SECONDS = 30 * 60

# Attention thresholds. Separate from the 70/90 colour bands in theme.py:
# colour warns earlier, the written line only appears when the user would actually act.
WEEKLY_ATTENTION_PCT = 85.0
FIVE_HOUR_ATTENTION_PCT = 90.0
CONTEXT_ATTENTION_PCT = 85.0

# Context-window bands. Names are the words printed next to the bar.
CONTEXT_BANDS = ((60.0, "ok"), (85.0, "getting full"))


def _as_epoch(ts) -> Optional[float]:
    """Accept an ISO-8601 string, a Unix epoch number, or a datetime. Anything else -> None."""
    if ts is None:
        return None
    if isinstance(ts, datetime):
        t = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
        return t.timestamp()
    if isinstance(ts, bool):
        return None
    if isinstance(ts, (int, float)):
        return float(ts)
    parsed = parse_ts(str(ts))
    return parsed.timestamp() if parsed else None


def burn_pct_per_hour(
    samples: Sequence[tuple], window_seconds: int = BURN_WINDOW_SECONDS,
    now: Optional[float] = None,
) -> Optional[float]:
    """Percent of budget spent per hour, measured over the last `window_seconds` of samples.

    `samples` is a list of (timestamp, percent) pairs; timestamps may be ISO strings or epoch
    seconds and need not be sorted. Returns None when there is not enough to say -- fewer than
    two usable readings, no time between them, or only readings from before a window reset.
    A reset (the percent dropping) ends the measurable run, so only the rising tail is used.
    `now` (epoch seconds) makes a newest reading older than the window count as no reading at
    all: with no live session the last samples are hours old, and `burn 9%/h` from the morning
    sat beside `5h ?` as if it were current.
    """
    clean: list[tuple[float, float]] = []
    for item in samples or ():
        if not item or len(item) < 2:
            continue
        t, pct = _as_epoch(item[0]), item[1]
        if t is None or pct is None:
            continue
        try:
            clean.append((t, float(pct)))
        except (TypeError, ValueError):
            continue
    if len(clean) < 2:
        return None
    clean.sort(key=lambda p: p[0])

    # Walk back from the newest reading; stop where the percent last fell (a window reset).
    tail = [clean[-1]]
    for t, pct in reversed(clean[:-1]):
        if pct > tail[0][1]:
            break
        tail.insert(0, (t, pct))
    newest = tail[-1][0]
    if now is not None and now - newest > window_seconds:
        return None
    tail = [(t, p) for t, p in tail if newest - t <= window_seconds]
    if len(tail) < 2:
        return None

    elapsed = tail[-1][0] - tail[0][0]
    if elapsed <= 0:
        return None
    delta = tail[-1][1] - tail[0][1]
    if delta < 0:
        return None
    return delta / (elapsed / 3600.0)


def time_to_limit(
    pct: Optional[float], burn: Optional[float], resets_at=None, now=None
) -> Optional[float]:
    """Seconds until the budget would reach 100% at the current burn. None when unknowable.

    `resets_at` is accepted so callers can pass the whole picture in one call; it does not
    change the answer -- the formatter decides whether the window resets first.
    """
    if pct is None or burn is None:
        return None
    try:
        pct, burn = float(pct), float(burn)
    except (TypeError, ValueError):
        return None
    if burn <= 0:
        return None
    remaining = 100.0 - pct
    if remaining <= 0:
        return 0.0
    return remaining / burn * 3600.0


def seconds_until(resets_at, now=None) -> Optional[float]:
    """Seconds from `now` until `resets_at`. Already past (or unreadable) -> None."""
    target = _as_epoch(resets_at)
    if target is None:
        return None
    ref = _as_epoch(now) if now is not None else datetime.now(timezone.utc).timestamp()
    if ref is None:
        return None
    delta = target - ref
    return delta if delta > 0 else None


def format_duration(seconds: Optional[float]) -> str:
    """`~3h10m`, `~14m`, `?`. Never a bare number, never a date."""
    if seconds is None:
        return "?"
    total = max(0, int(seconds))
    hours, minutes = total // 3600, (total % 3600) // 60
    if hours:
        return f"~{hours}h{minutes:02d}m"
    return f"~{minutes}m"


def format_time_to_limit(seconds: Optional[float], resets_at=None, now=None) -> str:
    """`~3h10m` when the budget runs out first, `>window` when the window resets first,
    `?` when unknown. The pane spells `>window` out in words next to it."""
    if seconds is None:
        return "?"
    until_reset = seconds_until(resets_at, now)
    if until_reset is not None and seconds > until_reset:
        return ">window"
    return format_duration(seconds)


# A reading older than this is shown with its age beside it (`61% 14m ago`); the same ten
# minutes `sources.STATUSLINE_MAX_AGE_SECONDS` uses for "this session is live".
STALE_READING_SECONDS = 600


def age_words(seconds: Optional[float]) -> Optional[str]:
    """`14m ago`, `3h ago`, `2d ago` -- one unit, rounded down, for a reading's age. Under a
    minute is `just now`; unknown is None (the caller says nothing rather than `?`)."""
    if seconds is None:
        return None
    total = max(0, int(seconds))
    if total < 60:
        return "just now"
    if total < 3600:
        return f"{total // 60}m ago"
    if total < 2 * 86400:
        return f"{total // 3600}h ago"
    return f"{total // 86400}d ago"


def reading_age_seconds(as_of, now=None) -> Optional[float]:
    """Seconds since `as_of` (ISO string, epoch, or datetime). None when unknown."""
    stamp = _as_epoch(as_of)
    if stamp is None:
        return None
    ref = _as_epoch(now) if now is not None else datetime.now(timezone.utc).timestamp()
    if ref is None:
        return None
    return max(0.0, ref - stamp)


def stale_age_words(as_of, now=None, fresh_seconds: int = STALE_READING_SECONDS) -> Optional[str]:
    """The age words for a reading only once it is old enough to matter (`14m ago`), else None:
    a live number needs no label, an old one must never pass for live."""
    age = reading_age_seconds(as_of, now)
    if age is None or age <= fresh_seconds:
        return None
    return age_words(age)


def usage_as_of(usage) -> Optional[str]:
    """When a provider's numbers were last reported: the 5-hour reading's time, else the weekly
    one's, else the source's own `reported_at` (Codex's log line). None for an older picture
    that carries none of them -- treated as live, the way it always was."""
    if not usage:
        return None
    get = usage.get if isinstance(usage, dict) else (lambda k: getattr(usage, k, None))
    for key in ("five_hour_as_of", "seven_day_as_of", "reported_at"):
        value = get(key)
        if value:
            return value
    return None


def context_band(pct: Optional[float]) -> str:
    """`ok` / `getting full` / `compaction soon`.
    `unknown` when there is no reading -- never a guess."""
    if pct is None:
        return "unknown"
    try:
        value = max(0.0, float(pct))
    except (TypeError, ValueError):
        return "unknown"
    for edge, name in CONTEXT_BANDS:
        if value < edge:
            return name
    return "compaction soon"


def _pct(usage, field: str) -> Optional[float]:
    """Read a percent off a ProviderUsage or a plain dict with the same field names."""
    if usage is None:
        return None
    value = usage.get(field) if isinstance(usage, dict) else getattr(usage, field, None)
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _name(usage, default: str) -> str:
    if usage is None:
        return default
    raw = usage.get("provider") if isinstance(usage, dict) else getattr(usage, "provider", None)
    raw = raw or default
    return {"claude": "Claude", "codex": "Codex"}.get(raw, raw.title())


def _fmt_clock_local(iso: Optional[str], clock: str = "24h") -> str:
    """ISO-8601 UTC -> local wall clock, in the format `[appearance] clock` asks for (same
    convention as `hud/app.py`'s `_fmt_clock`, duplicated here in the few lines it takes rather
    than imported -- `hud/app.py` already imports this module, so the other way round would be
    a cycle). `clock` has no `cfg` to read here (this module stays pure, no file reads); callers
    that have one pass `cfg.appearance_settings().clock`."""
    return format_clock(parse_ts(iso), clock)


def governor_line(summary: Optional[dict], clock: str = "24h") -> Optional[str]:
    """The HUD's governor attention line: `limit guard: 2 winding down ·
    1 parked · 5-hour window resets 01:12`, or `[dry run] limit guard: would wind down 2 ·
    5-hour window resets 01:12` when `dry_run` is on. `summary` is already-gathered counts
    (`governor/parked.py summary`)
    -- this module stays pure (no file reads) and only formats the words. None, or nothing to
    report, -> None so the caller draws no line."""
    if not summary:
        return None
    winding = int(summary.get("winding_down") or 0)
    parked = int(summary.get("parked") or 0)
    if not winding and not parked:
        return None
    reset = _fmt_clock_local(summary.get("five_hour_resets_at"), clock)
    if summary.get("dry_run"):
        return f"[dry run] limit guard: would wind down {winding} · 5-hour window resets {reset}"
    return f"limit guard: {winding} winding down · {parked} parked · 5-hour window resets {reset}"


def attention_lines(claude=None, codex=None, context_pct: Optional[float] = None,
                    governor: Optional[dict] = None, clock: str = "24h") -> list[str]:
    """The third line of a block: what needs the user now, in sentences a non-coder reads once.

    Empty list = nothing to say, and the pane draws no third line.
    """
    lines: list[str] = []
    for usage in (claude, codex):
        if usage is None:
            continue
        who = _name(usage, "provider")
        weekly = _pct(usage, "seven_day_pct")
        if weekly is not None and weekly > WEEKLY_ATTENTION_PCT:
            lines.append(f"{who}: {weekly:.0f}% of this week's budget is used")
        five = _pct(usage, "five_hour_pct")
        if five is not None and five > FIVE_HOUR_ATTENTION_PCT:
            lines.append(f"{who}: {five:.0f}% of this 5-hour budget is used")
    if context_pct is not None:
        try:
            ctx = float(context_pct)
        except (TypeError, ValueError):
            ctx = None
        if ctx is not None and ctx > CONTEXT_ATTENTION_PCT:
            lines.append(
                f"This session's memory is {ctx:.0f}% full - it will summarise itself soon"
            )
    gov_line = governor_line(governor, clock)
    if gov_line:
        lines.append(gov_line)
    return lines
