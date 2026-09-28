"""The one-line HUD for the tmux status bar.

tmux runs this every 60 seconds, so it reads `state/hud.json` and nothing else -- no npx, no
log walking, no network. The background refresher is what keeps that file current, which is
also why the status bar and the HUD pane can never disagree.

  C 5h 42% - wk 61% - X today $0.00 - 12:04     (the separator is a middle dot in Unicode mode)
  C 5h 42% 14m ago - wk 61% - ...               (no Claude session has reported for 14 minutes)
  usage not updated for 12m                     (the saved file is more than 5 minutes old)

Unknown numbers are left out rather than printed as `?`. Before it reads,
`main` runs `sources.refresh_if_stale`, so while the budget window is closed the status bar
itself keeps `state/hud.json` current with a file-only quick pass.
"""
from __future__ import annotations

import json
import sys

from .. import config, glyphs
from ..models import format_clock, parse_ts
from . import calc

MAX_CHARS = 50
STALE_AFTER_SECONDS = 300


def _pct(value) -> str:
    try:
        return f"{float(value):.0f}%"
    except (TypeError, ValueError):
        return "?"


def _money(value) -> str:
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return "$?"


def _clock(iso, clock: str = "24h") -> str:
    return format_clock(parse_ts(iso), clock)


def is_stale(picture: dict, max_age_s: int = STALE_AFTER_SECONDS, now=None) -> bool:
    when = parse_ts((picture or {}).get("fetched_at"))
    if when is None:
        return True
    from datetime import datetime, timezone

    now = now or datetime.now(timezone.utc)
    return (now - when).total_seconds() > max_age_s


def render(picture: dict, mode: str = "unicode", now=None, clock: str = "24h") -> str:
    """The status-bar line, never longer than MAX_CHARS. `C` is Claude, `X` is Codex."""
    dot = f" {glyphs.table(mode)['dot']} "
    if not picture:
        return "usage: no reading yet"
    if is_stale(picture, now=now):
        age = calc.reading_age_seconds(picture.get("fetched_at"), now)
        minutes = "" if age is None else f" for {int(age // 60)}m"
        return f"usage not updated{minutes}"[:MAX_CHARS]
    claude = picture.get("claude") or {}
    codex = picture.get("codex") or {}
    five, week = claude.get("five_hour_pct"), claude.get("seven_day_pct")
    age = calc.stale_age_words(calc.usage_as_of(claude), now)
    tail = f" {age}" if age else ""
    parts = []
    if five is not None:
        parts.append(f"C 5h {_pct(five)}{tail}")
        if week is not None:
            parts.append(f"wk {_pct(week)}")
    elif week is not None:
        parts.append(f"C wk {_pct(week)}{tail}")
    else:
        parts.append("C no reading")
    if codex.get("cost_today_usd") is not None:
        parts.append(f"X today {_money(codex.get('cost_today_usd'))}")
    parts.append(_clock(picture.get("fetched_at"), clock))
    line = dot.join(parts)
    while len(line) > MAX_CHARS and len(parts) > 1:
        parts.pop()
        line = dot.join(parts)
    return line[:MAX_CHARS]


def main() -> int:
    try:
        cfg = config.load()
        try:
            from . import sources
            sources.refresh_if_stale(cfg)   # never raises; a no-op while the collector runs
        except Exception:
            pass
        with open(cfg.hud_file, "r", encoding="utf-8", errors="replace") as fh:
            picture = json.load(fh)
        appearance = cfg.appearance_settings()
        print(render(picture if isinstance(picture, dict) else {}, appearance.glyphs, clock=appearance.clock))
    except Exception:
        # tmux shows whatever this prints; a traceback in the status bar helps nobody.
        print("usage: no reading yet")
    return 0


if __name__ == "__main__":
    sys.exit(main())
