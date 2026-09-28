"""The queue pane as a widget: the user's task list, the same tabs he sees in Obsidian, read-only.

It runs on its own as tmux window 1 of the deck (`python -m pantheon.queue`, which wraps this in
`QueueApp`), and it will later sit in one column of a combined desk layout -- so everything here
measures the PANE, never the screen, and the keys are the pane's, not the app's.

Nothing here writes to the vault. `o` asks the task's own SOURCE how to open it
: the Obsidian source opens the note's URI at the desk or the Sprints folder in a tmux
window on the phone; the standalone source suspends the deck into `$EDITOR` everywhere, since that
folder is not a vault and there is no separate app to hand off to. `w` / `x` start an agent on the
highlighted task in that task's project folder. The only record a dispatch leaves is the
briefing file under `state/dispatch/` and a line in `state/agents/events.jsonl`.

Screen shapes: at 90 columns and wider the row shows summary, project,
effort and age. Narrower than that -- a phone in portrait -- the age column goes first and the
project name is shortened, because dropping a column beats squeezing every column.
"""
from __future__ import annotations

import functools
import logging
import os
import subprocess
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import quote

from rich.text import Text
from textual import events as tevents
from textual.app import ComposeResult, SuspendNotSupported
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.widget import Widget
from textual.widgets import Input, Static

from .. import config, eventcache as eventcache_mod, glyphs as glyphs_mod, theme, tmuxctl
from ..dispatch import launch as launch_mod, marks as marks_mod
from ..hud import sources as hud_sources
from ..models import TaskRow
from ..providers import get_providers
from ..supervisor import state as state_mod
from ..tasks import get_task_source
from ..tasks import source_note as source_note_mod
from ..tasks.base import TabSpec
from ..tasks.obsidian_base import strip_wikilink
from ..widgets.modal import Confirm, Pick, PickOption
from ..widgets.toolbar import Action, RowToolbar

log = logging.getLogger("pantheon.queue")

NARROW = 66          # below this the layout sheds the age column

# Shorter tab names for the phone header's tab strip (65 columns must hold all eight with their
# number keys and counts). Anything not listed keeps its Base name.
def pack_cells(cells: list[str], width: int, gap: str = "  ") -> list[str]:
    """Lay cells out on as many lines as they need, breaking only BETWEEN cells, never inside
    one. A cell wider than the line still gets its own line rather than being cut."""
    lines: list[str] = []
    current = ""
    for cell in cells:
        candidate = cell if not current else current + gap + cell
        if current and len(candidate) > width:
            lines.append(current)
            current = cell
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


PHONE_TAB_NAMES = {
    "Quick wins": "Quick",
    "My queue": "Mine",
    "Agent's plate": "Plate",
    "By project": "Project",
}
SUMMARY_LINES = 2    # a summary wraps to two lines, then trails off with an ellipsis
# decision 5: the toolbar collapses only when it does not fit (`RowToolbar.set_actions(width=)`);
# the pane no longer computes its own `collapsed=` threshold -- it reads `RowToolbar.collapsed` back
# (`_toolbar_collapsed` below) wherever behaviour used to branch on `TOOLBAR_COLLAPSE_AT`.

# `x` chooses the headless Codex job; `i` is the same adapter in a PC window (S-UI section 6.3).
WHO_OPTIONS = [
    PickOption("c", "claude", "Claude in tmux · the usual"),
    PickOption("x", "codex-headless", "Codex headless job · runs unattended, output in a log"),
    PickOption("i", "codex-pc", "Codex in a PC window · not on the phone"),
    PickOption("l", "ollama", "local model · not switched on"),
]
# value -> (provider_name, interactive)
WHO_VALUES: dict[str, tuple[str, Optional[bool]]] = {
    "claude": ("claude", None),
    "codex-headless": ("codex", False),
    "codex-pc": ("codex", True),
    "ollama": ("ollama", None),
}

# The row toolbar catalogue (S-UI section 5.4).
QUEUE_ACTION_DEFS: dict[str, Action] = {
    "work_this": Action("w", "Work this", "work_this", "primary"),
    "choose_worker": Action("x", "Choose who…", "choose_worker"),
    "open_detail": Action("enter", "Details", "open_detail"),
    "open_folder": Action("o", "Open note", "open_folder"),
    "source_note": Action("O", "Source note", "source_note"),
}
QUEUE_TOOLBAR_ORDER = ["work_this", "choose_worker", "open_detail", "open_folder", "source_note"]

KEYS_TEXT = """\
1-8   jump to a tab (your Base's views, in its order)            Tab / Shift-Tab   next / previous tab
]  [  next / previous tab (these two always work, even beside another pane)
up down   move the highlight   Esc   back
Enter open the task's details, or -- narrower than 120 columns -- everything this row can do
/     search the list          p       filter to one project (press again to change)
w     work this task now, with Claude          x   choose who works it instead
o     open this task's note in Obsidian at the desk; on the phone, open its folder in a shell window
O     open the task's SOURCE note, the same way, if it names one (a URL just shows in this line)
r     re-read the vault now    ?  this list    q  quit
the (...) next to the tab names says how the list stays current: a plain timer, e.g. "every 5s",
or "manual: press r" when pantheon.toml sets refresh_seconds to 0

Words on this screen:
  tab       one of the saved views from your Sprints Base in Obsidian
  blocked   the task is stuck waiting on something
  dispatch  start an agent on this task, in its project folder
  headless  runs on its own with no screen to watch; output goes to a log
  Routing   the running record of which project a task was assigned to, and why
  *         this tab has a rule the deck could not reproduce, so trust Obsidian's count
"""


# ---------------------------------------------------------------- small helpers


def age_text(created: Optional[str], today: Optional[date] = None) -> str:
    """How long a task has been sitting there: `3d`, `5w`, `14mo`. Blank when there is no date."""
    if not created:
        return ""
    try:
        started = datetime.fromisoformat(str(created)[:10]).date()
    except ValueError:
        return ""
    days = ((today or date.today()) - started).days
    if days < 0:
        return "0d"
    if days < 14:
        return f"{days}d"
    if days < 70:
        return f"{days // 7}w"
    return f"{days // 30}mo"


def wrap_summary(text: str, width: int, lines: int = SUMMARY_LINES) -> list[str]:
    """Break a summary over at most `lines` lines; if it still does not fit, end with `...`."""
    width = max(8, width)
    words, out, current = str(text or "").split(), [], ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) <= width:
            current = candidate
            continue
        if current:
            out.append(current)
        if len(out) == lines:
            break
        current = word if len(word) <= width else word[: width - 3] + "..."
    if current and len(out) < lines:
        out.append(current)
    if not out:
        return [""]
    if len(out) == lines and len(" ".join(out)) < len(str(text or "").strip()):
        tail = out[-1]
        out[-1] = (tail[: width - 3] + "...") if len(tail) + 3 > width else tail + "..."
    return out


