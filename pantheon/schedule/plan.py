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
import math
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


def _reset_ts(value: Any) -> Optional[datetime]:
    """A reset time from the usage file, or `None` when it is not ISO text with a timezone: a
    number or a naive timestamp cannot be placed against the local clock, so it is treated as
    missing rather than guessed (and never compared with an aware time, which raises)."""
    when = parse_ts(value) if isinstance(value, str) else None
    return when if when is not None and when.tzinfo is not None else None


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
        resets = _reset_ts(picture.get(f"{window}_resets_at"))
        threshold = thresholds.get(window)
        if pct is None or resets is None or threshold is None:
            continue
        try:
            pct = float(pct)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(pct) or pct < float(threshold) or resets <= planned:
            continue
        sentences.append(
            f"{at} lands inside the {label} window, already {pct:.0f}% used "
            f"(resets {_clock(resets, planned)}); it may wind down fast."
        )
    return " ".join(sentences) or None


# ------------------------------------------------------------------------------ "at the reset"

RESET_MARGIN_MINUTES = 2          # fire a couple of minutes AFTER the reset
RESET_WORDS = {"reset": ("five_hour", "five-hour"), "weekly-reset": ("seven_day", "weekly")}


def is_reset_word(text: Optional[str]) -> bool:
    return (text or "").strip().lower() in RESET_WORDS


def _no_reset(why: str):
    raise ValueError(f"Cannot work out the reset time: {why}. Nothing was scheduled; "
                     f"give a clock time instead, e.g. --at 06:00.")


def resolve_reset(text: str, cfg, provider: str = "claude", now: Optional[datetime] = None,
                  hud: Any = "read") -> datetime:
    """`--at reset` / `--at weekly-reset` -> the next usage-window reset in `state/hud.json`
    plus `RESET_MARGIN_MINUTES`, on the local clock. Raises `ValueError` with a sentence the user
    can act on when the file is missing, older than `FRESH_SECONDS`, lacks the reset, or its reset
    has already passed (the number belongs to a window that has since moved on); it never
    guesses a time."""
    window, label = RESET_WORDS[text.strip().lower()]
    now = now or datetime.now().astimezone()
    if hud == "read":
        hud = read_hud(cfg)
    if not isinstance(hud, dict):
        _no_reset(f"there is no usage file ({cfg.hud_file}); the deck writes it while it is open")
    picture = hud.get(provider)
    if not isinstance(picture, dict):
        _no_reset(f"the usage file has nothing for {provider}")
    fetched_at = picture.get("fetched_at") or hud.get("fetched_at")
    if hud_is_stale(fetched_at, now=now, max_age_seconds=FRESH_SECONDS):
        _no_reset("the usage file is older than 10 minutes, so its reset time may be out of date; "
                  "open the deck and try again")
    resets = _reset_ts(picture.get(f"{window}_resets_at"))
    if resets is None:
        _no_reset(f"the usage file has no {label} reset time for {provider}")
    if resets <= now:
        _no_reset(f"the {label} reset in the usage file ({resets.astimezone().strftime('%H:%M')}) "
                  f"has already passed and the file has not caught up")
    planned = resets.astimezone() + timedelta(minutes=RESET_MARGIN_MINUTES)
    if planned.second or planned.microsecond:       # Task Scheduler works in whole minutes; never early
        planned = planned.replace(second=0, microsecond=0) + timedelta(minutes=1)
    return planned
