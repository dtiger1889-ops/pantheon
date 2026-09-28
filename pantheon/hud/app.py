"""The usage HUD pane (tmux window 2). Answers one question at a glance: how much budget is
left, how fast it is going, and when the window resets -- for every provider Pantheon drives.

It reads `state/hud.json` and nothing else. The collector process (`pantheon/hud/collector.py`,
started beside this window by `bin/pantheon`) is what talks to ccusage and the logs and writes that
file, so this screen never stalls on Node and never forks.

Design rules it obeys: bullet-graph bars with 70/90 band ticks and a sparkline,
plain Unicode with an ASCII switch, colour only as a second signal behind a written label
, nothing moving on its own, and silence unless something actually needs the user.
"""
from __future__ import annotations

import json
import logging
import re
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Digits, Sparkline, Static

from .. import config, glyphs, orphan, theme
from .. import restart as restart_mod
from ..widgets.footer import PhoneFooter, WindowBar, fit_footer, make_footer
from ..models import AgentStatus, format_age, format_clock, parse_ts
from ..session_view.approval import clip as approval_clip
from . import calc, collector, planning, sources

log = logging.getLogger("pantheon.hud")

BAR_CELLS = 20
SPARK_CELLS = 8
MIN_SPARK_SAMPLES = 3   # one or two readings are not a trend; draw nothing rather than a hint
DEFAULT_WIDTH = 80
NAME_COLUMN = 8
STALE_AFTER_SECONDS = 300
PHONE_WIDTH_BREAKPOINT = 90  # below this, one line per provider
TALL_HEIGHT_BREAKPOINT = 40  # rows; polish list item 4 -- room for the HISTORY panel and the
                             # help text shown by default, instead of ~35 blank rows under the cards
PHONE_BAR_CELLS = 8
REDRAW_SECONDS = 5   # how often the window re-reads state/hud.json (the collector process writes it)

PROVIDER_NAMES = {"claude": "Claude", "codex": "Codex"}

HELP_TEXT = """\
BUDGET - what this pane is telling you

  5h / week   how much of the rolling 5-hour and weekly allowance is
              already spent. The bar fills left to right; the two small
              ticks sit at 70% and 90%.
  burn        how fast the budget is going, in percent per hour.
  runs out    when the budget would reach 100% at that rate. "after the
              reset" means the window refills first - nothing to worry
              about.
  at reset    where the 5-hour window lands when it resets, if the burn
              keeps up: the likely figure, then the range the readings
              allow (they are whole percents), then how many readings
              the burn came from. The faint part of the 5h bar is the
              same figure. Gone when there is no burn to go on.
  resets      the clock time this window starts over, in your local time.
  today       what today's work has cost, from the logs on this machine.
  memory      how full this session's own memory is. "compaction soon"
              means Claude Code is about to summarise the conversation
              to make room.
  from        no live session is reporting, so the numbers come from the
  history     logs instead.
  14m ago     the last reading is that old: no session has reported
              since. The number is still the latest known one.
  from your   no Claude Code session has reported lately, so the number
  account     is your account's own, read from Anthropic every few
              minutes. A live session's number wins whenever it is newer.
              Turn it off with \\[usage] account_read = false.
  no reading  nobody has reported this number yet. Pantheon never
  yet         guesses one.

  \\[appearance] hide on the budget card drops named fields: today, burn,
  resets, resets_in, runs_out, memory, as_of, trend, week, projection.

Keys:  r  refresh now    ?  show or hide this help    q  close this window
"""


# --------------------------------------------------------------------------- formatting

def _fmt_pct(value: Optional[float]) -> str:
    return "?" if value is None else f"{value:.0f}%"


def _fmt_money(value: Optional[float]) -> str:
    return "$?" if value is None else f"${value:,.2f}"


def _fmt_clock(iso: Optional[str], clock: str = "24h") -> str:
    """ISO-8601 UTC -> the local clock the user is looking at, in the format `[appearance] clock`
    asks for (`models.format_clock`)."""
    return format_clock(parse_ts(iso), clock)


def _bar_or_blank(pct: Optional[float], cells: int, glyph_mode: str, blocks: bool = False) -> str:
    """The bar, or the same width of blank when the number is unknown: eight question marks read
    as an error, and the `?` after the bar already says it."""
    return glyphs.bar(pct, cells, glyph_mode, blocks) if pct is not None else " " * cells


def _source_tail(usage: dict, clock: str = "24h") -> Optional[str]:
    """Why the numbers are not straight from a live session, for the end of the phone line and
    the desk block's last line. Live statusline: nothing to say. Codex's own log: how old the
    reading is (`as of 07:27`), since Codex has no live feed. Claude with no statusline: the
    percentages only exist in the statusline a Claude Code session writes, and the Desktop app
    writes none -- so a Claude row that says `desktop` on the agents panel does not count, which
    `no live session yet` contradicted on the same screen."""
    source = usage.get("source")
    if source == "account":
        return _account_words(usage)
    age = _reading_age(usage)
    if source in ("statusline", "codex-log"):
        # How old the reading is, only once it is old enough to matter.
        return f"last reading {age}" if age else None
    if usage.get("provider") == "claude":
        return "needs Claude in tmux"
    note = (usage.get("note") or "").strip()
    return note if note and note != "from history" else None


def _account_words(usage: dict, sep: str = " ") -> str:
    """`from your account 2m ago` (with `sep` between the two halves): said only when the headline
    number came from the account's own usage read rather than a live Claude Code session
    (`hud/account.py`), and always with its age, because that read is minutes apart by design."""
    age = calc.age_words(calc.reading_age_seconds(calc.usage_as_of(usage),
                                                  datetime.now(timezone.utc)))
    return f"from your account{sep}{age}" if age else "from your account"


