"""The deck: `python -m pantheon.supervisor` in tmux window 0.

All the screen lives in `SupervisorPane` (`pane.py`) so the same list can also be one column of the
wide desk layout later. This file is only the app around it: the theme, the `?` key overlay, and
quit. The names other modules and tests already import (`NARROW_COLUMNS`, `WIDE_COLUMNS`,
`row_style`, `state_cell`) are re-exported here so nothing outside had to change.
"""
from __future__ import annotations

from typing import Callable, Optional

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from .. import config as config_mod
from .. import orphan as orphan_mod
from .. import restart as restart_mod
from ..widgets.footer import PhoneFooter, WindowBar, fit_footer, make_footer
from .. import theme as theme_mod
from ..models import TmuxWindow
from .pane import (  # re-exported: existing imports keep working
    NARROW_AT,
    NARROW_COLUMNS,
    PHONE_COLUMNS,
    SUPER_WIDE_AT,
    SUPER_WIDE_COLUMNS,
    WIDE_AT,
    WIDE_COLUMNS,
    SupervisorPane,
    columns_for,
    row_style,
    state_cell,
)

__all__ = [
    "NARROW_AT",
    "NARROW_COLUMNS",
    "PHONE_COLUMNS",
    "SUPER_WIDE_AT",
    "SUPER_WIDE_COLUMNS",
    "SupervisorApp",
    "SupervisorPane",
    "WIDE_AT",
    "WIDE_COLUMNS",
    "columns_for",
    "main",
    "row_style",
    "state_cell",
]

PIT_TITLE = "THE PIT · agents"

# The frame costs 2 columns of border once it appears, so it only turns on once the
# screen is wide enough that losing those 2 columns still leaves the pane's own N-tier width
# (`pane.NARROW_AT`) intact -- otherwise a screen sized exactly at the N/P boundary would frame
# itself straight back into the phone layout, which the design floor ("the 80-column N tier stays
# as it is") forbids.
FRAME_BORDER_COST = 2
FRAME_AT = NARROW_AT + FRAME_BORDER_COST


class SupervisorApp(App):
    """One tmux window holding one `SupervisorPane`."""

    CSS = """
    Screen { layout: vertical; }
    #frame { height: 1fr; width: 1fr; }
    #frame.framed {
        border: round $secondary; border-title-color: $primary; border-title-style: bold;
        border-subtitle-color: $text-muted;
    }
    """

    BINDINGS = [
        ("question_mark", "toggle_keys", "keys"),
        # pantheon/restart.py. Not in the footer: at 120 columns it is already full, and a
        # shown R pushed the last keys off the end -- `?` lists it.
        Binding("R", "restart", "restart", show=False),
        ("q", "quit_deck", "quit"),
    ]

    def __init__(
        self,
        cfg: Optional[config_mod.Config] = None,
        window_source: Optional[Callable[[], list[TmuxWindow]]] = None,
    ) -> None:
        super().__init__()
        self.cfg = cfg or config_mod.load()
        self.pane = SupervisorPane(self.cfg, window_source, id="supervisor")
        self._framed: Optional[bool] = None

    def compose(self) -> ComposeResult:
        yield WindowBar("deck")
        with Vertical(id="frame"):
            yield self.pane
        yield PhoneFooter("supervisor")
        yield make_footer()

    def on_mount(self) -> None:
        theme_mod.apply(self)
        self.title = "PANTHEON - agents"
        fit_footer(self, self.size.width)
        orphan_mod.install(self)     # leave when the tmux server is gone (pantheon/orphan.py)
        self._apply_frame(self.size.width)
        self.set_interval(max(1, int(self.cfg.refresh_seconds or 5)), self._refresh_frame_subtitle)

    def on_resize(self, event) -> None:
        fit_footer(self, event.size.width)
        self._apply_frame(event.size.width)

    def _apply_frame(self, width: int) -> None:
        """B.6: at >= 80 columns wrap the pane in the same frame as the deck (title
        `THE PIT · agents`, a subtitle); below 80, no border (the phone). The threshold is
        `FRAME_AT` (82), not a literal 80 -- see the module-level comment."""
        framed = width >= FRAME_AT
        if framed == self._framed:
            return
        self._framed = framed
        frame = self.query_one("#frame", Vertical)
        frame.set_class(framed, "framed")
        if framed:
            frame.border_title = PIT_TITLE
            self._refresh_frame_subtitle()

    def _refresh_frame_subtitle(self) -> None:
        if not self._framed:
            return
        try:
            frame = self.query_one("#frame", Vertical)
        except Exception:  # pragma: no cover - not mounted yet
            return
        frame.border_subtitle = self.pane.subtitle()

    def action_toggle_keys(self) -> None:
        self.pane.toggle_keys()

    def action_restart(self) -> None:
        """`R`: the same restart screens as the deck (pantheon/restart.py)."""
        restart_mod.ask(self, self.cfg, self.pane.say)

    def action_quit_deck(self) -> None:
        if self.pane.keys_open:
            return self.pane.toggle_keys()
        self.exit()


def main() -> int:
    cfg = config_mod.load()
    try:
        config_mod.ensure_state_dirs(cfg)
    except OSError:
        pass
    SupervisorApp(cfg).run()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
