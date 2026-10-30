"""The rotating tip line on the deck: one row above the
key strip that names, one at a time, a key the strip is NOT showing right now.

The list is computed from the screen's live bindings every time it is drawn, never typed out:
a key is a tip only when it is bound and enabled where the keys go right now, has words below,
and is not already on the footer at this width (`footer_visible_keys` lays the footer out the
same way Textual's compact `Footer` does). So it can never advertise a dead key or one the strip
already shows. Desk width only; the deck hides it under 120 columns (the phone tier).
"""
from __future__ import annotations

from typing import Callable, Iterable

from rich.cells import cell_len
from rich.text import Text
from textual.app import App
from textual.widgets import Static

from .. import theme as theme_mod

# Plain words per action, shown beside the key. An action with no entry is never a tip (that is
# how Textual's own bindings -- ctrl+q, the table's arrows -- stay out). Kept short so a tip fits.
WORDS = {
    # the whole deck window (deck/app.py)
    "next_pane": "move the keys to the next panel",
    "toggle_keys": "every key, then connectors, then tools",
    "new_session": "start a new session: project, who works it, first message",
    "toggle_pit": "tile every agent's real terminal side by side",
    "toggle_queue": "show or hide your task list as a column on the right",
    "edit_file": "edit a file beside the session list",
    "open_in_vscode": "open the highlighted session's folder in VS Code",
    "open_planning": "plan queue rows against your 5-hour and weekly limits",
    "assistant": "put the Assistant beside the session list",
    "assistant_reconnect": "reconnect the Assistant's phone link when it says off",
    "restart": "restart: everything (sessions come back), or the deck only",
    "quit_deck": "quit this deck window; your agents keep running",
    # the agents table (supervisor/pane.py) and the SESSIONS list (supervisor/rail.py)
    "jump": "go to that agent's own full-screen window",
    "kill": "kill that agent's window (asks first)",
    "open_folder": "open the agent's project folder here",
    "copy_id": "copy the session id",
    "refresh_now": "refresh the list right now",
    "model": "pick a model for that agent",
    "effort": "pick an effort level for that agent",
    "mode": "pick a permission mode for that agent",
    "log": "open a Codex job's log",
    "allow": "allow a waiting permission prompt, once",
    "deny": "deny a waiting permission prompt",
    "view_request": "see a permission request word for word",
    "hand_off": "hand a stuck or ended row to the other provider",
    "handoff": "ask it to /checkpoint, then start fresh in its folder",
    "restart_session": "restart an ended Claude session on its own conversation",
    "open_actions": "every action for the selected row",
    "approval('a')": "allow a waiting permission prompt, once",
    "approval('d')": "deny a waiting permission prompt",
    "approval('v')": "see a permission request word for word",
}

# The task list's own meanings where it shares an action name with the agents table.
QUEUE_WORDS = {
    "search": "search the tasks by text",
    "project_filter": "filter the tasks to one project",
    "work_this": "work this task with Claude now",
    "choose_worker": "choose who works this task",
    "open_folder": "open this task's note",
    "source_note": "open the note this task came from",
    "refresh_now": "re-read the task list now",
    "open_detail": "open the task detail",
    "drill_row": "show only this row's project's open threads",
    "undrill": "back to the whole task list",
    "cycle_tab(1)": "next task tab",
    "cycle_tab(-1)": "previous task tab",
}

NUMBER_ACTION = "jump_number("   # the SESSIONS list's 1-9: one tip for the whole run
NUMBER_WORDS = "open that running session, same as clicking it"
SKIP_KEYS = {"escape", "shift+tab"}   # close-an-overlay and the reverse of tab: never news
# Deck keys with no words on purpose: `b` (unstage) only does something while a session is staged
# beside the list, and the tip line is hidden in that layout, so it would always be a dead tip.
NOT_TIPS = {"unstage"}