def _join_to_width(parts: list[str], sep: str, width: int) -> str:
    """Join what fits and drop from the end rather than wrapping."""
    kept: list[str] = []
    for part in parts:
        candidate = sep.join(kept + [part])
        if len(candidate) > width:
            break
        kept.append(part)
    return sep.join(kept)


_MARKUP = re.compile(r"\[/?[^\]]*\]")


def _visible(text: str) -> str:
    """How many columns a line really takes: colour markup has no width."""
    return _MARKUP.sub("", text)


def _paint(text: str, tier: str) -> str:
    """Wrap in a colour role, when the tier has one. The words stay readable without it."""
    role = theme.role_for_tier(tier)
    if not role:
        return text
    return f"[{theme.TOKENS[role]}]{text}[/]"


# --------------------------------------------------------------------------- desk cards
#
# `ProviderCard` and `NeedsYouCard` are the desk-width picture: a framed card per provider with a big numeral, two gauges that fill the card, a
# trend line, and two foot lines, plus a card of what needs the user. Built once here so both the
# combined strip (`hud/strip.py`, a row of these) and this window's own F3 screen (below) draw
# the identical card -- review finding 11's fix. Sized from `self.size.width`, which Textual
# only reports correctly once real layout has run, so every number here is computed in
# `on_resize`/`paint`, never assumed at `__init__` or `compose` time.

def _digits_value(pct: Optional[float]) -> str:
    """The big numeral, or nothing: the caption under it says `no reading yet` once instead of
    a large `?`."""
    return f"{pct:.0f}" if pct is not None else ""


def _band(five: Optional[float], week: Optional[float]) -> tuple[str, str]:
    """The word for the worse of the two windows, and its colour, from the same 70/90 bands
    everything else on screen uses: `running low` / `almost out`, or nothing at all under 70%.
    It used to say `clear` in the quiet case, a word that told the user nothing; a
    card with nothing to warn about now has nothing in that corner."""
    worst = None
    for pct in (five, week):
        if pct is not None and (worst is None or pct > worst):
            worst = pct
    tier = theme.tier_for_percent(worst)
    if tier == "warning":
        return "almost out", theme.TOKENS["error"]
    if tier == "caution":
        return "running low", theme.TOKENS["warning"]
    return "", theme.TOKENS["dim"]


def _reading_age(usage: dict) -> Optional[str]:
    """`14m ago` when this provider's newest reading is older than ten minutes, else None
    (a live number needs no label). Pictures written before 2026-09-26 carry no reading time
    and read as live, the way they always did."""
    return calc.stale_age_words(calc.usage_as_of(usage), datetime.now(timezone.utc))


def _gauge(pct: Optional[float], cells: int, glyph_mode: str, blocks: bool = False,
           projected: Optional[float] = None, projected_style: Optional[str] = None) -> Text:
    """A bullet-graph gauge `cells` wide: filled to `pct`, a tick at the 70/90 bands, coloured
    by `theme.gauge_colour`. Reuses `glyphs.table` for every character so `glyphs = "ascii"`
    stays pure ASCII -- no glyph invented here that is not already in that table.
    `projected` draws the track from `pct` to that percent in a faint colour: where
    the window lands at the reset if the burn keeps up."""
    g = glyphs.table(glyph_mode)
    colour = theme.gauge_colour(pct)
    filled = 0 if pct is None else round(max(0.0, min(100.0, float(pct))) / 100 * cells)
    ahead = filled
    if pct is not None and projected is not None:
        ahead = max(filled, round(max(0.0, min(100.0, float(projected))) / 100 * cells))
    ticks = {min(cells - 1, int(band / 100 * cells)) for band in theme.BUDGET_BANDS}
    fill = g["bar_block"] if blocks else g["bar_full"]   # `[appearance] gauge = "blocks"`
    out = Text()
    for i in range(cells):
        if pct is not None and i < filled:
            out.append(fill, style=colour)
        elif i < ahead:
            out.append(g["bar_empty"], style=projected_style or theme.TOKENS["dim"])
        elif i in ticks:
            out.append(g["tick"], style=theme.TOKENS["dim"])
        else:
            out.append(g["bar_empty"], style=theme.TOKENS["gauge_track"])
    return out


def _gauge_line(label: str, pct: Optional[float], cells: int, glyph_mode: str,
                blocks: bool = False, projected: Optional[float] = None,
                projected_style: Optional[str] = None) -> Text:
    """`5h   ███████░░│░░  61%`; an unknown number leaves the track empty and the number
    column blank -- the card's caption already says `no reading yet` once."""
    line = Text(f"{label:<5}", style=theme.TOKENS["dim"])
    line.append_text(_gauge(pct, cells, glyph_mode, blocks, projected, projected_style))
    line.append(f" {_fmt_pct(pct):>4}" if pct is not None else "", style="bold")
    return line


def _dropping_line(parts: list[tuple[str, str, str]], width: int) -> Text:
    """Each part is `(label, value, value_style)`, printed as dim-label + styled-value,
    three spaces apart, dropping trailing parts that would not fit -- the coloured twin of `_join_to_width`, which only ever sees plain strings."""
    out = Text()
    plain = ""
    for i, (label, value, style) in enumerate(parts):
        sep = "   " if i else ""
        candidate = plain + sep + label + value
        if len(candidate) > width:
            break
        if sep:
            out.append(sep)
        out.append(label, style=theme.TOKENS["dim"])
        out.append(value, style=style)
        plain = candidate
    return out


def _batch_text(usage: dict, dot: str) -> Optional[str]:
    """The batch line for a provider's card, from the summary the collector folds into
    `usage["batch"]` (hud/limits.py). None when no batch has been started."""
    try:
        from . import limits
        return limits.batch_text(usage.get("batch"), dot)
    except Exception:
        return None


