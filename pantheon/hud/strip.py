"""The budget strip for the combined desk layout: a row of
framed provider cards plus a NEEDS YOU card, sized to sit across the top of the wide
supervisor+queue+HUD screen.

It reads `state/hud.json` and nothing else -- no refresher thread, no ccusage, no subprocess.
The HUD pane (a separate tmux window, `pantheon/hud/app.py`) is what keeps that file current;
this widget only ever reads it, on a timer, so the combined layout never blocks on Node
.

`render_strip`/`height_hint` are the pre-phone-oriented text form: the strip itself no
longer draws through them at desk width (it composes `ProviderCard`/`NeedsYouCard` from
`hud/app.py` instead), but they stay here, unchanged, because the phone-width HUD window and
their own tests still call them directly.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from textual import events
from textual.containers import Horizontal
from textual.widgets import Static

from .. import glyphs, theme
from ..models import parse_ts
from . import calc, sources
from .app import (
    NeedsYouCard,
    PHONE_WIDTH_BREAKPOINT,
    ProviderCard,
    _claude_model_effort,
    _paint,
    is_stale,
    render_block,
    render_block_narrow,
)

REFRESH_SECONDS = 30
DEFAULT_WIDTH = 80
NEEDS_YOU_WIDTH_BREAKPOINT = 150  # below this, the strip draws the two provider cards only


def _minutes_old(fetched_at: Optional[str], now=None) -> Optional[int]:
    when = parse_ts(fetched_at)
    if when is None:
        return None
    now = now or datetime.now(timezone.utc)
    return max(0, int((now - when).total_seconds() // 60))


def _muted(text: str) -> str:
    return f"[{theme.TOKENS['muted']}]{text}[/]"


def _read_picture(hud_file) -> dict:
    try:
        with open(hud_file, "r", encoding="utf-8", errors="replace") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError):
        return {}


def _governor_summary(cfg) -> Optional[dict]:
    """governor line, read from `state/limits/` and the toml -- wrapped in try/except so a
    governor package problem never blanks the strip (fail-open, same style as the rest of this
    file)."""
    try:
        from ..governor import parked as governor_parked

        return governor_parked.summary(cfg)
    except Exception:
        return None


BUSY_FOR_ATTENTION = {"working", "running", "winding_down", "queued"}


def attention_for_active(lines, rows) -> list:
    """Keep a provider budget line (`Claude: 87% of ...`, `Codex: ...`) only while an agent of that
    provider is actually running.
    Lines that name no provider (memory, the governor) pass through."""
    active = set()
    for r in rows or []:
        status = getattr(r, "status", None)
        name = getattr(status, "value", None) or getattr(status, "name", "") or ""
        if str(name).lower() in BUSY_FOR_ATTENTION:
            active.add((getattr(r, "provider", "") or "").lower())
    out = []
    for line in lines:
        head = line.split(":", 1)[0].strip().lower() if ":" in line else ""
        if head in ("claude", "codex") and head not in active:
            continue
        out.append(line)
    return out


def _providers(picture: dict) -> list[tuple[str, dict]]:
    out = []
    for key in ("claude", "codex"):
        usage = picture.get(key)
        if isinstance(usage, dict) and usage:
            out.append((key, usage))
    return out


def render_strip(picture: dict, cfg, width: int = DEFAULT_WIDTH) -> list[str]:
    """Every line of the strip, in order, for a `state/hud.json` picture. No title line and no
    `read at` line -- this widget lives inside someone else's frame."""
    picture = picture or {}
    g = glyphs.table(cfg.appearance_settings().glyphs)
    series = picture.get("history") or {}
    narrow = width < PHONE_WIDTH_BREAKPOINT
    providers = _providers(picture)

    lines: list[str] = []
    for key, usage in providers:
        if narrow:
            lines.append(render_block_narrow(usage, cfg, width))
        else:
            lines.extend(render_block(usage, cfg, width, series.get(f"{key}_pct") or None))

    if providers:
        attention = calc.attention_lines(
            picture.get("claude"), picture.get("codex"),
            (picture.get("claude") or {}).get("context_pct"),
            governor=_governor_summary(cfg),
            clock=cfg.appearance_settings().clock,
        )
        for text in attention:
            lines.append(_paint(f"{g['attention']} {text}"[: width - 1], "caution"))

    if not picture:
        lines.append(_muted("no usage file yet; open the usage window (F3) once"))
    elif is_stale(picture):
        minutes = _minutes_old(picture.get("fetched_at"))
        age = "?" if minutes is None else str(minutes)
        lines.append(_muted(
            f"usage numbers are {age} minutes old; the usage window (F3) refreshes them"
        ))
    return lines


