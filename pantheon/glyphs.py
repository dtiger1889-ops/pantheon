"""Glyph policy: plain Unicode by default, ASCII when `pantheon.toml` says `glyphs = "ascii"`.
No Nerd Font icons anywhere in the data layer -- `bar`/`sparkline` below always draw from
`UNICODE`/`ASCII` regardless of the `[appearance] icons` setting.

Nerd Font icons are a desktop-only enhancement layered on top of that floor, behind `pantheon.toml`'s `[appearance] icons = true/false`. `NERD` below pairs 1:1 with the status keys used by `_GLYPH_FOR`/`state_cell` in
`pantheon/supervisor/pane.py` (THE PIT's state column) and `icon_table` is how a caller picks
between it and the plain table -- the word label next to the glyph never goes away either way
(state_cell always prints `row.status.label`), so turning icons off loses nothing but the glyph."""
from __future__ import annotations

UNICODE = {
    "ok": "✓",         # check
    "fail": "✗",       # cross
    "working": "●",    # filled circle
    "idle": "○",       # hollow circle
    "attention": "▲",  # triangle
    "square": "■",
    "bar_full": "█",
    "bar_empty": "░",
    "bar_block": "▉",   # a filled cell with a hairline gap on its right (gauge = "blocks")
    "tick": "│",
    "spark": "▁▂▃▄▅▆▇█",
    "dot": "·",
    "dash": "—",
}
ASCII = {
    "ok": "+",
    "fail": "x",
    "working": "*",
    "idle": "-",
    "attention": "!",
    "square": "#",
    "bar_full": "#",
    "bar_empty": ".",
    "bar_block": "#",
    "tick": "|",
    "spark": "_.-=+*#@",
    "dot": ".",
    "dash": "-",
}


# Nerd Font (Font Awesome page) codepoints -- THE PIT's state column only (`icon_table`, below);
# never used by `bar()`/`sparkline()`. Falls back key-for-key to `UNICODE` for anything not listed
# (a Nerd Font glyph nobody has asked for yet), so a new status added to `_GLYPH_FOR` still renders.
NERD = {
    "ok": "",         # nf-fa-check
    "fail": "",       # nf-fa-times
    "working": "",    # nf-fa-circle (filled)
    "idle": "",       # nf-fa-circle_o (hollow)
    "attention": "",  # nf-fa-warning
    "dot": "",        # nf-fa-circle_o -- the "gone" marker; a hollow dot reads better than a
                            # dash next to the other icons
    "dash": "",       # nf-fa-minus
}


def table(mode: str) -> dict[str, str]:
    return ASCII if (mode or "").lower() == "ascii" else UNICODE


def icon_table(mode: str, icons: bool) -> dict[str, str]:
    """The state-glyph table THE PIT and the project picker draw from: `NERD` (falling back to
    `table(mode)` for any key it does not cover) when `[appearance] icons` is true, else the same
    `table(mode)` every other screen and the data layer use. `mode` still governs the fallback so
    a Termius session with `glyphs = "ascii"` and `icons` left off keeps the ASCII floor."""
    base = table(mode)
    if not icons:
        return base
    return {**base, **NERD}


BANDS = (70, 90)  # the house warning bands: caution at 70%, warning at 90%


def bar(pct: float | None, width: int, mode: str = "unicode", blocks: bool = False) -> str:
    """A bullet-graph style bar: filled cells, a target tick at each band. `?` fill when unknown.
    `blocks=True` (`[appearance] gauge = "blocks"`) swaps the fill glyph for `bar_block` -- a
    hairline-separated cell -- everything else about the bullet-graph semantics is unchanged."""
    g = table(mode)
    if pct is None:
        return "?" * width
    pct = max(0.0, min(100.0, pct))
    cells = list(g["bar_empty"] * width)
    filled = int(round(pct / 100 * width))
    fill = g["bar_block"] if blocks else g["bar_full"]
    for i in range(filled):
        cells[i] = fill
    for b in BANDS:
        i = min(width - 1, int(b / 100 * width))
        if cells[i] == g["bar_empty"]:
            cells[i] = g["tick"]
    return "".join(cells)


def sparkline(values: list[float], mode: str = "unicode", width: int | None = None) -> str:
    g = table(mode)["spark"]
    if not values:
        return ""
    vals = values[-width:] if width else values
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    return "".join(g[int((v - lo) / span * (len(g) - 1))] for v in vals)