def _claude_model_effort(cfg) -> tuple[Optional[str], Optional[str]]:
    """The newest statusline capture's model and effort, for a provider card's title (e.g.
    `opus[1m] · high`). Codex writes no statusline, so its card never has one -- `None` rather
    than a made-up name. Fails open: a bad statusline directory never blanks the card."""
    try:
        files = sources.read_statusline_files(cfg.statusline_dir)
    except Exception:
        return None, None
    if not files:
        return None, None
    newest = files[0]
    model = (newest.get("model") or {}).get("display_name") or (newest.get("model") or {}).get("id")
    effort = (newest.get("effort") or {}).get("level")
    return model, effort


class ProviderCard(Vertical):
    """One provider's whole budget picture: a big numeral, two gauges that fill the card, a
    trend line, and two foot lines --  "D. BUDGET" step 1."""

    DEFAULT_CSS = """
    /* step 2/3: two columns of breathing room inside the card, and the
       unfocused border is the chrome_dim hairline every other panel now uses. */
    ProviderCard {
        border: round #1D5B6E;
        border-title-style: bold;
        border-title-color: $primary;
        background: $surface;
        padding: 0 2;
        width: 1fr;
        height: 8;
    }
    ProviderCard .hud-card-top { height: 4; }
    ProviderCard .hud-card-left { width: 14; align-horizontal: left; }
    ProviderCard Digits.hud-card-big { color: $primary; height: 3; }
    ProviderCard .hud-card-caption { color: $text-muted; height: 1; }
    ProviderCard .hud-card-right { width: 1fr; padding-left: 1; }
    ProviderCard .hud-gaugeline { height: 1; }
    ProviderCard .hud-sparkrow { height: 1; }
    ProviderCard .hud-sparklabel { width: 6; color: $text-muted; }
    ProviderCard .hud-spark { height: 1; color: $primary; }
    ProviderCard .hud-sparknone { height: 1; color: $text-muted; }
    ProviderCard .hud-cardfoot { height: 1; }
    ProviderCard Sparkline > .sparkline--max-color { color: $primary; }
    ProviderCard Sparkline > .sparkline--min-color { color: $secondary; }
    """

    def __init__(self, provider_key: str, usage: Optional[dict] = None, cfg=None, *,
                 history: Optional[list] = None,
                 model_effort: Optional[tuple[Optional[str], Optional[str]]] = None,
                 id: Optional[str] = None) -> None:
        super().__init__(id=id or f"provider-card-{provider_key}")
        self.provider_key = provider_key
        self.usage: dict = usage or {}
        self.cfg = cfg
        self.history: list = list(history or [])
        self.model_effort = model_effort or (None, None)
        appearance = cfg.appearance_settings() if cfg is not None else None
        glyph_mode = appearance.glyphs if appearance is not None else "unicode"
        self._ascii = (glyph_mode or "").lower() == "ascii"
        self._blocks = (getattr(appearance, "gauge", "solid") or "solid").lower() == "blocks"
        self._clock = appearance.clock if appearance is not None else "24h"
        # polish list item 2:
        # foot-line field names to drop, plus "trend" (the sparkline row) and "week" (the
        # weekly gauge) -- checked once here rather than re-reading appearance on every paint.
        self._hide = frozenset(appearance.hide) if appearance is not None else frozenset()

    def compose(self) -> ComposeResult:
        with Horizontal(classes="hud-card-top"):
            with Vertical(classes="hud-card-left"):
                yield Digits("", id=f"{self.id}-digits", classes="hud-card-big")
                yield Static("of the 5-hour", id=f"{self.id}-caption", classes="hud-card-caption")
            with Vertical(classes="hud-card-right"):
                yield Static(id=f"{self.id}-5h", classes="hud-gaugeline")
                yield Static(id=f"{self.id}-wk", classes="hud-gaugeline")
                with Horizontal(classes="hud-sparkrow", id=f"{self.id}-sparkrow"):
                    yield Static("trend ", classes="hud-sparklabel")
                    if self._ascii:
                        yield Static("", id=f"{self.id}-spark-text", classes="hud-spark")
                    else:
                        yield Sparkline(
                            [], summary_function=max, id=f"{self.id}-spark-widget",
                            classes="hud-spark",
                        )
                        yield Static(
                            "no samples yet", id=f"{self.id}-spark-none", classes="hud-sparknone",
                        )
        yield Static(id=f"{self.id}-foot1", classes="hud-cardfoot")
        yield Static(id=f"{self.id}-foot2", classes="hud-cardfoot")

    def on_mount(self) -> None:
        self.update_data(self.usage, self.history, self.model_effort)

    def on_resize(self, event) -> None:
        self.paint()

    def update_data(self, usage: Optional[dict] = None, history: Optional[list] = None,
                    model_effort: Optional[tuple] = None) -> None:
        self.usage = usage or {}
        if history is not None:
            self.history = list(history)
        if model_effort is not None:
            self.model_effort = model_effort
        self._set_title()
        self.paint()

    def _set_title(self) -> None:
        name = PROVIDER_NAMES.get(
            self.usage.get("provider") or self.provider_key, self.provider_key.title()
        ).upper()
        model, effort = self.model_effort
        bits = [b for b in (model, effort) if b]
        self.border_title = f"{name}  ·  {' · '.join(bits)}" if bits else name
        word, colour = _band(self.usage.get("five_hour_pct"), self.usage.get("seven_day_pct"))
        if self.usage.get("source") == "account":
            where = _account_words(self.usage, " · ")
        else:
            age = None if "as_of" in self._hide else _reading_age(self.usage)
            where = f"last reading {age}" if age else None
        bits = [b for b in (word, where) if b]
        self.border_subtitle = " · ".join(bits)
        self.styles.border_subtitle_color = colour if word else theme.TOKENS["dim"]

    def paint(self) -> None:
        width = self.size.width or 40
        cells = max(10, width - 32)
        usage = self.usage
        five, week = usage.get("five_hour_pct"), usage.get("seven_day_pct")

        hide = self._hide
        self.query_one(f"#{self.id}-digits", Digits).update(_digits_value(five))
        self.query_one(f"#{self.id}-caption", Static).update(
            "of the 5-hour" if five is not None else "no reading yet"
        )
        proj = self._projection()
        self.query_one(f"#{self.id}-5h", Static).update(
            _gauge_line("5h", five, cells, "ascii" if self._ascii else "unicode", self._blocks,
                        projected=proj["pct"] if proj else None,
                        projected_style=self._projection_style(proj))
        )
        week_widget = self.query_one(f"#{self.id}-wk", Static)
        if "week" in hide:
            week_widget.display = False
        else:
            week_widget.display = True
            week_widget.update(_gauge_line("week", week, cells, "ascii" if self._ascii else "unicode", self._blocks))

        sparkrow = self.query_one(f"#{self.id}-sparkrow", Horizontal)
        has_trend = len(self.history) >= MIN_SPARK_SAMPLES
        if "trend" in hide or not has_trend:
            # Fewer than three readings is not a trend: the row goes rather than saying
            # `no samples yet`.
            sparkrow.display = False
        else:
            sparkrow.display = True
            if self._ascii:
                text_widget = self.query_one(f"#{self.id}-spark-text", Static)
                if has_trend:
                    spark = glyphs.sparkline(
                        [float(v) for v in self.history], "ascii", width=min(24, max(8, cells))
                    )
                    text_widget.update(Text(spark, style=theme.TOKENS["chrome"]))
                else:
                    text_widget.update(Text("no samples yet", style=theme.TOKENS["dim"]))
            else:
                spark_widget = self.query_one(f"#{self.id}-spark-widget", Sparkline)
                none_widget = self.query_one(f"#{self.id}-spark-none", Static)
                spark_widget.display = has_trend
                none_widget.display = not has_trend
                if has_trend:
                    spark_widget.data = [float(v) for v in self.history]

        self.query_one(f"#{self.id}-foot1", Static).update(self._foot1(width))
        self.query_one(f"#{self.id}-foot2", Static).update(self._foot2(width))

    def _runs_before_reset(self) -> bool:
        """True when the burn rate would hit 100% before the window resets -- the one number
        that matters: `burn` and `runs out`
        both get the caution colour then, not just the plain default they used to share."""
        usage = self.usage
        five = usage.get("five_hour_pct")
        resets = usage.get("five_hour_resets_at")
        burn = usage.get("burn_pct_per_hour")
        left = calc.time_to_limit(five, burn, resets)
        return left is not None and calc.format_time_to_limit(left, resets) != ">window"

    def _projection(self) -> Optional[dict]:
        """where the 5-hour window lands at the reset if the burn keeps up, from
        `usage["projection"]` (the collector's `planning.block_projection`). None -- nothing drawn
        -- when hidden, when there is no burn on the card, or when the reading is old enough to
        be dated."""
        usage = self.usage
        proj = usage.get("projection")
        if ("projection" in self._hide or not isinstance(proj, dict)
                or usage.get("burn_pct_per_hour") is None or _reading_age(usage)):
            return None
        if any(proj.get(k) is None for k in ("pct", "low", "high")):
            return None
        return proj

    def _projection_style(self, proj: Optional[dict]) -> str:
        if proj and proj.get("caution"):
            return f"bold {theme.TOKENS['warning']}"
        return theme.TOKENS["dim"]

    def _burn_style(self) -> str:
        return f"bold {theme.TOKENS['warning']}" if self._runs_before_reset() else "bold"

    def _foot1(self, width: int) -> Text:
        usage = self.usage
        hide = self._hide
        burn = usage.get("burn_pct_per_hour")
        burn_cost = usage.get("burn_cost_per_hour")
        if burn is not None:
            burn_text = f"{burn:.0f}%/h"
        elif burn_cost is not None:
            burn_text = f"{_fmt_money(burn_cost)}/h"
        else:
            burn_text = None          # unknown: the field goes, never `burn ?`
        resets = usage.get("five_hour_resets_at")
        today = usage.get("cost_today_usd")
        parts = []
        if "burn" not in hide and burn_text:
            parts.append(("burn ", burn_text, self._burn_style()))
        proj = self._projection()
        if proj:
            style = self._projection_style(proj) if proj.get("caution") else "bold"
            parts.append(("at reset ", planning.projection_words(proj), style))
        if "resets" not in hide and parse_ts(resets) is not None:
            parts.append(("resets ", _fmt_clock(resets, self._clock), "bold"))
        if "today" not in hide and today is not None:
            parts.append(("today ", _fmt_money(today), f"bold {theme.TOKENS['chrome']}"))
        # How old the reading is sits in the card's bottom-right corner (`_set_title`), for
        # Claude and Codex alike; the Codex-only `as of HH:MM` that used to be here is gone.
        return _dropping_line(parts, width)

    def _foot2(self, width: int) -> Text:
        usage = self.usage
        hide = self._hide
        five = usage.get("five_hour_pct")
        resets = usage.get("five_hour_resets_at")
        burn = usage.get("burn_pct_per_hour")
        left = calc.time_to_limit(five, burn, resets)
        runs_out = calc.format_time_to_limit(left, resets)
        runs_out_text = "after the reset" if runs_out == ">window" else runs_out
        if five is not None and five >= 90:
            runs_style = f"bold {theme.TOKENS['error']}"
        elif self._runs_before_reset():
            runs_style = f"bold {theme.TOKENS['warning']}"
        else:
            runs_style = "bold"
        countdown = calc.format_duration(calc.seconds_until(resets))
        parts = []
        batch = _batch_text(usage, "·")
        if batch and "batch" not in hide:
            # The work-the-plate batch: what this run has cost so far. First, so a narrow card drops it last.
            parts.append(("batch ", batch, "bold"))
        if "resets_in" not in hide and countdown != "?":
            parts.append(("resets in ", countdown, "bold"))
        if "runs_out" not in hide and runs_out != "?":
            parts.append(("runs out ", runs_out_text, runs_style))
        context = usage.get("context_pct")
        if context is None or "memory" in hide:
            return _dropping_line(parts, width)
        mem = ("memory ", f"{_fmt_pct(context)} {calc.context_band(context)}", "bold")
        combined = _dropping_line(parts + [mem], width)
        if "memory" not in combined.plain:
            fallback = [parts[0], mem] if parts else [mem]
            combined = _dropping_line(fallback, width)
        return combined