def shorten(text: Optional[str], width: int) -> str:
    value = str(text or "")
    return value if len(value) <= width else value[: max(1, width - 1)] + "…"


def matches(row: TaskRow, needle: str) -> bool:
    """Search looks in the summary, project, both message lanes, and the file name."""
    needle = needle.strip().lower()
    if not needle:
        return True
    haystack = " ".join(str(v or "") for v in (row.summary, row.project, row.reply, row.note, row.id))
    return needle in haystack.lower()


def queue_toolbar_actions(row: Optional[TaskRow], source_name: str = "") -> list[Action]:
    """The row toolbar's buttons for one task (S-UI section 5.4). `Work this`/`Choose who…` still
    show when the row has no project folder -- pressing them is what tells the user why, the same
    way a disabled button explains itself on the supervisor pane (S-UI section 4.4).

    `open_folder`'s label follows the source: the standalone source has no Obsidian
    to hand off to, so it reads `Open in editor` instead."""
    if row is None:
        return []
    has_folder = bool(row.project)
    open_label = "Open in editor" if source_name == "standalone" else QUEUE_ACTION_DEFS["open_folder"].label
    out = []
    for name in QUEUE_TOOLBAR_ORDER:
        base = QUEUE_ACTION_DEFS[name]
        label = open_label if name == "open_folder" else base.label
        enabled = has_folder if name in ("work_this", "choose_worker") else True
        out.append(Action(base.key, label, base.action_name, base.variant, enabled))
    return out


@dataclass
class _Line:
    """One line of the list body, tagged with enough to style it at desk width
    without re-parsing the plain text back apart. `_list_text` (phone, and the plain fallback)
    reads only `.text`; `_list_text_desk` reads the rest."""

    kind: str            # "blank" | "heading_group" | "row_first" | "row_continuation" | "row_dispatch"
    text: str
    row_index: Optional[int] = None
    blocked: bool = False
    heavy: bool = False


def group_headings(rows: list[TaskRow], tab: TabSpec) -> list[str]:
    """Heading order for a grouped tab: the order the tab asked for, else alphabetical."""
    if tab.group_by is None:
        return []
    present = {tab.group_by(r) for r in rows}
    if tab.group_order:
        ordered = [g for g in tab.group_order if g in present]
        ordered += sorted(present - set(tab.group_order))
        return ordered
    return sorted(present)


# ---------------------------------------------------------------- the pane


