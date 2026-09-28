"""Colour roles: Okabe-Ito truecolor with a 16-colour fallback. Colour is never
the only signal -- every state also prints its text label.

Two layers, kept apart on purpose:
- the DATA layer -- `accent` / `success` / `warning` / `error` / `muted` -- is the Okabe-Ito set,
  chosen to survive colour-blindness and a 16-colour phone terminal; panes paint states with these
  through `TOKENS`, and they do not change;
- the CHROME layer -- `chrome`, `chrome_dim`, `dim`, the cursor band, the zebra shading, the button
  pills -- is the look: near-black ground, one cyan used on every border, title, heading, key chip
  and gauge fill. It reaches CSS as `$primary` / `$secondary` / `$accent` and the variables
  Textual derives from them (`$primary-muted`, `$text-primary`, `$error-muted`,
  `$surface-lighten-1`, ...). CSS must use ONLY those built-ins or literal hex from `TOKENS`:
  a widget's DEFAULT_CSS is parsed before any theme is applied (and under a test-harness app
  that applies none), so a custom `$variable` there is an UnresolvedVariableError at mount
.
"""
from __future__ import annotations

TOKENS = {
    # data layer
    "accent": "#0072B2",   # active / working (blue) -- data only; chrome uses `chrome`
    "success": "#009E73",  # done (green)
    "warning": "#E69F00",  # Caution: waiting on a human, budget 70-90 (amber, steady)
    "error": "#D55E00",    # Warning: crash, budget >=90, destructive confirm (red-orange, bold)
    "muted": "#888888",    # idle / gone
    # chrome layer
    "chrome": "#22D3EE",       # borders, titles, headings, key chips, gauge fill below 70%
    "chrome_dim": "#1D5B6E",   # hairlines, unfocused borders
    "dim": "#5C7480",          # captions, ages, secondary words
    "cursor_band": "#0C3A48",  # the selected row's solid band
    "zebra": "#0A1218",        # every other row
    "gauge_track": "#16242C",  # the empty part of a gauge
    "pill": "#14232C",         # a plain control pill
    "pill_primary": "#0C4E5E",
    "pill_primary_fg": "#BEEBF5",
    "pill_error": "#3A1B0C",
    "pill_error_fg": "#F0B48A",
    "foreground": "#C8D6DC",
    "background": "#05080B",
    "surface": "#0B1116",
    "panel": "#0E1720",
}

# 16-colour names, for TERM=xterm and Android SSH clients without truecolor.
FALLBACK_16 = {
    "accent": "blue",
    "success": "green",
    "warning": "yellow",
    "error": "red",
    "muted": "bright_black",
    "chrome": "cyan",
    "chrome_dim": "cyan",
    "dim": "bright_black",
}

BUDGET_BANDS = (70, 90)  # Silent below 70, Caution 70-90, Warning at/above 90


def tier_for_percent(pct: float | None) -> str:
    """'silent' | 'caution' | 'warning' | 'unknown' -- text, so callers never branch on colour alone."""
    if pct is None:
        return "unknown"
    if pct >= BUDGET_BANDS[1]:
        return "warning"
    if pct >= BUDGET_BANDS[0]:
        return "caution"
    return "silent"


def role_for_tier(tier: str) -> str | None:
    return {"caution": "warning", "warning": "error"}.get(tier)


def gauge_colour(pct: float | None) -> str:
    """The fill colour of a budget gauge: chrome cyan while clear, amber from 70, red-orange from
    90. The number is always printed beside the gauge by the caller."""
    tier = tier_for_percent(pct)
    if tier == "warning":
        return TOKENS["error"]
    if tier == "caution":
        return TOKENS["warning"]
    return TOKENS["chrome"]


def make_textual_theme():
    """Build the Textual `Theme` named 'pantheon'. Imported lazily so tests need no terminal."""
    from textual.theme import Theme

    return Theme(
        name="pantheon",
        primary=TOKENS["chrome"],
        secondary=TOKENS["chrome_dim"],
        accent=TOKENS["chrome"],
        success=TOKENS["success"],
        warning=TOKENS["warning"],
        error=TOKENS["error"],
        foreground=TOKENS["foreground"],
        background=TOKENS["background"],
        surface=TOKENS["surface"],
        panel=TOKENS["panel"],
        dark=True,
    )


def make_high_contrast_theme():
    """The `high-contrast` theme: pure white foreground, no muted grey below 50%
    luminance -- everything that would otherwise be dim is a real, readable colour instead."""
    from textual.theme import Theme

    return Theme(
        name="high-contrast",
        primary=TOKENS["chrome"],
        secondary="#FFFFFF",
        accent=TOKENS["chrome"],
        success=TOKENS["success"],
        warning=TOKENS["warning"],
        error=TOKENS["error"],
        foreground="#FFFFFF",
        background="#000000",
        surface="#0A0A0A",
        panel="#161616",
        dark=True,
    )


def make_light_theme():
    """The `light` theme: the same Okabe-Ito role colours on a light ground
    (`dark=False`)."""
    from textual.theme import Theme

    return Theme(
        name="light",
        primary="#0E7490",
        secondary="#7FB3C2",
        accent="#0E7490",
        success=TOKENS["success"],
        warning=TOKENS["warning"],
        error=TOKENS["error"],
        foreground="#101214",
        background="#F5F5F0",
        surface="#FFFFFF",
        panel="#E7E7E1",
        dark=False,
    )


THEME_FACTORIES = {
    "pantheon": make_textual_theme,
    "high-contrast": make_high_contrast_theme,
    "light": make_light_theme,
}


def apply(app, cfg=None) -> None:
    """`theme.apply(self)` inside `App.on_mount`. `cfg` is optional and falls back to
    `app.cfg` (every app in this project already sets that in `__init__`), then a fresh
    `config.load` -- so every existing `theme.apply(self)` call site keeps working unchanged.
    Textual's own command-palette theme picker (`Ctrl+P`) stays enabled and can preview any
    registered theme live without a config edit."""
    if cfg is None:
        cfg = getattr(app, "cfg", None)
    if cfg is None:
        from . import config as config_mod

        cfg = config_mod.load()
    for factory in THEME_FACTORIES.values():
        app.register_theme(factory())
    wanted = getattr(cfg, "appearance_settings", None)
    theme_name = wanted().theme if callable(wanted) else "pantheon"
    if theme_name not in THEME_FACTORIES:
        # Unknown theme name: start on the default rather than crash or draw nothing.
        theme_name = "pantheon"
    app.theme = theme_name