class NeedsYouCard(Vertical):
    """The one card allowed to shout, and only while it is true: every agent waiting on the user
    or that just failed, then the budget attention lines, then the governor line when there is
    one --  "D. BUDGET" step 2.

    Quiet, it is one dim line with no frame (`nothing needs you`) and gives its width back to
    the provider cards; it grows into the framed amber card, as tall as its lines, only when
    something does need the user."""

    QUIET_TEXT = "nothing needs you"
    LOUD_WIDTH = 56
    MAX_LOUD_HEIGHT = 12

    DEFAULT_CSS = """
    /* Keeps its amber border, unlike every other panel: this is the one card allowed to shout
, so the chrome diet gives it the extra padding and nothing else. */
    NeedsYouCard {
        border: round $warning;
        border-title-style: bold;
        border-title-color: $warning;
        background: $surface;
        padding: 0 2;
        width: 56;
        height: auto;
    }
    """

    def __init__(self, rows: Optional[list] = None, attention: Optional[list[str]] = None, *,
                 glyph_mode: str = "unicode", id: Optional[str] = None) -> None:
        super().__init__(id=id or "needs-you-card")
        self.rows = list(rows or [])
        self.attention = list(attention or [])
        self.glyph_mode = glyph_mode

    def compose(self) -> ComposeResult:
        yield Static(id=f"{self.id}-body")

    def on_mount(self) -> None:
        self.border_title = "NEEDS YOU"
        self.paint()

    def paint(self) -> None:
        g = glyphs.table(self.glyph_mode)
        now = datetime.now(timezone.utc)
        needing = [r for r in self.rows if getattr(r, "needs_human", False)]
        failed = [r for r in self.rows if getattr(r, "status", None) is AgentStatus.FAILED]
        body = Text()
        quiet = not needing and not failed and not self.attention
        # An empty card is not an amber alert and not a big empty box either: one dim line, no
        # frame, no title, only as wide as its words.
        if quiet:
            self.styles.border = ("none", theme.TOKENS["chrome_dim"])
            self.border_title = ""
            self.styles.padding = (0, 1)
            self.styles.width = len(self.QUIET_TEXT) + 2
            self.styles.height = 1
            body.append(self.QUIET_TEXT, style=theme.TOKENS["dim"])
        else:
            self.styles.border = ("round", theme.TOKENS["warning"])
            self.styles.border_title_color = theme.TOKENS["warning"]
            self.border_title = "NEEDS YOU"
            self.styles.padding = (0, 2)
            self.styles.width = self.LOUD_WIDTH
            rows_with_marker = (
                [(r, g["attention"], theme.TOKENS["warning"]) for r in needing]
                + [(r, g["fail"], theme.TOKENS["error"]) for r in failed]
            )
            for row, marker, colour in rows_with_marker:
                what = " ".join(x for x in (row.project, row.last_action) if x) or "?"
                # Cut only at the end, saying how much is not shown.
                what = approval_clip(what, 40)[0]
                body.append(f"{marker} ", style=f"bold {colour}")
                body.append(what, style=colour)
                body.append(f"   {format_age(row.age_seconds(now))}\n", style=theme.TOKENS["dim"])
            for text in self.attention:
                body.append(f"{g['attention']} {text}\n", style=theme.TOKENS["warning"])
            body.append("\n")
            body.append("⏎ answer the top one · h hand it over", style=theme.TOKENS["dim"])
            # as tall as its lines plus the frame, capped so a flood cannot push the desk down
            self.styles.height = min(self.MAX_LOUD_HEIGHT, len(body.plain.splitlines()) + 2)
        self.query_one(f"#{self.id}-body", Static).update(body)


