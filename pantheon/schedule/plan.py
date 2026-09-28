"""Pure planning for a timed start: no files written, no subprocesses, no clock of its own
(every function takes `now`), so all of it is unit-testable.

`warn_for` is the part nothing else found online does: before a start is
scheduled, check whether its time lands inside a usage window that is already at or past the
governor's own wind-down line, and say so in one plain sentence. It never invents a number -- no
fresh `state/hud.json`, no percent, or a window that resets before the start all mean no warning
.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from ..governor.policy import hud_is_stale
from ..models import parse_ts

# The usage file is rewritten about once a minute while the budget window is open; older than
# this and its percent may belong to a window that has since moved on.
FRESH_SECONDS = 600

_WINDOWS = (
    ("five_hour", "five-hour"),
    ("seven_day", "weekly"),
)
_AT_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*$")


def parse_at(text: str, now: Optional[datetime] = None) -> datetime:
    """`"HH:MM"` (24-hour, local time) -> the next moment the clock says that: today if it is
    still ahead, else tomorrow. Raises `ValueError` with a sentence the user can act on."""
    match = _AT_RE.match(text or "")
    if not match:
        raise ValueError(f"'{text}' is not a time; write it as HH:MM, 24-hour, e.g. 06:00 or 23:30")
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        raise ValueError(f"'{text}' is not a time of day; hours go 00-23 and minutes 00-59")
    now = now or datetime.now().astimezone()
    planned = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if planned <= now:
        planned += timedelta(days=1)
    return planned


def read_hud(cfg) -> Optional[dict]:
    """`state/hud.json` as a dict, or `None` when it is missing or unreadable."""
    try:
        return json.loads(Path(cfg.hud_file).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _clock(when: datetime, like: datetime) -> str:
    """`when` on the same clock as `like` (the planned time is local), as `HH:MM`, with the day
    in front when it is not the same day (`Sat 17:11`, the weekly window)."""
    if like.tzinfo is not None and when.tzinfo is not None:
        when = when.astimezone(like.tzinfo)
    if when.date() != like.date():
        return when.strftime("%a %H:%M")
    return when.strftime("%H:%M")


def warn_for(planned: datetime, cfg, provider: str = "claude", now: Optional[datetime] = None,
             hud: Any = "read") -> Optional[str]:
    """One sentence when `planned` lands inside a usage window already at or past the
    governor's `wind_down_at_percent` for it, else `None`. `hud` defaults to reading
    `state/hud.json`; pass a dict (or `None`) to test without a file."""
    if hud == "read":
        hud = read_hud(cfg)
    if not isinstance(hud, dict):
        return None
    picture = hud.get(provider)
    if not isinstance(picture, dict):
        return None
    fetched_at = picture.get("fetched_at") or hud.get("fetched_at")
    if hud_is_stale(fetched_at, now=now, max_age_seconds=FRESH_SECONDS):
        return None
    thresholds = cfg.governor_settings().wind_down_at_percent
    at = planned.strftime("%H:%M")
    sentences: list[str] = []
    for window, label in _WINDOWS:
        pct = picture.get(f"{window}_pct")
        resets = parse_ts(picture.get(f"{window}_resets_at"))
        threshold = thresholds.get(window)
        if pct is None or resets is None or threshold is None:
            continue
        try:
            pct = float(pct)
        except (TypeError, ValueError):
            continue
        if pct < float(threshold) or resets <= planned:
            continue
        sentences.append(
            f"{at} lands inside the {label} window, already {pct:.0f}% used "
            f"(resets {_clock(resets, planned)}); it may wind down fast."
        )
    return " ".join(sentences) or None