def words_for(node, action: str) -> str:
    """The plain words for `action` as bound on `node`, or "" when it gets no tip."""
    if type(node).__name__ == "QueuePane" and action in QUEUE_WORDS:
        return QUEUE_WORDS[action]
    return WORDS.get(action, "")


def footer_visible_keys(entries: Iterable[tuple[str, str, str, str]], width: int) -> set[str]:
    """Which keys a compact `Footer` of `width` cells shows whole, given `(key, key_display,
    description, action)` for every shown binding in `active_bindings` order. Mirrors
    `Footer.compose` (one entry per action, its first key) and `FooterKey`'s compact layout
    (key, one space, description, one cell of margin); an entry cut off at the edge counts as
    not shown."""
    visible: set[str] = set()
    seen_actions: set[str] = set()
    x = 0
    for key, display, description, action in entries:
        if action in seen_actions:
            continue
        seen_actions.add(action)
        w = cell_len(display) + (1 + cell_len(description) if description else 0)
        if x + w <= width:
            visible.add(key)
        x += w + 1
    return visible


def compute_tips(active: Iterable[tuple], key_display: Callable, width: int) -> list[tuple[str, str]]:
    """`(key shown, words)` for every tip-worthy binding in `active` (the values of a screen's
    `active_bindings`: `(node, binding, enabled, tooltip)`): the deck-wide keys first (they work
    whatever is selected), then the focused panel's, each in binding order."""
    active = list(active)   # the footer's own order: it decides what fits
    shown = [(b.key, key_display(b), b.description, b.action) for _n, b, _e, _t in active if b.show]
    visible = footer_visible_keys(shown, width)
    active.sort(key=lambda item: 0 if isinstance(item[0], App) else 1)
    tips: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    numbers: list[str] = []
    for node, binding, enabled, _tooltip in active:
        if not enabled or binding.key in visible or binding.key in SKIP_KEYS:
            continue
        if binding.action.startswith(NUMBER_ACTION):
            numbers.append(binding.key)
            continue
        words = words_for(node, binding.action)
        if not words:
            continue
        tip = (key_display(binding), words)
        if tip not in seen:
            seen.add(tip)
            tips.append(tip)
    if numbers:
        tips.append((numbers[0] if len(numbers) == 1 else f"{numbers[0]}-{numbers[-1]}",
                     NUMBER_WORDS))
    return tips


def tip_text(key: str, words: str) -> Text:
    """` tip   P  tile every agent's ...` -- the key lit like a footer chip, the rest dim."""
    line = Text()
    line.append("tip  ", style=theme_mod.TOKENS["dim"])
    line.append(f" {key} ", style=f"bold {theme_mod.TOKENS['chrome']}")
    line.append(f" {words}", style=theme_mod.TOKENS["dim"])
    return line


class TipBar(Static):
    """One muted row above the key strip; the deck feeds it `compute_tips` and turns it."""

    DEFAULT_CSS = """
    TipBar {
        height: 1; padding: 0 1; background: $panel; color: $text-muted;
        text-wrap: nowrap; text-overflow: ellipsis; display: none;
    }
    """

    def __init__(self, id: str = "deck-tip") -> None:
        super().__init__("", id=id)
        self.tips: list[tuple[str, str]] = []
        self.index = 0

    @property
    def current(self) -> tuple[str, str] | None:
        return self.tips[self.index] if self.tips else None

    def show_tips(self, tips: list[tuple[str, str]]) -> None:
        """New list; the tip on screen stays when it is still a tip, else its slot is reused."""
        current = self.current
        self.tips = list(tips)
        if current in self.tips:
            self.index = self.tips.index(current)
        elif self.tips:
            self.index %= len(self.tips)
        else:
            self.index = 0
        self._draw()

    def advance(self) -> None:
        if self.tips:
            self.index = (self.index + 1) % len(self.tips)
        self._draw()

    def _draw(self) -> None:
        tip = self.current
        self.update(tip_text(*tip) if tip else "")