class HistoryPanel(Vertical):
    """The usage window's HISTORY panel: the last N samples of
    `history.claude_pct` (and `codex_pct`, once a source ever writes one -- Codex has none yet,
    so its row reads `no samples yet`, the same words `ProviderCard`'s own trend row uses) as a
    wide sparkline per provider, with the min/max/last numbers beside it -- filling the ~35 blank
    rows a desk-sized (200x50) usage window otherwise leaves under the two cards."""

    DEFAULT_CSS = """
    HistoryPanel {
        border: round $secondary;
        border-title-style: bold;
        border-title-color: $primary;
        background: $surface;
        padding: 0 1;
        height: auto;
    }
    HistoryPanel .history-row { height: 1; }
    HistoryPanel .history-label { width: 8; color: $text-muted; }
    HistoryPanel .history-spark { width: 1fr; color: $primary; }
    HistoryPanel .history-stats { width: 30; color: $text-muted; padding-left: 1; }
    HistoryPanel Sparkline > .sparkline--max-color { color: $primary; }
    HistoryPanel Sparkline > .sparkline--min-color { color: $secondary; }
    """

    PROVIDERS = ("claude", "codex")

    def __init__(self, cfg=None, id: Optional[str] = None) -> None:
        super().__init__(id=id or "hud-history")
        self.cfg = cfg
        self.history: dict = {}
        glyph_mode = cfg.appearance_settings().glyphs if cfg is not None else "unicode"
        self._ascii = (glyph_mode or "").lower() == "ascii"

    def compose(self) -> ComposeResult:
        for key in self.PROVIDERS:
            with Horizontal(classes="history-row", id=f"{self.id}-{key}-row"):
                yield Static(PROVIDER_NAMES.get(key, key.title()).upper(), classes="history-label")
                if self._ascii:
                    yield Static("", id=f"{self.id}-{key}-spark-text", classes="history-spark")
                else:
                    yield Sparkline(
                        [], summary_function=max, id=f"{self.id}-{key}-spark-widget",
                        classes="history-spark",
                    )
                yield Static("", id=f"{self.id}-{key}-stats", classes="history-stats")

    def on_mount(self) -> None:
        self.border_title = "HISTORY"
        self.paint()

    def on_resize(self, event) -> None:
        self.paint()

    def update_data(self, history: Optional[dict] = None) -> None:
        self.history = history or {}
        self.paint()

    def paint(self) -> None:
        width = max(20, (self.size.width or 60) - 24)
        for key in self.PROVIDERS:
            samples = [float(v) for v in (self.history.get(f"{key}_pct") or []) if v is not None]
            has = len(samples) >= MIN_SPARK_SAMPLES
            try:
                stats = self.query_one(f"#{self.id}-{key}-stats", Static)
            except Exception:
                continue
            if has:
                stats.update(f"min {min(samples):.0f}%  max {max(samples):.0f}%  last {samples[-1]:.0f}%")
            else:
                stats.update(Text("no samples yet", style=theme.TOKENS["dim"]))
            if self._ascii:
                try:
                    text_widget = self.query_one(f"#{self.id}-{key}-spark-text", Static)
                except Exception:
                    continue
                if has:
                    spark = glyphs.sparkline(samples, "ascii", width=width)
                    text_widget.update(Text(spark, style=theme.TOKENS["chrome"]))
                else:
                    text_widget.update("")
            else:
                try:
                    spark_widget = self.query_one(f"#{self.id}-{key}-spark-widget", Sparkline)
                except Exception:
                    continue
                spark_widget.data = samples if has else []


