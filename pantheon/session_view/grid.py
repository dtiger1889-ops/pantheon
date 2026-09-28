"""`ConversationGrid` -- the pit, as a wall of conversations. So where THE PIT's table and its empty detail box
used to sit, the deck now lays a grid of `SessionTile`s -- one per running session, each drawing
that session's own transcript the way Claude Desktop draws a chat.

How many tiles fit is a width decision, not a preference: one column below 160 content columns,
two from 160, three from 240, and at most `MAX_ROWS` rows of them. Everything past that is not
dropped -- it is one arrow key away in the SESSIONS sidebar, which lists every session there is.

Two rules keep it from being "slow or laggy when several sessions are busy":

1. Only the tiles ON SCREEN are re-read. A session sitting below the fold, or hidden behind an
   expanded tile, costs nothing per tick.
2. The re-read happens on a worker THREAD (`run_worker(thread=True)`), and only the finished
   `Conversation` objects come back to the drawing thread. Parsing a transcript never blocks a
   key press.

Conversations are cached per session id, so a tick calls `transcript.update` -- which reads
only the bytes appended since last time -- instead of re-reading whole files.
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

from textual.app import ComposeResult
from textual.binding import Binding
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Static

from . import codex_transcript as codex_mod
from . import models as m
from . import transcript as transcript_mod
from .tile import SessionTile

log = logging.getLogger("pantheon.session_view.grid")

# Column tiers, measured against the GRID's own content width -- never the terminal's. The deck
# spends 30 columns on the sidebar before the grid sees any, and the tiers below are the widths
# at which a tile still has room for a chip line and a paragraph of prose side by side.
COLS_2_AT = 160
COLS_3_AT = 240
# Two rows of tiles is the whole grid: a third row makes every tile too short to show more than
# its header, which is the "table, not a chat" failure this build exists to end.
MAX_ROWS = 2

REFRESH_SECONDS = 5

NO_SESSIONS = "no sessions running — press n to start one"
NO_TRANSCRIPT = "no transcript yet"


def columns_for(width: int) -> int:
    """1 / 2 / 3 columns by content width."""
    if width >= COLS_3_AT:
        return 3
    if width >= COLS_2_AT:
        return 2
    return 1


def parser_for(entry: m.SessionEntry):
    """Which parser module reads this session's transcript -- Codex rollouts and Claude
    transcripts are different file formats behind the same `load`/`update` pair."""
    return codex_mod if (entry.provider or "claude").lower() == "codex" else transcript_mod


def read_conversation(entry: m.SessionEntry,
                      previous: Optional[m.Conversation] = None) -> Optional[m.Conversation]:
    """One session's transcript, incrementally when we have already read it once.

    Runs on a worker thread. A transcript that has been deleted, truncated, or half-written
    must never take the deck down, so every failure returns what we already had (or None) and
    the tile simply keeps showing the last good conversation.
    """
    path = entry.transcript_path
    if not path:
        return None
    module = parser_for(entry)
    try:
        if previous is not None and previous.path == path:
            return module.update(previous)
        return module.load(path)
    except Exception as exc:  # pragma: no cover - defensive; a bad file must not kill the deck
        log.debug("could not read %s: %r", path, exc)
        return previous


def order_entries(entries: list) -> list:
    """THE PIT's own order: whoever needs the user first, everything else left as it came (the
    supervisor pane already sorted those by who needs you, then by age)."""
    return sorted(entries, key=lambda e: 0 if e.needs_human else 1)


class NoTranscriptTile(Widget):
    """A live session we can see running but whose transcript we cannot find yet -- a Codex job
    before its rollout file appears, or a session started outside `~/.claude/projects`. One
    honest line beats an empty chat box that looks broken."""

    can_focus = True

    DEFAULT_CSS = """
    NoTranscriptTile {
        height: 3; width: 1fr; padding: 0 2;
        border: round #1D5B6E; border-title-color: #22D3EE;
        background: $surface;
    }
    NoTranscriptTile:focus { border: heavy $primary; }
    NoTranscriptTile .stub-title { width: 1fr; text-style: bold; color: $text; }
    NoTranscriptTile .stub-note { width: 1fr; color: $text-muted; }
    """

    def __init__(self, entry: m.SessionEntry, id: Optional[str] = None) -> None:
        super().__init__(id=id)
        self.entry = entry

    def compose(self) -> ComposeResult:
        yield Static(self.entry.title or self.entry.project or self.entry.session_id,
                     markup=False, classes="stub-title")
        yield Static(NO_TRANSCRIPT, markup=False, classes="stub-note")


class ConversationGrid(Widget):
    """The wall of tiles. `set_entries()` is what the deck calls; everything else follows."""

    # The wall itself takes focus when it holds no tile yet (nothing has started) -- otherwise
    # Tab would step into an empty panel and the focus would silently stay behind, and the
    # grid's own arrow / Enter / Esc keys would have nowhere to arrive.
    can_focus = True

    class Expanded(Message):
        """A tile filled the grid (Enter, or the sidebar activating a session)."""

        def __init__(self, entry: Optional[m.SessionEntry]) -> None:
            self.entry = entry
            super().__init__()

    DEFAULT_CSS = """
    ConversationGrid {
        layout: grid; grid-size: 1; grid-gutter: 0 0; height: 1fr; width: 1fr;
    }
    ConversationGrid:focus { border: heavy $primary; }
    ConversationGrid > #grid-empty {
        width: 1fr; height: 1fr; padding: 1 2; color: $text-muted;
        border: round #1D5B6E; border-title-color: #22D3EE;
    }
    """

    BINDINGS = [
        Binding("enter", "expand", "expand this session", show=False),
        Binding("escape", "collapse", "back to the grid", show=False),
        Binding("right", "focus_delta(1)", show=False),
        Binding("left", "focus_delta(-1)", show=False),
        Binding("down", "focus_row(1)", show=False),
        Binding("up", "focus_row(-1)", show=False),
    ]

    def __init__(
        self,
        entries_source: Optional[Callable[[], list]] = None,
        id: Optional[str] = "grid",
        refresh_seconds: int = REFRESH_SECONDS,
    ) -> None:
        super().__init__(id=id)
        # The deck hands in `lambda: self.rail.entries` -- the sidebar has already built the
        # `SessionEntry` list for this tick, and building it twice would mean scanning
        # `~/.claude/projects` twice on the drawing thread.
        self._entries_source = entries_source
        self._refresh_seconds = max(1, int(refresh_seconds))
        self.entries: list = []
        # session id -> the last Conversation we parsed for it (so a tick can `update()`).
        self._conversations: dict = {}
        # session id -> the mounted widget (SessionTile or NoTranscriptTile), in grid order.
        self._tiles: dict = {}
        self._laid_out: list = []
        # An expanded entry may be one the grid does not otherwise show (a RECENT transcript the
        # sidebar activated); it is kept here for as long as it fills the grid.
        self._expanded: Optional[m.SessionEntry] = None
        self._columns = 1
        self._empty: Optional[Static] = None
        self.border_title = "THE PIT · conversations"

    # ------------------------------------------------------------------ lifecycle

    def on_mount(self) -> None:
        self.set_interval(self._refresh_seconds, self.tick)
        self.pull()
        # The first `pull()` runs before this widget has been given a size (and, depending on the
        # Textual version, before `is_mounted` flips), so it can lay out nothing at all. `tick()`
        # after the first paint is what actually fills a one-column wall -- a wider one relays
        # itself out anyway when its column count changes -- and, more to the point, it is what
        # puts a CONVERSATION in each card. Without it the wall's first five seconds were a row
        # of empty boxes saying "no messages yet", which is the open, empty space this build
        # exists to end.
        self.call_after_refresh(self.tick)

    def on_resize(self) -> None:
        # Relayout when the width crossed a column tier, and also whenever there are sessions but
        # nothing on the wall: the column count on a narrow deck is 1 both before and after the
        # first real size arrives, so a "did the number change?" test alone left it empty.
        if columns_for(self.size.width) != self._columns or (self.entries and not self._laid_out):
            self.relayout()

    def subtitle(self) -> str:
        """The deck writes this into the panel's bottom border on its 5-second tick."""
        if self._expanded is not None:
            return "one session · Esc back to the wall"
        shown, total = len(self._laid_out), len(self.entries)
        word = "conversation" if shown == 1 else "conversations"
        more = f" of {total}" if total > shown else ""
        return f"{shown}{more} {word} · every {self._refresh_seconds}s"

    # ------------------------------------------------------------------ data in

    def pull(self) -> None:
        """Ask the source for the current session list and redraw the wall."""
        if self._entries_source is None:
            return
        try:
            entries = list(self._entries_source() or [])
        except Exception as exc:
            log.debug("entries source failed: %r", exc)
            return
        self.set_entries(entries)

    def set_entries(self, entries: list) -> None:
        """Live sessions, in any order; the grid sorts and lays them out itself."""
        self.entries = order_entries([e for e in entries if e.group == m.LIVE])
        self.relayout()

    # ------------------------------------------------------------------ layout

    def visible_entries(self) -> list:
        """The sessions whose transcripts a tick is allowed to re-read: the expanded one alone,
        or the tiles actually laid out. This IS the perf rule -- everything else costs nothing."""
        if self._expanded is not None:
            return [self._expanded]
        return list(self._laid_out)

    def _planned(self) -> tuple[int, list]:
        if self._expanded is not None:
            return 1, [self._expanded]
        columns = columns_for(self.size.width or 0)
        return columns, self.entries[: columns * MAX_ROWS]

    def relayout(self) -> None:
        """Mount / unmount tiles so the wall matches the current entries and width. Tiles that
        are already on screen keep their widget (and so their scroll position, their open
        chips, and anything half-typed in the message box)."""
        if not self.is_mounted:
            return
        columns, planned = self._planned()
        self._columns = columns
        self._laid_out = planned
        try:
            self.styles.grid_size_columns = max(1, columns)
        except Exception:  # pragma: no cover - a Textual version without the style
            pass

        planned_ids = [e.session_id for e in planned]
        for session_id in list(self._tiles):
            if session_id not in planned_ids:
                widget = self._tiles.pop(session_id)
                if widget.is_mounted:
                    widget.remove()

        if not planned:
            self._show_empty()
            self._refresh_subtitle()
            return
        self._hide_empty()

        # A last row that does not fill its columns leaves a tile-sized hole beside it, which is
        # exactly the empty space this wall replaced. The last card takes the rest of
        # the row instead, so every row is full whatever the session count is.
        remainder = len(planned) % columns if columns > 1 else 0
        span_last = columns - remainder + 1 if remainder else 1

        for index, entry in enumerate(planned):
            widget = self._tiles.get(entry.session_id)
            wants_tile = bool(entry.transcript_path)
            if widget is not None and isinstance(widget, SessionTile) != wants_tile:
                widget.remove()
                widget = None
                self._tiles.pop(entry.session_id, None)
            if widget is None:
                widget = self._make(entry)
                self._tiles[entry.session_id] = widget
                self.mount(widget)
            elif isinstance(widget, SessionTile):
                widget.set_entry(entry)
            else:
                widget.entry = entry
            if isinstance(widget, SessionTile):
                widget.expanded = self._expanded is not None
            try:
                widget.styles.column_span = span_last if index == len(planned) - 1 else 1
            except Exception:  # pragma: no cover - a Textual version without the style
                pass

        self._refresh_subtitle()

    def _refresh_subtitle(self) -> None:
        """The count in the panel's bottom border. The deck also writes this on its own tick, but
        that tick first fires before anything is laid out, so a wall that had just mounted three
        cards sat under a border still claiming two until five seconds later."""
        try:
            self.border_subtitle = self.subtitle()
        except Exception:  # pragma: no cover - decoration; never take the wall down for it
            pass

    def _make(self, entry: m.SessionEntry):
        if not entry.transcript_path:
            return NoTranscriptTile(entry)
        return SessionTile(entry, self._conversations.get(entry.session_id),
                           expanded=self._expanded is not None)

    def _show_empty(self) -> None:
        if self._empty is None or not self._empty.is_mounted:
            self._empty = Static(NO_SESSIONS, markup=False, id="grid-empty")
            self.mount(self._empty)
        try:
            self.styles.grid_size_columns = 1
        except Exception:  # pragma: no cover
            pass

    def _hide_empty(self) -> None:
        if self._empty is not None and self._empty.is_mounted:
            self._empty.remove()
        self._empty = None

    # ------------------------------------------------------------------ the 5-second tick

    def tick(self) -> None:
        """Re-read the visible transcripts, off the drawing thread."""
        if not self.display or not self.is_mounted:
            return
        self.pull()
        entries = self.visible_entries()
        if not entries:
            return
        previous = {e.session_id: self._conversations.get(e.session_id) for e in entries}
        self.run_worker(
            lambda: self._parse_then_apply(entries, previous),
            thread=True, exclusive=True, group="grid-parse",
        )

    def _parse_then_apply(self, entries: list, previous: dict) -> None:
        parsed = parse_entries(entries, previous)
        try:
            self.app.call_from_thread(self.apply_conversations, parsed)
        except Exception as exc:  # pragma: no cover - the app went away mid-parse
            log.debug("could not apply parsed conversations: %r", exc)

    def refresh_now(self) -> dict:
        """The same work a tick does, synchronously on this thread -- what the tests time and
        what a `--render` pass uses. Returns what it parsed, so a caller can count it."""
        entries = self.visible_entries()
        previous = {e.session_id: self._conversations.get(e.session_id) for e in entries}
        parsed = parse_entries(entries, previous)
        self.apply_conversations(parsed)
        return parsed

    def apply_conversations(self, parsed: dict) -> None:
        """Back on the drawing thread: hand each tile its newer conversation."""
        for session_id, conv in parsed.items():
            if conv is None:
                continue
            self._conversations[session_id] = conv
            widget = self._tiles.get(session_id)
            if isinstance(widget, SessionTile):
                widget.set_conversation(conv)

    # ------------------------------------------------------------------ focus

    def tiles(self) -> list:
        """The mounted tiles, in grid order."""
        return [self._tiles[e.session_id] for e in self._laid_out if e.session_id in self._tiles]

    def focus_session(self, session_id: str) -> bool:
        """The sidebar moved its cursor: put the focus on that session's tile, when it has one."""
        widget = self._tiles.get(session_id)
        if widget is None or not widget.is_mounted:
            return False
        widget.focus()
        return True

    def _focused_index(self) -> Optional[int]:
        tiles = self.tiles()
        focused = self.screen.focused if self.screen is not None else None
        for i, tile in enumerate(tiles):
            if focused is tile or (focused is not None
                                   and focused in list(tile.walk_children(with_self=True))):
                return i
        return None

    def _move_focus(self, step: int) -> None:
        tiles = self.tiles()
        if len(tiles) < 2:
            return
        current = self._focused_index()
        if current is None:
            tiles[0].focus()
            return
        tiles[(current + step) % len(tiles)].focus()

    def action_focus_delta(self, step: int) -> None:
        self._move_focus(step)

    def action_focus_row(self, step: int) -> None:
        self._move_focus(step * max(1, self._columns))

    def focus_first(self) -> None:
        tiles = self.tiles()
        if tiles:
            tiles[0].focus()
        elif self.display and self.is_mounted:
            self.focus()

    # ------------------------------------------------------------------ expand / collapse

    @property
    def expanded_entry(self) -> Optional[m.SessionEntry]:
        return self._expanded

    def action_expand(self) -> None:
        if self._expanded is not None:
            return
        index = self._focused_index()
        tiles = self.tiles()
        if index is None or not tiles:
            return
        self.expand_entry(self._laid_out[index])

    def expand_entry(self, entry: Optional[m.SessionEntry]) -> None:
        """Fill the grid with one session. The entry does not have to be one of the live tiles:
        the sidebar can activate a finished transcript, and it opens here read-only."""
        if entry is None:
            return
        self._expanded = entry
        self.relayout()
        widget = self._tiles.get(entry.session_id)
        if widget is not None and widget.is_mounted:
            widget.focus()
        self.refresh_now()
        self.post_message(self.Expanded(entry))

    def action_collapse(self) -> None:
        self.collapse()

    def collapse(self) -> None:
        if self._expanded is None:
            return
        was = self._expanded
        self._expanded = None
        self.relayout()
        if not self.focus_session(was.session_id):
            self.focus_first()
        self.post_message(self.Expanded(None))


def parse_entries(entries: list, previous: Optional[dict] = None) -> dict:
    """Read each entry's transcript. Pure and thread-safe: no widget is touched, so this is the
    half of a tick that runs off the drawing thread (and the half the perf test times)."""
    previous = previous or {}
    out: dict = {}
    for entry in entries:
        conv = read_conversation(entry, previous.get(entry.session_id))
        if conv is not None:
            out[entry.session_id] = conv
    return out


__all__ = [
    "ConversationGrid",
    "NoTranscriptTile",
    "columns_for",
    "order_entries",
    "parse_entries",
    "parser_for",
    "read_conversation",
    "COLS_2_AT",
    "COLS_3_AT",
    "MAX_ROWS",
    "REFRESH_SECONDS",
    "NO_SESSIONS",
    "NO_TRANSCRIPT",
]