class QueuePane(Widget):
    """The task list. Owns its own keys, its own width, and the "work this" flow."""

    can_focus = True

    DEFAULT_CSS = """
    QueuePane { layout: vertical; height: 1fr; }
    QueuePane #header { height: auto; padding: 0 1; background: $panel; color: $foreground; }
    QueuePane #body { height: 1fr; padding: 0 1; }
    QueuePane #toolbar { height: auto; padding: 0 1; }
    QueuePane #search { dock: bottom; height: 3; }
    QueuePane #message { height: auto; padding: 0 1; color: $foreground; }
    """

    BINDINGS = [
        Binding("slash", "search", "search"),
        Binding("p", "project_filter", "project"),
        Binding("w", "work_this", "work this"),
        Binding("x", "choose_worker", "who works it"),
        Binding("o", "open_folder", "open folder"),
        Binding("O", "source_note", "source note"),
        Binding("r", "refresh_now", "refresh"),
        Binding("enter", "open_detail", "details"),
        Binding("escape", "back", "back"),
        # drill to the highlighted row's project (its CHECKPOINT.md Open threads); `D` or
        # `Esc` (via `action_back`'s fallback below) un-drills back to whatever was showing before.
        Binding("d", "drill_row", "drill", show=False),
        Binding("D", "undrill", "un-drill", show=False),
        # `priority` because Textual would otherwise use Tab to move between widgets.
        # `check_action` turns these off when the pane is not the whole screen, so Tab is free
        # to move between panes in the combined desk layout.
        Binding("tab", "next_tab", "next tab", show=False, priority=True),
        Binding("shift+tab", "prev_tab", "previous tab", show=False, priority=True),
        # Always on, standalone or not: the tab keys that never clash with anything.
        Binding("right_square_bracket", "cycle_tab(1)", "next tab", show=False),
        Binding("left_square_bracket", "cycle_tab(-1)", "previous tab", show=False),
        Binding("down", "move(1)", "down", show=False),
        Binding("up", "move(-1)", "up", show=False),
        Binding("j", "move(1)", "down", show=False),
        Binding("k", "move(-1)", "up", show=False),
    ]
    for _n in "123456789":
        BINDINGS.append(Binding(_n, f"go_tab('{_n}')", f"tab {_n}", show=False))
    del _n

    def __init__(self, cfg=None, source=None, standalone: bool = True, id: str = "queue",
                 window_source: Optional[Callable[[], list]] = None,
                 agent_rows_source: Optional[Callable[[], list]] = None,
                 on_change: Optional[Callable[[], None]] = None) -> None:
        super().__init__(id=id)
        # contract: `QueueApp` (standalone) uses this to keep its own frame's border title,
        # subtitle and border class current after anything that calls `redraw` -- the combined
        # deck draws its own frame (build A) and never passes this.
        self._on_change = on_change
        self.cfg = cfg or config.load()
        self._source = source
        self.standalone = standalone
        # The default goes through the short-lived cache (perf sweep item 2), same as the
        # supervisor pane's default -- inside the combined deck the two usually share one
        # subprocess spawn instead of each pane starting its own.
        self._window_source = window_source or (lambda: tmuxctl.list_windows_cached(None))
        # Set only by `DeckApp` (perf sweep item 1): reuse the supervisor pane's own last fold
        # instead of folding the whole event log again just for the dispatch concurrency guard.
        # `None` (every standalone `QueueApp`) means `agent_rows()` keeps folding on its own.
        self._agent_rows_source = agent_rows_source
        self.tabs: list[TabSpec] = []
        self.tab_index = 0
        self.cursor = 0
        # drill-down: the source(s) the queue swapped OUT of, so `undrill` restores the
        # vault-wide view exactly (source, tabs, tab_index, cursor, drilled_project it replaced).
        self._drill_stack: list[dict] = []
        self.drilled_project: Optional[str] = None
        self.search_text = ""
        self.project_filter: Optional[str] = None
        self.view: str = "list"        # "list" | "detail" | "keys"
        self.message = ""
        self._message_style: Optional[str] = None
        self.rows_on_screen: list[TaskRow] = []
        self.providers: dict = {}
        self._marks: dict = {}
        self._last_body_text: Optional[str] = None   # perf sweep item 3: skip a no-op redraw
        # contract: UTC, set on every successful read of the task source; the deck's status
        # bar prints `vault read HH:MM` from it.
        self.last_read_at: Optional[datetime] = None

    # -- layout -------------------------------------------------------

    def compose(self) -> ComposeResult:
        # `markup=False` everywhere: a task summary may contain square brackets, and Textual
        # would otherwise read them as formatting instructions and mangle the row.
        yield Static("", id="header", markup=False)
        yield Static("", id="message", markup=False)
        with Vertical(id="body"):
            scroll = VerticalScroll()
            scroll.can_focus = False      # the keys belong to the pane, not to the scroll box
            with scroll:
                yield Static("", id="list", markup=False)
        yield RowToolbar(self._toolbar_dispatch, id="toolbar")
        # Starts hidden AND out of the way of the keyboard: otherwise it takes the focus on
        # startup and every key press is typed into it instead of doing what it says on the tab.
        search = Input(placeholder="type to search, Esc to clear", id="search")
        search.can_focus = False
        yield search

    def on_mount(self) -> None:
        self._setup_logging()
        search = self.query_one("#search", Input)
        search.display = False
        search.can_focus = False
        self.focus()
        try:
            self._source = self._source or get_task_source(self.cfg)
        except Exception as exc:
            log.exception("could not start the task source")
            self.message = f"could not read your task list: {exc}"
            self._source = None
        if self._source is not None:
            self.tabs = self._source.tabs()
            if self._source.notice and not self.message:   # e.g. "Base file not found at ...; showing the built-in tabs"
                self.message = self._source.notice
        try:
            self.providers = get_providers(self.cfg)
        except Exception as exc:
            log.exception("could not read the provider list")
            self.providers = {}
            if not self.message:   # never write over a message the caller already set
                self.message = f"could not read the list of agents from pantheon.toml: {exc}"
        # So the
        # folder watcher is gone -- `refresh_seconds = 0` means manual only (the `r` key), and
        # anything above 0 is this plain timer, never live.
        if self.cfg.refresh_seconds > 0:
            self.set_interval(self.cfg.refresh_seconds, self._tick)
        if self._source is not None:
            self.last_read_at = datetime.now(timezone.utc)
        self.redraw()

    def _setup_logging(self) -> None:
        try:
            path = Path(self.cfg.log_file)
            path.parent.mkdir(parents=True, exist_ok=True)
            handler = logging.FileHandler(path, encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
            root = logging.getLogger("pantheon")
            root.setLevel(logging.INFO)
            root.addHandler(handler)
        except OSError:
            pass  # a deck that cannot open its log file still runs

    def check_action(self, action: str, parameters: tuple) -> Optional[bool]:
        """Tab cycles the queue's own tabs only when the queue IS the screen. Beside another
        pane, `None` hides the binding so Tab travels up to the app and moves between panes."""
        if action in ("next_tab", "prev_tab") and not self.standalone:
            return None
        return True

    # -- data ---------------------------------------------------------

    def _tick(self) -> None:
        if self._source is None:
            return
        try:
            changed = self._source.refresh()
            self.last_read_at = datetime.now(timezone.utc)
            if changed:
                self.redraw()
            else:
                self._read_marks()
                self._paint_header()
        except Exception:
            log.exception("refresh failed")

    def _all_rows(self) -> list[TaskRow]:
        return self._source.rows() if self._source is not None else []

    def rows_for_tab(self, index: int) -> list[TaskRow]:
        rows = self._all_rows()
        if not self.tabs:
            return []
        tab = self.tabs[max(0, min(index, len(self.tabs) - 1))]
        kept = [r for r in rows if tab.filter(r)]
        if self.project_filter:
            kept = [r for r in kept if (r.project or "") == self.project_filter]
        if self.search_text:
            kept = [r for r in kept if matches(r, self.search_text)]
        return sorted(kept, key=tab.sort_key)

    @property
    def tab(self) -> Optional[TabSpec]:
        return self.tabs[self.tab_index] if self.tabs else None

    def selected_row(self) -> Optional[TaskRow]:
        if not self.rows_on_screen:
            return None
        return self.rows_on_screen[max(0, min(self.cursor, len(self.rows_on_screen) - 1))]

    def projects(self) -> list[str]:
        return sorted({r.project for r in self._all_rows() if r.project})

    def _events(self) -> list:
        try:
            found, _ = eventcache_mod.read_events_cached(self.cfg.events_file)
        except OSError:
            log.exception("could not read the event log")
            return []
        return found

    def _windows(self) -> list:
        try:
            return list(self._window_source() or [])
        except Exception:
            log.exception("could not ask tmux for its windows")
            return []

    def agent_rows(self) -> list:
        """The live agent rows the concurrency guard counts (same fold the supervisor draws).

        Inside the combined deck, `DeckApp` hands in `agent_rows_source` (perf sweep item 1): the
        supervisor pane already folds the same events on its own timer, so this just reads that
        result instead of re-folding the whole event log again for a `w`/`x` press. A standalone
        `QueueApp` has no supervisor pane to ask, so it keeps folding here, as before."""
        if self._agent_rows_source is not None:
            return list(self._agent_rows_source() or [])
        mtimes = hud_sources.statusline_mtimes(self.cfg.statusline_dir)
        return state_mod.fold(self._events(), self._windows(), datetime.now(timezone.utc),
                              self.cfg.tmux_session, self.cfg.projects_root, statusline_mtimes=mtimes)

    def _read_marks(self) -> None:
        """Which rows have an agent on them right now. One file read, so it is cheap per tick."""
        if not Path(self.cfg.events_file).exists():
            self._marks = {}
            return
        try:
            self._marks = marks_mod.dispatch_marks(self._events(), self._windows())
        except Exception:
            log.exception("could not work out which rows are dispatched")
            self._marks = {}

    # -- painting -----------------------------------------------------

    def redraw(self) -> None:
        self._read_marks()
        self._paint_header()
        self._paint_message()
        self._refresh_toolbar()
        body = self.query_one("#list", Static)
        try:
            if self.view == "keys":
                kind = self._source.kind if self._source is not None else "unknown"
                return self._update_body(body, f"source: {kind}\n\n{KEYS_TEXT}")
            if self.view == "detail":
                return self._update_body(body, self._detail_text(self.selected_row()))
            self.rows_on_screen = self.rows_for_tab(self.tab_index)
            self.cursor = max(0, min(self.cursor, max(0, len(self.rows_on_screen) - 1)))
            width = self.size.width or 80
            if width >= NARROW:
                self._update_body(body, self._list_text_desk())
            else:
                self._update_body(body, self._list_text())
        finally:
            # contract: the standalone `QueueApp` keeps its own frame's border title/subtitle
            # current off the back of every redraw (deck A never passes `on_change`).
            if self._on_change is not None:
                self._on_change()

    def _update_body(self, body: Static, text: str) -> None:
        """Perf sweep item 3: an idle tick recomputes the same text every time (nothing in the
        vault or the marks changed) -- skip the `Static.update()` call, not the computation,
        when it is byte-identical to what is already on screen."""
        if text == self._last_body_text:
            return
        self._last_body_text = text
        body.update(text)

    def _refresh_toolbar(self) -> None:
        """The row toolbar (S-UI section 5.4), rebuilt for whatever is highlighted right now.
        Hidden in the `keys` overlay -- there is no row to act on while it is showing."""
        try:
            bar = self.query_one("#toolbar", RowToolbar)
        except Exception:  # pragma: no cover - not mounted yet
            return
        row = self.selected_row() if self.view != "keys" else None
        source_name = self._source.name if self._source is not None else ""
        actions = queue_toolbar_actions(row, source_name)
        width = self.size.width or self.app.size.width
        bar.set_actions(actions, width=width, collapse_below=NARROW)

    def _toolbar_collapsed(self) -> bool:
        """decision 5: the three places that used to branch on `TOOLBAR_COLLAPSE_AT` now read
        this back instead -- `RowToolbar` itself decides, from the width `_refresh_toolbar` just
        handed it, whether it fits as buttons or collapses to one `Actions… (Enter)` pill."""
        try:
            return self.query_one("#toolbar", RowToolbar).collapsed
        except Exception:  # pragma: no cover - not mounted yet
            return True

    def _paint_message(self) -> None:
        """One sentence under the header. Amber only when something went wrong -- and the words
        say so too, because colour is never the only signal. Nothing here
        disappears on a timer; it stays until the next thing happens."""
        try:
            widget = self.query_one("#message", Static)
        except Exception:
            return
        text = self.message or ""
        if text and self._message_style:
            widget.update(Text(text, style=theme.TOKENS.get(self._message_style, "")))
        else:
            widget.update(text)

    def _say(self, message: str, warn: bool = False) -> None:
        self.message = message
        self._message_style = "warning" if warn else None
        self._paint_message()

    def _tab_counts(self) -> list[int]:
        """Every tab's row count in one pass over `_all_rows()` (perf sweep item 4): the header
        used to call `rows_for_tab()` -- filter AND sort -- once per tab just to `len()` the
        result and throw the sorted list away."""
        rows = self._all_rows()
        if self.project_filter:
            rows = [r for r in rows if (r.project or "") == self.project_filter]
        if self.search_text:
            rows = [r for r in rows if matches(r, self.search_text)]
        return [sum(1 for r in rows if tab.filter(r)) for tab in self.tabs]

    def _paint_header(self) -> None:
        width = self.size.width or 80
        flagged = self._source.flagged_tabs() if self._source is not None else set()
        tab_counts = self._tab_counts()
        refresh_label = self._refresh_label()
        extras = self._header_extras(tab_counts)
        header = self.query_one("#header", Static)
        if width < NARROW:
            header.update(self._phone_header(flagged, tab_counts, refresh_label, extras, width))
        else:
            header.update(self._desk_header(flagged, tab_counts, refresh_label, extras, width))

    def _header_extras(self, tab_counts: list[int]) -> list[str]:
        extras = []
        if self.project_filter:
            extras.append(f"project: {self.project_filter}")
        if self.search_text:
            current = tab_counts[self.tab_index] if self.tabs else 0
            extras.append(f"search '{self.search_text}': {current} found")
        return extras

    def _phone_header(self, flagged: set, tab_counts: list[int], refresh_label: str,
                      extras: list[str], width: int) -> str:
        # The phone header used to show only the current tab, and nothing on screen said the
        # other tabs existed or how to reach them. Line 1 the deck name and refresh mode, line 2 every tab with the
        # number key that jumps to it, the current one in brackets. decision 6: this branch's
        # CHARACTERS do not change at the desk look pass -- only the threshold that reaches it
        # moved.
        line = f"PANTHEON · queue  ({refresh_label})"
        if extras:
            line += "  " + "  ".join(extras)
        strip = []
        for i, tab in enumerate(self.tabs):
            name = PHONE_TAB_NAMES.get(tab.name, tab.name) + ("*" if tab.name in flagged else "")
            cell = f"{tab.key} {name} {tab_counts[i]}"
            strip.append(f"[{cell}]" if i == self.tab_index else cell)
        # The header has one column of padding each side (its CSS), so the strip packs to
        # width - 2, breaking only between tabs: left to wrap itself, the Static split
        # `6 Notes 52` across two lines.
        line += "\n" + "\n".join(pack_cells(strip, max(20, width - 2)))
        return line

    def _desk_header(self, flagged: set, tab_counts: list[int], refresh_label: str,
                     extras: list[str], width: int) -> Text:
        """item C.2: the tab strip as lit pills, the current one bold black-on-cyan, the
        others dim key + name + dim count -- no `PANTHEON · queue` prefix (the deck's own command
        bar already says where we are); `(every 5s)` stays, dim, at the right."""
        # Each tab is one cell; cells break only BETWEEN tabs (the phone header's `pack_cells`
        # rule), so a pill is never split across two lines (found in the 200x50 render).
        content_width = max(10, width - 2)   # `#header`'s own CSS: `padding: 0 1`
        cells: list[Text] = []
        for i, tab in enumerate(self.tabs):
            name = tab.name + ("*" if tab.name in flagged else "")
            count = tab_counts[i]
            # Three parts, each styled differently so they never read as one run of words
            #: the KEY is a dim digit before
            # the name, the NAME is the pill, the COUNT is a small badge after it.
            cell = Text()
            badge = f"bold {theme.TOKENS['chrome']} on {theme.TOKENS['cursor_band']}"
            if i == self.tab_index:
                cell.append(f" {tab.key} {name}", style=f"bold black on {theme.TOKENS['chrome']}")
            else:
                cell.append(f"{tab.key} ", style=theme.TOKENS["dim"])
                cell.append(name, style=theme.TOKENS["foreground"])
            cell.append(f" {count} ", style=badge)
            cells.append(cell)
        lines: list[Text] = []
        current = Text()
        for cell in cells:
            if current.plain and len(current.plain) + 2 + len(cell.plain) > content_width:
                lines.append(current)
                current = Text()
            if current.plain:
                current.append("  ")
            current.append_text(cell)
        if extras:
            current.append("   " + "   ".join(extras))
        right = f"({refresh_label})"
        pad = max(1, content_width - len(current.plain) - len(right))
        current.append(" " * pad)
        current.append(right, style=theme.TOKENS["dim"])
        lines.append(current)
        text = Text(chr(10)).join(lines)
        return text

    def _refresh_label(self) -> str:
        """What the header's parenthetical says about how the list stays current: a plain timer, or manual only when `refresh_seconds` is 0."""
        seconds = int(self.cfg.refresh_seconds)
        return "manual: press r" if seconds <= 0 else f"every {seconds}s"

    def subtitle(self) -> str:
        """contract: `QueuePane.subtitle -> str`, e.g. `1 of 3 · every 5s` -- the deck (and
        this pane's own standalone frame) puts it in the panel's bottom border."""
        if not self.tabs:
            return ""
        total = len(self.rows_on_screen)
        position = min(self.cursor + 1, total) if total else 0
        return f"{position} of {total} · {self._refresh_label()}"

    SUMMARY_CAP = 72   # polish list item 5: at 200 columns the summary column used to
                       # stretch to ~165 characters; cap it so project/effort/age sit at a
                       # readable fixed position instead, with the rest left as blank trailing
                       # space (rows are already `.rstrip`-ed in `_row_segments`, so nothing
                       # stretches into it -- the summary column just stops growing past this).

    def _columns(self) -> tuple[int, int, int, bool]:
        """Widths for summary / project / effort, and whether the age column fits."""
        width = self.size.width or 80
        narrow = width < NARROW
        project_w = 8 if narrow else 12
        effort_w = 8
        show_age = not narrow
        fixed = 4 + project_w + 1 + effort_w + 1 + (6 + 1 if show_age else 0)
        # `- 6`: the body's own padding (2) plus the scroll bar (2) plus the `>` cursor gutter
        # rounding -- at 76 columns the age wrapped onto its own line.
        summary_w = max(20, min(self.SUMMARY_CAP, width - fixed - 6))
        return summary_w, project_w, effort_w, show_age

    def _branch(self) -> str:
        """The little elbow in front of a dispatched line, ASCII when the config asks for it."""
        return "->" if glyphs_mod.table(self.cfg.appearance_settings().glyphs) is glyphs_mod.ASCII else "↳"

    def _list_blocks(self) -> list[tuple[str, list[TaskRow]]]:
        """Rows grouped by heading, in the tab's own order -- shared by `_list_text()` and
        `_list_lines()` so the two can never drift apart on which rows go where."""
        tab = self.tab
        headings = group_headings(self.rows_on_screen, tab)
        return ([(h, [r for r in self.rows_on_screen if tab.group_by(r) == h]) for h in headings]
                if headings else [("", self.rows_on_screen)])

    def _row_segments(self, row: TaskRow, index: int, summary_w: int, project_w: int,
                      effort_w: int, show_age: bool) -> tuple[str, list[str], Optional[str]]:
        """One row's first line, its wrapped-summary continuation lines, and its dispatch
        sub-line (`None` when no agent is on it) -- split apart so the desk renderer can style
        the dispatch line on its own without guessing which line it is from the text."""
        marker = ">" if index == self.cursor else " "
        effort = row.complexity or ""
        if (row.status or "").lower() == "blocked":
            effort = "blocked"          # printed as a word, never colour alone
        wrapped = wrap_summary(row.summary, summary_w)
        first = (
            f"{marker} {wrapped[0]:<{summary_w}}  "
            f"{shorten(row.project, project_w):<{project_w}} "
            f"{shorten(effort, effort_w):<{effort_w}}"
        )
        if show_age:
            first += f" {age_text(row.created):>6}"
        first = first.rstrip()
        continuation = [f"  {extra}" for extra in wrapped[1:]]
        dispatch = None
        mark = self._marks.get(row.id)
        if mark is not None:
            width = (self.size.width or 80) - 2
            dispatch = "  " + shorten(f"{self._branch()} {mark.label()}", max(10, width))
        return first, continuation, dispatch

    def _row_lines(self, row: TaskRow, index: int, summary_w: int, project_w: int,
                   effort_w: int, show_age: bool) -> list[str]:
        first, continuation, dispatch = self._row_segments(row, index, summary_w, project_w,
                                                            effort_w, show_age)
        lines = [first, *continuation]
        if dispatch is not None:
            lines.append(dispatch)
        return lines

    def _list_lines(self) -> list[_Line]:
        """Every line of the list body, tagged: `_list_text` joins `.text` for
        the phone's plain string; `_list_text_desk` reads the tags to paint the desk's heading
        row, cursor band, zebra shading and column colours over the SAME characters."""
        if self.tab is None or not self.rows_on_screen:
            return []
        summary_w, project_w, effort_w, show_age = self._columns()
        lines: list[_Line] = []
        index = 0
        for heading, members in self._list_blocks():
            if not members:
                continue
            if heading:
                lines.append(_Line("blank", ""))
                lines.append(_Line("heading_group", f"-- {heading} ({len(members)})"))
            for row in members:
                first, continuation, dispatch = self._row_segments(
                    row, index, summary_w, project_w, effort_w, show_age)
                blocked = (row.status or "").lower() == "blocked"
                heavy = (row.complexity or "") == "heavy"
                lines.append(_Line("row_first", first, row_index=index, blocked=blocked, heavy=heavy))
                for extra in continuation:
                    lines.append(_Line("row_continuation", extra, row_index=index))
                if dispatch is not None:
                    lines.append(_Line("row_dispatch", dispatch, row_index=index))
                index += 1
        # Mirrors the old `"\n".join(out).strip("\n")`: a grouped tab's `out` always opened with a
        # blank marker before its first heading, which `.strip("\n")` then removed.
        while lines and lines[0].kind == "blank":
            lines.pop(0)
        while lines and lines[-1].kind == "blank":
            lines.pop()
        return lines

    def _list_text(self) -> str:
        tab = self.tab
        if tab is None:
            return "No tabs. Check that the Base file exists at " + str(self.cfg.sprints_base)
        if not self.rows_on_screen:
            return self._empty_text()
        return "\n".join(line.text for line in self._list_lines())

    # -- the desk-width styled body ---------------------

    def _heading_row(self, summary_w: int, project_w: int, effort_w: int, show_age: bool) -> str:
        """`task  project  effort  age`, at the same fixed column positions `_row_segments()`
        prints a row's own fields at -- new text, only ever built at desk width."""
        line = f"  {'task':<{summary_w}}  {'project':<{project_w}} {'effort':<{effort_w}}"
        if show_age:
            line += f" {'age':>6}"
        return line.rstrip()

    def _column_offsets(self, summary_w: int, project_w: int, effort_w: int, show_age: bool):
        """Character offsets of the project/effort/age fields within a row's first line -- derived
        from the exact same arithmetic `_row_segments()` uses to build that line, so the desk
        renderer can colour those columns without re-parsing the text."""
        summary_start = 2
        summary_end = summary_start + summary_w
        project_start = summary_end + 2
        project_end = project_start + project_w
        effort_start = project_end + 1
        effort_end = effort_start + effort_w
        if show_age:
            age_start = effort_end + 1
            age_end = age_start + 6
        else:
            age_start = age_end = None
        return project_start, project_end, effort_start, effort_end, age_start, age_end

    def _row_band(self, row_index: Optional[int]) -> Optional[str]:
        """The row's background: a solid cursor band on the selected row, faint zebra shading on
        alternate rows, nothing otherwise."""
        if row_index is None:
            return None
        if row_index == self.cursor:
            return theme.TOKENS["cursor_band"]
        if row_index % 2 == 1:
            return theme.TOKENS["zebra"]
        return None

    def _append_styled_line(self, text: Text, line: _Line, summary_w: int, project_w: int,
                            effort_w: int, show_age: bool) -> None:
        if line.kind == "blank":
            return
        if line.kind == "heading_group":
            text.append(line.text, style=f"bold {theme.TOKENS['dim']}")
            return
        if line.kind == "row_dispatch":
            text.append(line.text, style=theme.TOKENS["chrome"])
            return
        if line.kind == "row_continuation":
            band = self._row_band(line.row_index)
            text.append(line.text, style=f"on {band}" if band else "")
            return
        # row_first: the cursor row gets a solid full-width band and bold; zebra otherwise; then
        # project/age in dim, effort amber for heavy/blocked.
        start = len(text)
        is_cursor = line.row_index == self.cursor
        band = self._row_band(line.row_index)
        base = " ".join(p for p in (("bold" if is_cursor else ""), (f"on {band}" if band else "")) if p)
        text.append(line.text, style=base)
        project_start, project_end, effort_start, effort_end, age_start, age_end = (
            self._column_offsets(summary_w, project_w, effort_w, show_age))
        end = len(line.text)
        if project_start < end:
            text.stylize(theme.TOKENS["dim"], start + project_start, start + min(project_end, end))
        if effort_start < end:
            effort_style = theme.TOKENS["dim"]
            if line.heavy:
                effort_style = theme.TOKENS["warning"]
            if line.blocked:
                effort_style = f"bold {theme.TOKENS['warning']}"
            text.stylize(effort_style, start + effort_start, start + min(effort_end, end))
        if show_age and age_start is not None and age_start < end:
            text.stylize(theme.TOKENS["dim"], start + age_start, start + min(age_end, end))

    def _list_text_desk(self) -> Text:
        tab = self.tab
        if tab is None:
            return Text("No tabs. Check that the Base file exists at " + str(self.cfg.sprints_base))
        if not self.rows_on_screen:
            return Text(self._empty_text())
        summary_w, project_w, effort_w, show_age = self._columns()
        text = Text()
        text.append(self._heading_row(summary_w, project_w, effort_w, show_age),
                    style=f"bold {theme.TOKENS['chrome']}")
        for line in self._list_lines():
            text.append("\n")
            self._append_styled_line(text, line, summary_w, project_w, effort_w, show_age)
        return text

    def _empty_text(self) -> str:
        if self.search_text:
            return f"Nothing on this tab matches '{self.search_text}'. Press Esc to clear the search."
        if self.project_filter:
            return f"Nothing on this tab for {self.project_filter}. Press Esc to show every project."
        return "Nothing on this tab right now."

    def _detail_text(self, row: Optional[TaskRow]) -> str:
        if row is None:
            return "No task selected. Press Esc to go back."
        lines = [row.summary, ""]
        for label, value in (
            ("project", row.project), ("status", row.status), ("tier", row.tier),
            ("effort", row.complexity), ("context to proceed", row.est_context),
            ("added", row.created), ("picked up", row.picked), ("due", row.due),
        ):
            if value:
                lines.append(f"{label + ':':<20}{value}")
        source = strip_wikilink(row.source)
        if source:
            lines.append(f"{'source note:':<20}{source}")
            if not source_note_mod.is_url(source):
                resolved = source_note_mod.resolve(Path(self.cfg.vault), row.source)
                lines.append(f"{'source note file:':<20}{resolved if resolved else 'not found'}")
        lines.append(f"{'file:':<20}{row.id}.md")
        if row.note:
            lines += ["", "Note to Claude", row.note]
        if row.reply:
            lines += ["", "Claude's reply", row.reply]
        if row.project_assignment_log:
            lines += ["", "Routing"]
            lines += [f"  {entry}" for entry in row.project_assignment_log]
        if row.body:
            lines += ["", "Note body", row.body]
        mark = self._marks.get(row.id)
        if mark is not None:
            lines += ["", mark.label()]
        lines += ["", "to work this task: press w (claude) or x (choose)"]
        lines += ["Esc goes back. Editing happens in Obsidian: press o for the folder."]
        return "\n".join(lines)

    # -- actions ------------------------------------------------------

    def action_move(self, step: int) -> None:
        if self.view != "list" or not self.rows_on_screen:
            return
        self.cursor = max(0, min(self.cursor + step, len(self.rows_on_screen) - 1))
        self.redraw()

    def action_go_tab(self, key: str) -> None:
        for i, tab in enumerate(self.tabs):
            if tab.key == key:
                self.tab_index, self.cursor, self.view = i, 0, "list"
                self.redraw()
                return

    def _typing(self) -> bool:
        """True while the search box has the keyboard."""
        return getattr(self.app.focused, "id", None) == "search"

    def action_cycle_tab(self, step: int) -> None:
        if self.tabs and not self._typing():
            self.tab_index = (self.tab_index + step) % len(self.tabs)
            self.cursor, self.view = 0, "list"
            self.redraw()

    def action_next_tab(self) -> None:
        self.action_cycle_tab(1)

    def action_prev_tab(self) -> None:
        self.action_cycle_tab(-1)

    def action_open_detail(self) -> None:
        """`Enter`: Details, or -- when the toolbar itself has collapsed to one `Actions…` pill
        -- the row's full action list, exactly the same trade Textual's own docs
        make for a toolbar that has to fit 65 columns (S-UI section 5.4, "Actions… (Enter)")."""
        if self._toolbar_collapsed():
            return self.action_open_actions()
        self._open_detail()

    def _open_detail(self) -> None:
        if self.view == "list" and self.selected_row() is not None:
            self.view = "detail"
            self.redraw()

    # -- the row toolbar -----------------------------------

    def _toolbar_dispatch(self, action_name: str) -> None:
        """A toolbar button was pressed, or a `Pick` option was chosen -- both run through the
        same `action_<name>` method a key press would call."""
        if action_name == "__actions__":
            return self.action_open_actions()
        if action_name == "open_detail":
            return self._open_detail()
        method = getattr(self, f"action_{action_name}", None)
        if method is not None:
            method()

    def action_open_actions(self) -> None:
        row = self.selected_row()
        if row is None:
            return self._say("no task is highlighted, so there is nothing to work on")
        source_name = self._source.name if self._source is not None else ""
        options = [
            PickOption(a.key, a.action_name, a.label.rstrip("…"), disabled=not a.enabled)
            for a in queue_toolbar_actions(row, source_name)
        ]
        self.app.push_screen(Pick("actions for this task", options), self._dispatch_or_none)

    def _dispatch_or_none(self, action_name: Optional[str]) -> None:
        if action_name is not None:
            self._toolbar_dispatch(action_name)

    def action_keys(self) -> None:
        self.view = "keys" if self.view != "keys" else "list"
        self.redraw()

    def action_back(self) -> None:
        search = self.query_one("#search", Input)
        if search.display:
            search.display = False
            search.value = ""
            search.can_focus = False
            self.search_text = ""
            self.focus()
        elif self.view != "list":
            self.view = "list"
        elif self.project_filter:
            self.project_filter = None
        elif self._drill_stack:
            # `Esc` un-drills only once nothing else is left to back out of (search box,
            # detail view, a project filter) -- the same "back out one layer at a time" rule
            # every other `Esc` branch here already follows.
            return self.undrill()
        self.message = ""
        self._message_style = None
        self.redraw()

    # -- drill-down: swap the queue's source for one project's CHECKPOINT open threads ----

    def action_drill_row(self) -> None:
        """`d`: drill to the highlighted row's `project`. A row with no project (a support folder,
        or a source that never sets one) cannot be drilled -- say so rather than drilling nowhere."""
        row = self.selected_row()
        project = (row.project if row is not None else None) or None
        if not project:
            return self._say("this row has no project to drill into")
        self.drill(project)

    def action_undrill(self) -> None:
        self.undrill()

    def drill(self, project: str, source=None) -> None:
        """Swap the active source for `project`'s own CHECKPOINT.md Open threads. `source`
        is only for tests -- production always builds a fresh `checkpoint_source.CheckpointSource`
        per drill (the build package's own preference over a shared, retargetable instance)."""
        if source is None:
            from ..tasks import checkpoint_source as checkpoint_source_mod
            source = checkpoint_source_mod.make(self.cfg, project)
        self._drill_stack.append({
            "source": self._source,
            "tabs": self.tabs,
            "tab_index": self.tab_index,
            "cursor": self.cursor,
            "drilled_project": self.drilled_project,
            "project_filter": self.project_filter,
            "search_text": self.search_text,
        })
        self._source = source
        self.tabs = source.tabs()
        self.tab_index = 0
        self.cursor = 0
        self.view = "list"
        self.project_filter = None
        self.search_text = ""
        self.drilled_project = project
        self.message = source.notice
        self._message_style = None
        self.redraw()

    def undrill(self) -> bool:
        """Restore the exact source, tabs, tab index, cursor, project filter and search that were
        showing before the last `drill()` -- `False` when there is nothing to restore (already at
        the vault-wide view)."""
        if not self._drill_stack:
            return False
        prev = self._drill_stack.pop()
        self._source = prev["source"]
        self.tabs = prev["tabs"]
        self.tab_index = prev["tab_index"]
        self.cursor = prev["cursor"]
        self.drilled_project = prev["drilled_project"]
        self.project_filter = prev["project_filter"]
        self.search_text = prev["search_text"]
        self.view = "list"
        self.message = ""
        self._message_style = None
        self.redraw()
        return True

    def action_search(self) -> None:
        search = self.query_one("#search", Input)
        search.display = True
        search.can_focus = True
        self.view = "list"
        search.focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "search":
            self.search_text = event.value
            self.cursor = 0
            self.redraw()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "search":
            self.focus()

    def on_descendant_blur(self, event=None) -> None:
        """When the search box hands the keyboard back, the pane takes it, so the keys work."""
        search = self.query_one("#search", Input)
        if not search.has_focus and self.app.focused is None:
            self.focus()

    def action_project_filter(self) -> None:
        names = self.projects()
        if not names:
            self._say("No projects are set on these tasks.")
        elif self.project_filter is None:
            self.project_filter = names[0]
            self._say("")
        else:
            i = names.index(self.project_filter) if self.project_filter in names else -1
            self.project_filter = names[i + 1] if i + 1 < len(names) else None
        self.cursor = 0
        self.redraw()

    def action_refresh_now(self) -> None:
        if self._source is not None:
            try:
                self._source.refresh()
                self.last_read_at = datetime.now(timezone.utc)
                self._say("")
            except Exception as exc:
                log.exception("manual refresh failed")
                self._say(f"could not re-read your task list: {exc}", warn=True)
        self.redraw()

    def action_open_folder(self) -> None:
        """`o` / the toolbar's `Open in Obsidian`/`Open in editor`: asks the task's SOURCE how it
        wants to be edited. The standalone source has no Obsidian to hand off to, so
        it always suspends the deck into `$EDITOR`, at the desk and in tmux alike; the Obsidian
        source keeps its behaviour -- the note's own URI at the desk, or inside tmux on the
        phone a reusable shell window `cd`'d to the Sprints folder."""
        if self._source is not None and self._source.name == "standalone":
            return self._open_in_editor()
        if not self._toolbar_collapsed():
            return self._open_in_obsidian()
        self._open_sprints_shell()

    def _open_in_editor(self) -> None:
        """The standalone source's own edit path: suspend the deck (Textual hands the terminal
        fully back) and run `$EDITOR` (or `notepad` when unset) on the task's file, then redraw
        on return. Works the same at the desk and inside a tmux pane on the phone -- there is no
        separate app to hand off to the way Obsidian is, so one path covers both."""
        row = self.selected_row()
        if row is None or not row.path:
            return self._say("no file recorded for that task")
        editor = os.environ.get("EDITOR") or "notepad"
        try:
            with self.app.suspend():
                subprocess.run([editor, row.path])
        except SuspendNotSupported:
            return self._say(
                f"an editor cannot open from here; the file is {row.path}",
                warn=True,
            )
        except OSError as exc:
            log.exception("could not launch the editor")
            return self._say(f"could not open the editor: {exc}", warn=True)
        self._say(f"back from editing {row.id}")
        self.redraw()

    def _open_in_obsidian(self) -> None:
        row = self.selected_row()
        path = row.path if row is not None else None
        if not path:
            self._say("no file recorded for that task; opening the Sprints folder instead")
            return self._open_sprints_shell()
        uri = f"obsidian://open?path={quote(str(path))}"
        try:
            subprocess.run([self.cfg.tools.cmd, "/c", "start", "", uri], check=False)
        except OSError as exc:
            log.exception("could not launch Obsidian")
            return self._say(f"could not open Obsidian: {exc}", warn=True)
        self._say(f"asked Obsidian to open {row.id}")

    def action_source_note(self) -> None:
        """`O` / the toolbar's `Source note`: resolves the highlighted task's `source` wikilink to
        a real file in the vault (`pantheon/tasks/source_note.py`, S-UI section 5.4) and opens it
        the same way `Open in Obsidian` opens the task's own note -- the file's `obsidian://` URI
        at the desk, or a reusable shell window `cd`'d to its folder inside tmux. A URL source has
        nothing to resolve, so the status line just shows it; never writes anything."""
        row = self.selected_row()
        if row is None:
            return self._say("no task is highlighted, so there is no source note to open")
        title = strip_wikilink(row.source)
        if not title:
            return self._say("no source note recorded for that task")
        if source_note_mod.is_url(title):
            return self._say(f"that source is a link, not a note: {row.source}")
        path = source_note_mod.resolve(Path(self.cfg.vault), row.source)
        if path is None:
            return self._say(f'source note not found for "{title}"')
        if not self._toolbar_collapsed():
            return self._open_source_in_obsidian(path)
        self._open_source_shell(path)

    def _open_source_in_obsidian(self, path: Path) -> None:
        uri = f"obsidian://open?path={quote(str(path))}"
        try:
            subprocess.run([self.cfg.tools.cmd, "/c", "start", "", uri], check=False)
        except OSError as exc:
            log.exception("could not launch Obsidian")
            return self._say(f"could not open Obsidian: {exc}", warn=True)
        self._say(f"opened the source note {path.stem} in Obsidian")

    def _open_source_shell(self, path: Path) -> None:
        folder = str(path.parent)
        if not tmuxctl.in_tmux():
            self._say(f"not inside tmux; the source note is {path}")
            return
        index = tmuxctl.open_or_reuse_shell(self.cfg.tmux_session, folder)
        if index is None:
            self._say(f"could not open a tmux window; the source note is {path}", warn=True)
        else:
            self._say(f"shell window is now in {folder}")

    def _open_sprints_shell(self) -> None:
        """Open the Sprints folder in a new tmux window so the user can edit in Obsidian."""
        folder = str(self.cfg.sprints_dir)
        if self._source is not None:
            row = self.selected_row()
            if row is not None:
                folder = self._source.open_for_edit(row)
        if not tmuxctl.in_tmux():
            self._say(f"not inside tmux; the folder is {folder}")
            self.redraw()
            return
        index = tmuxctl.open_or_reuse_shell(self.cfg.tmux_session, folder)
        if index is None:
            self._say(f"could not open a tmux window; the folder is {folder}", warn=True)
        else:
            self._say(f"shell window is now in {folder}")
        self.redraw()

    def on_resize(self, event=None) -> None:
        self.redraw()

    # -- work this -----------------------------------------

    def action_work_this(self) -> None:
        """`w`: start the usual agent on the highlighted task, in that task's project folder."""
        self._start(self.cfg.default_provider, None)

    def action_choose_worker(self) -> None:
        """`x`: a `Pick` naming who should work it."""
        if self.selected_row() is None:
            return self._say("no task is highlighted, so there is nothing to work on")
        options = [
            PickOption(o.key, o.value, o.label,
                      disabled=(o.value == "ollama" and not self.cfg.providers.get("ollama", False)))
            for o in WHO_OPTIONS
        ]
        self.app.push_screen(Pick("who works it", options), self._who_picked)

    def _who_picked(self, choice: Optional[str]) -> None:
        if choice is None:
            return self._say("nothing was dispatched")
        provider_name, interactive = WHO_VALUES[choice]
        self._start(provider_name, interactive)

    def _start(self, provider_name: str, interactive: Optional[bool], confirmed: bool = False) -> None:
        row = self.selected_row()
        refusal = launch_mod.guard(row, self.cfg, self.agent_rows(), provider_name,
                                   self.providers.keys(), confirmed=confirmed)
        if refusal is not None:
            if refusal.needs_confirm:
                self.app.push_screen(
                    Confirm(refusal.reason, yes_label="Yes, start it (y)"),
                    functools.partial(self._work_confirmed, provider_name, interactive),
                )
            else:
                self._say(refusal.reason)
            return
        provider = self.providers[provider_name]
        self._say(f"starting {provider_name} in {row.project}...")
        # In a thread: the Claude launch waits on tmux for many seconds, and the deck must not
        # freeze while it does.
        self.run_worker(functools.partial(self._launch, row, provider, interactive), thread=True)

    def _work_confirmed(self, provider_name: str, interactive: Optional[bool], confirmed: Optional[bool]) -> None:
        if not confirmed:
            return self._say("left that task alone")
        self._start(provider_name, interactive, confirmed=True)

    def _launch(self, row: TaskRow, provider, interactive: Optional[bool]) -> None:
        try:
            result = launch_mod.dispatch(row, self.cfg, provider, interactive)
        except Exception:
            log.exception("dispatch failed")
            self.app.call_from_thread(
                self._done, None, f"could not start that agent; what went wrong is in {self.cfg.log_file}")
            return
        self.app.call_from_thread(self._done, result, "")

    def _done(self, result, fallback: str) -> None:
        if result is None:
            self._say(fallback, warn=True)
        else:
            self._say(result.message or ("started" if result.ok else "that agent did not start"),
                      warn=not result.ok)
        self.redraw()