def render_block(usage: dict, cfg, width: int = DEFAULT_WIDTH,
                 history: Optional[list] = None) -> list[str]:
    """The lines for one provider: the two required lines, plus a memory line and a
    `from history` line when there is something true to put in them."""
    appearance = cfg.appearance_settings()
    glyph_mode = appearance.glyphs
    clock = appearance.clock
    blocks = (appearance.gauge or "solid").lower() == "blocks"
    g = glyphs.table(glyph_mode)
    dot = f" {g['dot']} "
    name = PROVIDER_NAMES.get(usage.get("provider", ""), (usage.get("provider") or "?").title())

    five, week = usage.get("five_hour_pct"), usage.get("seven_day_pct")
    # Pad to a fixed column FIRST, then colour: markup has no width, so the other way round
    # would knock the columns out of line. An unknown number is a blank column, never `?`.
    def pct_text(pct):
        return _paint(f"{_fmt_pct(pct):>4}", theme.tier_for_percent(pct)) if pct is not None else "    "

    if five is None and week is None:
        line1 = f"{name:<{NAME_COLUMN}} no reading yet"
    else:
        line1 = (
            f"{name:<{NAME_COLUMN}} 5h {_bar_or_blank(five, BAR_CELLS, glyph_mode, blocks)} {pct_text(five)}"
            f"   week {_bar_or_blank(week, BAR_CELLS, glyph_mode, blocks)} {pct_text(week)}"
        )
        age = _reading_age(usage)
        if age and len(_visible(line1)) + len(age) + 16 <= width:
            line1 += f"   last reading {age}"
    if history and len(history) >= MIN_SPARK_SAMPLES and (five is not None or week is not None):
        # The trend only earns its place if there is a trend to show and it fits beside the
        # bars; at a narrow width the bars matter more.
        room = width - len(_visible(line1)) - 1
        cells = min(SPARK_CELLS, room)
        if cells >= 3:
            spark = glyphs.sparkline([float(v) for v in history], glyph_mode, width=cells)
            if spark:
                line1 += " " + spark

    burn = usage.get("burn_pct_per_hour")
    resets = usage.get("five_hour_resets_at")
    left = calc.time_to_limit(five, burn, resets)
    runs_out = calc.format_time_to_limit(left, resets)
    burn_cost = usage.get("burn_cost_per_hour")
    if burn is not None:
        burn_text = f"{burn:.0f}%/h"
    elif burn_cost is not None:
        burn_text = f"{_fmt_money(burn_cost)}/h"  # no percent samples yet; ccusage's cost rate is real
    else:
        burn_text = None
    today = usage.get("cost_today_usd")
    proj = usage.get("projection") if burn is not None and not _reading_age(usage) else None
    if "projection" in (appearance.hide or ()):
        proj = None
    proj_text = planning.projection_words(proj) if isinstance(proj, dict) and all(
        proj.get(k) is not None for k in ("pct", "low", "high")) else None
    # Unknown parts are left out, not printed as `?`.
    parts = [p for p in (
        f"burn {burn_text}" if burn_text else None,
        f"at reset {proj_text}" if proj_text else None,
        ("runs out " + ("after the reset" if runs_out == ">window" else runs_out))
        if runs_out != "?" else None,
        f"resets {_fmt_clock(resets, clock)}" if parse_ts(resets) is not None else None,
        f"today {_fmt_money(today)}" if today is not None else None,
    ) if p]
    indent = " " * (NAME_COLUMN + 1)
    lines = [line1]
    if parts:
        lines.append(indent + _join_to_width(parts, dot, width - len(indent)))

    context = usage.get("context_pct")
    if context is not None:
        lines.append(
            f"{indent}memory {glyphs.bar(context, BAR_CELLS, glyph_mode, blocks)} "
            f"{_fmt_pct(context):>4}  {calc.context_band(context)}"
        )
    batch = _batch_text(usage, g["dot"])
    if batch:
        lines.append(f"{indent}batch {batch}")
    source = usage.get("source")
    if source == "codex-log":
        # Codex has no live feed; its log is the source. How old the reading is sits on line
        # 1 (`last reading 3h ago`) once it is old enough to matter -- the same rule as Claude.
        lines.append(f"{indent}from Codex's own log")
    elif source == "account":
        lines.append(f"{indent}{_account_words(usage, ' ' + g['dot'] + ' ')}")
    elif source != "statusline":
        note = (usage.get("note") or "").strip()
        tail = f" {g['dot']} {note}" if note and note != "from history" else ""
        if usage.get("provider") == "claude":
            tail += f" {g['dot']} the percentages need a Claude Code session in tmux"
        lines.append(f"{indent}from history{tail}")
    return lines


