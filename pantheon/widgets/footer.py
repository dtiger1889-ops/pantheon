"""The key strip at the bottom of every window.

Textual's own Footer lists every binding on one line. At 65 columns on the phone it squeezes them
until `open`, `refresh`, `Esc`, `?` and `q` fall off the right edge, and the command-palette entry
takes one of the few slots left. So: under 80 columns
the window shows a hand-written line of the four keys that matter most on the phone and `?`
lists the rest; at 80 columns and wider Textual's footer is used, compact and without the palette
entry (the palette itself still opens with Ctrl+P -- that is where the theme picker lives).

at the desk the keys should read as small lit chips -- the letter in
chrome cyan, the word dim -- rather than Textual's stock footer chrome. `PillFooter` is the same
`Footer` widget with CSS-only styling of its own built-in `.footer-key--key` / `.footer-key--
description` component classes, so every window that calls `make_footer` picks it up with no
change to its own file.

leftover, closed 2026-09-05: the pass above only recoloured `.footer-key--key`'s text -- the
description half of each entry kept Textual's own flat `$footer-description-background` (usually
the same colour as the strip itself), so a key read as a coloured letter floating over plain text,
not a chip. Every other pill in the deck (`widgets/toolbar.py`'s row-action buttons, the command
bar's ` N need you ` pills) is a solid two-tone block: an accent segment plus a neutral segment on
one shared background, no border (Textual draws no rounded corners; the flat-fill treatment is
this deck's stand-in). `PillFooter` now gives each `FooterKey` that same shared `$surface-lighten-1`
background with the key half lit `$primary` and the description half toned to `$text-muted`, so a
key strip reads as a run of two-tone chips with a gap between them (`FooterKey`'s own
`margin-right: 1` in compact mode) instead of tinted text -- CSS only, using only Textual's
built-in variables.
"""
from __future__ import annotations

from rich.text import Text
from textual.widgets import Footer, Static

from .. import theme as theme_mod

NARROW_AT = 80   # the S-UI "P" width: below this the phone line replaces Textual's footer

# One line per window, four keys each, nothing that needs a modifier. The agents panel says `Enter actions` because that is the button the
# phone layout puts under the table.
PHONE_KEYS = {
    "deck": "j jump   k kill   Enter actions   ? keys",
    "supervisor": "n new   j jump   k kill   Enter actions   ? keys",
    "queue": "F1 deck   1-8 tabs   w work   ? keys",
    "hud": "F1 deck   r refresh   ? keys",
    "tools": "r refresh   / filter   ? keys",
    "notes": "Ctrl+T new   F2 rename   Insert (i)   ? keys",
}


class PillFooter(Footer):
    """Textual's `Footer`, restyled as two-tone chips -- key half lit `$primary`, description half
    a neutral `$surface-lighten-1` tone, both on the same `FooterKey` background so each entry
    reads as one pill with a gap before the next -- CSS only, so `Footer`'s own key-dispatch and
    layout are untouched."""

    DEFAULT_CSS = """
    PillFooter { background: $panel; }
    PillFooter FooterKey {
        background: $surface-lighten-1;
    }
    PillFooter FooterKey .footer-key--key {
        background: $primary; color: $background; text-style: bold;
    }
    PillFooter FooterKey .footer-key--description {
        background: $surface-lighten-1; color: $text-muted;
    }
    PillFooter FooterKey:hover {
        background: $primary-muted;
    }
    PillFooter FooterKey:hover .footer-key--key { background: $primary; color: $background; }
    PillFooter FooterKey:hover .footer-key--description {
        background: $primary-muted; color: $foreground;
    }
    PillFooter FooterKey.-disabled .footer-key--key,
    PillFooter FooterKey.-disabled .footer-key--description { text-opacity: 0.45; }
    """


def make_footer() -> Footer:
    """Textual's footer, the way every Pantheon window wants it: `PillFooter` so the keys read as
    chrome-cyan chips at the desk -- unchanged at the phone width, where
    `PhoneFooter`'s hand-written line replaces it entirely (`fit_footer`, below)."""
    return PillFooter(show_command_palette=False, compact=True)


class PhoneFooter(Static):
    """The four-key line shown under 80 columns instead of Textual's footer."""

    DEFAULT_CSS = """
    PhoneFooter { height: 1; padding: 0 1; background: $panel; color: $foreground; display: none; }
    """

    def __init__(self, pane: str, id: str = "phone-footer") -> None:
        super().__init__(PHONE_KEYS.get(pane, PHONE_KEYS["deck"]), id=id, markup=False)


def fit_footer(app, width: int) -> None:
    """Show the phone line under `NARROW_AT` columns and Textual's footer otherwise. Called from
    each app's `on_mount` and `on_resize`; safe to call before either widget exists."""
    narrow = width < NARROW_AT
    for footer in app.query(Footer):
        footer.display = not narrow
    for bar in app.query(WindowBar):
        bar.display = not narrow
    for line in app.query(PhoneFooter):
        line.display = narrow


# The four tmux windows and the key that reaches each from anywhere (bin/pantheon binds F1-F4
# without a prefix). Shown as a bar at the top of every window so "how do I get back to the
# deck" is never a question. `notes` joined the other three 2026-09-05.
WINDOWS = (("F1", "deck"), ("F2", "queue"), ("F3", "budget"), ("F4", "notes"))


def window_bar_text(current: str, wordmark: bool = True) -> Text:
    """` ▰▰ PANTHEON   F1 deck   F2 queue   F3 budget   F4 notes ` with the current window lit."""
    chrome = theme_mod.TOKENS["chrome"]
    dim = theme_mod.TOKENS["dim"]
    line = Text()
    if wordmark:
        line.append(" ▰▰ PANTHEON ", style=f"bold {chrome}")
        line.append("│", style=dim)
    for key, name in WINDOWS:
        line.append(" ")
        if name == current:
            line.append(f" {key} {name} ", style=f"bold black on {chrome}")
        else:
            line.append(f" {key} ", style=f"bold {chrome}")
            line.append(name, style=dim)
    return line


class WindowBar(Static):
    """One row at the top of a standalone window: the wordmark and the F1/F2/F3 window chips.
    Hidden under 80 columns (the phone footer line carries `F1 deck` there)."""

    DEFAULT_CSS = """
    WindowBar { height: 1; background: $panel; }
    """

    def __init__(self, current: str, id: str = "window-bar") -> None:
        super().__init__(window_bar_text(current), id=id)