def height_hint(picture: dict, width: int = DEFAULT_WIDTH) -> int:
    """How many rows `render_strip` will draw, without needing a `cfg` -- callers sizing layout
    space ahead of time don't have to build the glyph/colour tables just to count lines."""
    picture = picture or {}
    narrow = width < PHONE_WIDTH_BREAKPOINT
    providers = _providers(picture)

    count = 0
    for _key, usage in providers:
        if narrow:
            count += 1
        else:
            count += 2
            if usage.get("context_pct") is not None:
                count += 1
            if usage.get("batch"):
                count += 1
            if usage.get("source") != "statusline":
                count += 1

    if providers:
        count += len(calc.attention_lines(
            picture.get("claude"), picture.get("codex"),
            (picture.get("claude") or {}).get("context_pct"),
        ))

    if not picture or is_stale(picture):
        count += 1
    return count


class HudStrip(Horizontal):
    """The strip embedded in the combined wide layout: one framed `ProviderCard` per provider in
    `state/hud.json`, plus a `NeedsYouCard` when there is room for it. Mounts, builds from
    whatever is on disk, and rebuilds every 30 seconds -- never anything faster or heavier than
    a file read.

    Card sizing is never computed here at compose time -- each card reads its
    own `self.size.width` in its own `on_resize`, which Textual only fires once real layout has
    run.
    """

    DEFAULT_CSS = """
    HudStrip {
        height: auto;
    }
    """

    def __init__(self, cfg, id: str = "hud-strip", agents_source=None, attention_source=None) -> None:
        super().__init__(id=id)
        self.cfg = cfg
        self.agents_source = agents_source
        # Extra NEEDS YOU lines the deck knows about and the usage file does not.
        self.attention_source = attention_source
        self.picture: dict = {}

    def compose(self):
        try:
            yield from self._cards()
        except Exception:
            # A bad file or a mid-write read must not blank the strip or crash the layout.
            yield Static(_muted("could not read the saved usage file"), id="hud-strip-error")

    def _extra_attention(self) -> list:
        if self.attention_source is None:
            return []
        try:
            return [str(x) for x in (self.attention_source() or [])]
        except Exception:
            return []

    def _agents(self) -> list:
        if self.agents_source is None:
            return []
        try:
            return list(self.agents_source() or [])
        except Exception:
            return []

    def _cards(self):
        # The deck is the reader that is always running, so it is the one that keeps the file
        # alive while the budget window (and the collector in it) is closed: a quick, file-only
        # pass when the file is past `sources.UNOWNED_AFTER_SECONDS`. No subprocess, no thread.
        sources.refresh_if_stale(self.cfg)
        self.picture = _read_picture(self.cfg.hud_file)
        picture = self.picture
        providers = _providers(picture)
        series = picture.get("history") or {}
        model_effort = _claude_model_effort(self.cfg)
        glyph_mode = self.cfg.appearance_settings().glyphs

        for key, usage in providers:
            yield ProviderCard(
                key, usage, self.cfg,
                history=series.get(f"{key}_pct") or [],
                model_effort=model_effort if key == "claude" else None,
                id=f"hud-strip-card-{key}",
            )

        attention = calc.attention_lines(
            picture.get("claude"), picture.get("codex"),
            (picture.get("claude") or {}).get("context_pct"),
            governor=_governor_summary(self.cfg),
            clock=self.cfg.appearance_settings().clock,
        )
        agents = self._agents()
        attention = attention_for_active(attention, agents) + self._extra_attention()
        yield NeedsYouCard(
            agents, attention, glyph_mode=glyph_mode, id="hud-strip-needs-you",
        )

        if not providers:
            yield Static(
                _muted("no usage file yet; open the usage window (F3) once"),
                id="hud-strip-note",
            )

    def on_mount(self) -> None:
        self._apply_needs_you_width(self.size.width)
        self.set_interval(REFRESH_SECONDS, self.redraw)

    def on_resize(self, event: events.Resize) -> None:
        self._apply_needs_you_width(event.size.width)

    async def redraw(self) -> None:
        try:
            await self.recompose()
        except Exception:
            return
        self._apply_needs_you_width(self.size.width)

    def refresh_needs_you(self) -> None:
        """polish list item 3: repaint only the NEEDS YOU card, from `agents_source` and the
        already-cached `self.picture` -- no file read, no recompose. `redraw` above still owns
        the full 30-second rebuild (provider cards included) that keeps the file itself current;
        this is the fast path the deck's own 5-second header refresh calls so a new needs-you
        agent shows up here as soon as it shows on THE PIT's table, instead of up to 30s late."""
        try:
            card = self.query_one(NeedsYouCard)
        except Exception:
            return
        picture = self.picture
        attention = calc.attention_lines(
            picture.get("claude"), picture.get("codex"),
            (picture.get("claude") or {}).get("context_pct"),
            governor=_governor_summary(self.cfg),
            clock=self.cfg.appearance_settings().clock,
        )
        card.rows = self._agents()
        card.attention = attention_for_active(attention, card.rows) + self._extra_attention()
        card.paint()

    def _apply_needs_you_width(self, width: int) -> None:
        try:
            self.query_one(NeedsYouCard).display = width >= NEEDS_YOU_WIDTH_BREAKPOINT
        except Exception:
            pass