def render_block_narrow(usage: dict, cfg, width: int) -> str:
    """One line for a provider under 90 columns: name, a small 5-hour bar and
    percent, the weekly percent as a number, then burn -- dropped from the right when it does
    not fit, with `hist` appended when the source is not the
    live statusline."""
    appearance = cfg.appearance_settings()
    glyph_mode = appearance.glyphs
    blocks = (appearance.gauge or "solid").lower() == "blocks"
    dot = f" {glyphs.table(glyph_mode)['dot']} "
    name = PROVIDER_NAMES.get(usage.get("provider", ""), (usage.get("provider") or "?").title())

    five, week = usage.get("five_hour_pct"), usage.get("seven_day_pct")
    five_text = _paint(_fmt_pct(five), theme.tier_for_percent(five))
    week_text = _paint(_fmt_pct(week), theme.tier_for_percent(week))

    burn = usage.get("burn_pct_per_hour")
    burn_cost = usage.get("burn_cost_per_hour")
    if burn is not None:
        burn_text = f"burn {burn:.0f}%/h"
    elif burn_cost is not None:
        burn_text = f"burn {_fmt_money(burn_cost)}/h"  # no percent samples yet; ccusage's cost rate is real
    else:
        burn_text = None   # unknown: left out, not `burn ?`

    # Unknown numbers are left out; with neither window known the line says so once.
    if five is not None:
        parts = [f"{name} 5h {glyphs.bar(five, PHONE_BAR_CELLS, glyph_mode, blocks)} {five_text}"]
        if week is not None:
            parts.append(f"wk {week_text}")
    elif week is not None:
        parts = [f"{name} wk {week_text}"]
    else:
        parts = [f"{name} no reading yet"]
    # Why the numbers may not be live goes before the burn rate: on a narrow phone the line is
    # cut from the right, and `last reading 35m ago` matters more than the rate beside it.
    tail = _source_tail(usage, appearance.clock)
    if tail:
        parts.append(tail)
    if burn_text:
        parts.append(burn_text)
    return _join_to_width(parts, dot, width)


def render_lines(picture: dict, cfg, width: int = DEFAULT_WIDTH) -> list[str]:
    """Every line of the pane, in order, for a `state/hud.json` picture."""
    appearance = cfg.appearance_settings()
    g = glyphs.table(appearance.glyphs)
    clock = appearance.clock
    series = picture.get("history") or {}
    lines: list[str] = ["BUDGET - how much is left, and how fast it is going", ""]

    providers = []
    for key in ("claude", "codex"):
        usage = picture.get(key)
        if isinstance(usage, dict) and usage:
            providers.append((key, usage))
    if not providers:
        lines.append("No usage data yet. Press r to look now.")
        return lines

    narrow = width < PHONE_WIDTH_BREAKPOINT
    for key, usage in providers:
        if narrow:
            lines.append(render_block_narrow(usage, cfg, width))
        else:
            lines.extend(render_block(usage, cfg, width, series.get(f"{key}_pct") or None))
            lines.append("")

    attention = calc.attention_lines(
        picture.get("claude"), picture.get("codex"),
        (picture.get("claude") or {}).get("context_pct"),
        clock=clock,
    )
    for text in attention:
        lines.append(_paint(f"{g['attention']} {text}"[: width - 1], "caution"))
    if attention:
        lines.append("")

    lines.append(f"read at {_fmt_clock(picture.get('fetched_at'), clock)}")
    return lines


def is_stale(picture: dict, max_age_s: int = STALE_AFTER_SECONDS, now=None) -> bool:
    """True when the file is old enough that its numbers should not be trusted."""
    when = parse_ts(picture.get("fetched_at"))
    if when is None:
        return True
    now = now or datetime.now(timezone.utc)
    return (now - when).total_seconds() > max_age_s


# --------------------------------------------------------------------------- the app

