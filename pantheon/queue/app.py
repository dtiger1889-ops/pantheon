"""The queue window: one `QueuePane` filling the screen, plus quit and the key list.

Everything the task list does lives in `pane.py`, so the same widget can sit in one column of the
combined desk view (`pantheon/deck/app.py`) without a second copy of the code. This file is only
the wrapper that `python -m pantheon.queue` runs as tmux window 1: it applies the theme, gives the
pane the whole screen, and keeps `q` and `?` at the app level where a Footer can show them.

One set of state, and it lives on the pane: callers and tests read and write it as `app.pane.X`
(e.g. `app.pane.message = ...`), never through the app.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Callable, Optional

from textual.app import App, ComposeResult
from textual.binding import Binding
from .. import config, theme
from .. import orphan as orphan_mod
from .. import restart as restart_mod
from ..widgets.footer import PhoneFooter, WindowBar, fit_footer, make_footer
from .pane import (  # re-exported: these were importable from here before the pane was split out
    KEYS_TEXT,
    QueuePane,
    age_text,
    group_headings,
    matches,
    shorten,
    wrap_summary,
)

__all__ = [
    "QueueApp", "main", "KEYS_TEXT", "age_text", "group_headings", "matches", "shorten",
    "wrap_summary",
]

log = logging.getLogger("pantheon.queue")


class QueueApp(App):
    """One tmux window: the task list."""

    AUTO_FOCUS = None    # the pane focuses itself on mount; nothing else may steal the keyboard

    # item C.5: framed like the combined deck's own panels (`border: round $secondary`, a
    # bold accent title, live counts in the bottom border) once there is room for a frame; below
    # that the phone keeps its borderless full-bleed screen. `.framed` is toggled in `_sync_frame`,
    # never in CSS directly -- Textual has no width media query, only a class to switch on.
    CSS = """
    Screen { layout: vertical; background: $background; }
    QueuePane { height: 1fr; }
    QueuePane.framed {
        border: round $secondary;
        border-title-color: $primary; border-title-style: bold;
        border-subtitle-color: $text-muted;
    }
    """

    FRAME_AT = 80   # matches RowToolbar.COLLAPSE_AT: below this there is no room for a frame either

    BINDINGS = [
        # pantheon/restart.py. Not in the footer: at 120 columns it is already full, and a
        # shown R pushed `? keys` off the end -- `?` lists it.
        Binding("R", "restart", "restart", show=False),
        Binding("q", "quit", "quit"),
        Binding("question_mark", "keys", "keys"),
    ]

    def __init__(self, cfg=None, source=None,
                 window_source: Optional[Callable[[], list]] = None) -> None:
        super().__init__()
        self.cfg = cfg or config.load()
        # Built here, not in compose(), so `app.pane.message = ...` works before the app is running.
        self.pane = QueuePane(self.cfg, source=source, standalone=True,
                              window_source=window_source, id="queue", on_change=self._sync_frame)

    def compose(self) -> ComposeResult:
        yield WindowBar("queue")
        yield self.pane
        yield PhoneFooter("queue")
        yield make_footer()

    def on_mount(self) -> None:
        theme.apply(self)
        self.title = "PANTHEON - queue"
        fit_footer(self, self.size.width)
        orphan_mod.install(self)     # leave when the tmux server is gone (pantheon/orphan.py)
        self._sync_frame()

    def on_resize(self, event) -> None:
        fit_footer(self, event.size.width)
        self._sync_frame()

    def _sync_frame(self) -> None:
        """Keeps the pane's own frame current: on by width, its title naming the current tab, its
        subtitle the pane's own `subtitle` contract. Called on mount, on resize,
        and after every `QueuePane.redraw` (the `on_change` hook above) so a tab switch or a
        cursor move updates the border without a second polling loop."""
        if not self.is_running:
            return
        width = self.size.width
        framed = width >= self.FRAME_AT
        self.pane.set_class(framed, "framed")
        if not framed:
            self.pane.border_title = ""
            self.pane.border_subtitle = ""
            return
        if self.pane.drilled_project:
            # drilled, the title names the project's CHECKPOINT instead of the tab.
            self.pane.border_title = f"QUEUE · CHECKPOINT: {self.pane.drilled_project}"
        else:
            tab_name = self.pane.tab.name if self.pane.tab else ""
            self.pane.border_title = f"QUEUE · {tab_name}" if tab_name else "QUEUE"
        self.pane.border_subtitle = self.pane.subtitle()

    def action_keys(self) -> None:
        """`?` shows the pane's key list, and shows it again to put the tasks back."""
        self.pane.action_keys()

    def action_restart(self) -> None:
        """`R`: the same restart screens as the deck (pantheon/restart.py)."""
        restart_mod.ask(self, self.cfg, lambda m, tone=None: self.pane._say(m, warn=bool(tone)))


def main() -> int:
    cfg = config.load()
    app = QueueApp(cfg)
    if not Path(cfg.sprints_dir).is_dir():
        app.pane.message = f"vault folder not found at {cfg.sprints_dir}; fix it in pantheon.toml"
    try:
        app.run()
    except Exception:
        log.exception("the queue pane stopped")
        print(f"the queue pane stopped; details are in {cfg.log_file}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