class HudApp(App):
    """One window: a block per provider. `r` refreshes, `?` explains, `q` closes."""

    CSS = """
    Screen { background: $background; }
    #hud-window-bar { height: 1; background: $panel; color: $primary; text-style: bold; padding: 0 1; }
    #hud-cards { height: auto; padding: 1 1 0 1; }
    #hud-readat { height: 1; padding: 0 1; color: $text-muted; }
    #hud-history { height: auto; margin: 0 1; }
    #hud-body { padding: 1 1; height: auto; }
    #hud-help { padding: 1 1; height: auto; background: $panel; }
    #hud-message { padding: 0 1; height: auto; color: #888888; }
    """
    BINDINGS = [
        ("r", "refresh", "refresh"),
        ("question_mark", "help", "keys"),
        ("R", "restart", "restart"),   # pantheon/restart.py (the footer has room here)
        ("q", "quit", "quit"),
    ]

    def __init__(self, cfg=None, start_refresher: bool = True) -> None:
        super().__init__()
        self.cfg = cfg or config.load()
        # True when the collector process runs beside this window (bin/pantheon starts it in the
        # same pane; pantheon/hud/collector.py); `r` then asks it for fresh numbers. False (tests,
        # a bare `python -m pantheon.hud`): only the saved file is read, nothing is asked.
        self.start_refresher = start_refresher
        self.rendered_lines: list[str] = []
        self._message = ""
        self._wide = False
        self._tall = False

    def compose(self) -> ComposeResult:
        # No stock `Header`: the wordmark bar below carries the identity.
        yield WindowBar("budget", id="hud-window-bar")
        with Horizontal(id="hud-cards"):
            yield ProviderCard("claude", cfg=self.cfg, id="hud-window-card-claude")
            yield ProviderCard("codex", cfg=self.cfg, id="hud-window-card-codex")
        yield Static("", id="hud-readat")
        yield HistoryPanel(cfg=self.cfg, id="hud-history")
        with Vertical(id="hud-body-wrap"):
            yield Static("", id="hud-body")
        yield Static(HELP_TEXT, id="hud-help")
        yield Static("", id="hud-message")
        yield PhoneFooter("hud")
        yield make_footer()

    def on_resize(self, event) -> None:
        fit_footer(self, event.size.width)
        self._apply_width(event.size.width)
        self._apply_height(event.size.height)

    def _apply_width(self, width: int) -> None:
        # >= 90 columns: the two provider cards
        # side by side; below that, the phone-width `render_lines` block (unchanged, and still
        # what its own tests exercise directly).
        self._wide = width >= PHONE_WIDTH_BREAKPOINT
        try:
            self.query_one("#hud-cards").display = self._wide
            self.query_one("#hud-readat").display = self._wide
            self.query_one("#hud-body-wrap").display = not self._wide
            self.query_one("#hud-history", HistoryPanel).display = self._wide and self._tall
        except Exception:
            pass

    def _apply_height(self, height: int) -> None:
        # >= 40 rows: room for the HISTORY panel and the help text
        # by default -- `?` still toggles the help off/on either way; at < 40 rows the layout
        # stays exactly what it was (help behind `?`, no history panel).
        self._tall = height >= TALL_HEIGHT_BREAKPOINT
        try:
            self.query_one("#hud-help", Static).display = self._tall
            self.query_one("#hud-history", HistoryPanel).display = self._tall and self._wide
        except Exception:
            pass

    def on_mount(self) -> None:
        theme.apply(self)
        self.title = "PANTHEON - budget"
        fit_footer(self, self.size.width)
        self._apply_width(self.size.width)
        self._apply_height(self.size.height)
        self.redraw()
        # This window only ever reads the file; the collector process writes it.
        self.set_interval(REDRAW_SECONDS, self.redraw)
        orphan.install(self)     # leave when the tmux server is gone (pantheon/orphan.py)

    # ---- drawing -------------------------------------------------------
    def read_picture(self) -> dict:
        try:
            with open(self.cfg.hud_file, "r", encoding="utf-8", errors="replace") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError) as exc:
            self.show_message("Could not read the saved usage file; showing what is on screen.")
            self._log_traceback(exc)
            return {}

    def redraw(self) -> None:
        try:
            picture = self.read_picture()
            # `- 2` is the widget's own left and right padding; drawing into it would wrap.
            width = max(40, (self.size.width or DEFAULT_WIDTH) - 2)
            self.rendered_lines = render_lines(picture, self.cfg, width)
            if picture and is_stale(picture):
                self.rendered_lines.append("These numbers are more than 5 minutes old. Press r.")
            self.query_one("#hud-body", Static).update("\n".join(self.rendered_lines))
            self._redraw_cards(picture)
            self._redraw_history(picture)
            self.query_one("#hud-readat", Static).update(
                f"read at {_fmt_clock(picture.get('fetched_at'), self.cfg.appearance_settings().clock)}"
            )
        except Exception as exc:  # a bad file must not blank the deck
            self.show_message("Something went wrong drawing the budget; details in the log file.")
            self._log_traceback(exc)

    def _redraw_cards(self, picture: dict) -> None:
        series = picture.get("history") or {}
        model_effort = _claude_model_effort(self.cfg)
        try:
            self.query_one("#hud-window-card-claude", ProviderCard).update_data(
                picture.get("claude") or {}, series.get("claude_pct") or [], model_effort
            )
            self.query_one("#hud-window-card-codex", ProviderCard).update_data(
                picture.get("codex") or {}, series.get("codex_pct") or []
            )
        except Exception:
            pass

    def _redraw_history(self, picture: dict) -> None:
        try:
            self.query_one("#hud-history", HistoryPanel).update_data(picture.get("history") or {})
        except Exception:
            pass

    def show_message(self, text: str) -> None:
        self._message = text
        try:
            self.query_one("#hud-message", Static).update(text)
        except Exception:
            pass

    def _log_traceback(self, exc: BaseException) -> None:
        """Tracebacks belong in the log file, never on the user's screen."""
        try:
            path = Path(self.cfg.log_file)
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
        except OSError:
            pass

    # ---- keys ----------------------------------------------------------
    def action_refresh(self) -> None:
        if self.start_refresher:
            collector.poke(self.cfg)
            self.show_message("Asked for fresh usage numbers; they show here within a few seconds.")
            self.set_timer(3, self.redraw)
        else:
            self.show_message("Reading the saved usage file.")
        self.redraw()

    def action_restart(self) -> None:
        """`R`: the same restart screens as the deck (pantheon/restart.py)."""
        restart_mod.ask(self, self.cfg, lambda m, tone=None: self.show_message(m))

    def action_help(self) -> None:
        help_widget = self.query_one("#hud-help", Static)
        help_widget.display = not help_widget.display


def main() -> int:
    cfg = config.load()
    config.ensure_state_dirs(cfg)
    logging.basicConfig(
        filename=str(cfg.log_file), level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    HudApp(cfg).run()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
