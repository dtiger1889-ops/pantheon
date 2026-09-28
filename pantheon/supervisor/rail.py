"""The SESSIONS sidebar, Orca-shaped.

One list, top to bottom:

    ＋ New session                       -> the new-session picker
    ◀ back · agent keeps running         -> only while a session (or the editor) is beside the deck
    ✎ Edit a file                        -> a text editor beside the list
    ◆ Assistant  idle                    -> the pinned Assistant; never listed again below
    loom-os · 2 running                  -> a project heading (click: the project menu)
      1 ● working  fix the launcher      -> a running session (click: its real terminal)
      · 2h  earlier conversation         -> a finished one (click: resume it)
      …4 more                            -> the rest of that project's finished ones
    MORE PROJECTS
      career                             -> projects with no recent conversation

One heading per project, never repeated. Projects with running sessions come first, then by most recent activity.

The widget only posts messages; the deck decides what a click does (stage, resume, menu).
`j` and `1`-`9` are unchanged in spirit: `1`-`9` choose that running session (same as a click),
`j` jumps to its own full-screen tmux window.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Optional

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.message import Message
from textual.widget import Widget
from textual.widgets import ListItem, ListView, Static

from .. import assistant as assistant_mod
from .. import config as config_mod
from .. import eventcache as eventcache_mod
from .. import events as events_mod
from .. import glyphs as glyphs_mod
from .. import theme as theme_mod
from .. import tmuxctl
from ..models import AgentState, TmuxWindow, parse_ts
from ..session_view import models as session_models
from ..session_view import recent as recent_mod
from ..session_view import summary as summary_mod
from ..session_view.models import SessionEntry
from . import state as state_mod

# 1-9 choose that running session straight away (a tenth+ is still reachable by arrow/click).
MAX_NUMBERED = 9
# Finished conversations shown under each project before `…N more`.
RECENT_PER_PROJECT = 3

NO_AGENTS = "no agents running"
NEW_SESSION_LABEL = "＋ New session"
HOME_LABEL = "◀ back · agent keeps running"
HOME_EDITOR_LABEL = "◀ back · editor keeps your text"
EDIT_LABEL = "✎ Edit a file"
ASSISTANT_LABEL = "◆ Assistant"
MORE_PROJECTS_LABEL = "MORE PROJECTS"
ON_SCREEN = "on screen"


def _norm(path: Optional[str]) -> str:
    return str(path or "").replace("\\", "/").rstrip("/")


def _age(ts, now: Optional[datetime] = None) -> str:
    """`4m`, `3h`, `2d` -- short enough to sit in front of a title. Accepts an ISO string or a
    datetime (the session-data build's `last_push_at` may be either)."""
    when = ts if isinstance(ts, datetime) else parse_ts(str(ts)) if ts else None
    if when is None:
        return ""
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    seconds = ((now or datetime.now(timezone.utc)) - when).total_seconds()
    if seconds < 3600:
        return f"{max(1, int(seconds // 60))}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


def _git_age(entry: SessionEntry) -> str:
    """`pushed 4m` / `committed 12m` when the entry carries those fields (added on another
    branch); nothing when it does not."""
    pushed = getattr(entry, "last_push_at", None)
    if pushed:
        return f"pushed {_age(pushed)}"
    committed = getattr(entry, "last_commit_at", None)
    if committed:
        return f"committed {_age(committed)}"
    return ""


class SessionSidebar(Widget):
    """Projects with their sessions nested under them."""

    class Selected(Message):
        """The highlighted session row changed (cursor move, not necessarily Enter)."""

        def __init__(self, entry: SessionEntry) -> None:
            self.entry = entry
            super().__init__()

    class Activated(Message):
        """Enter (or a mouse click) chose a session row."""

        def __init__(self, entry: SessionEntry) -> None:
            self.entry = entry
            super().__init__()

    class NewSession(Message):
        """`＋ New session` was chosen."""

    class ProjectChosen(Message):
        """A project heading (or a MORE PROJECTS name) was chosen."""

        def __init__(self, name: str, path: str) -> None:
            self.name = name
            self.path = path
            super().__init__()

    class AssistantChosen(Message):
        """`◆ Assistant` was chosen: put it on screen, starting it first if needed."""

    class AssistantReconnect(Message):
        """`↻ reconnect the phone link` (or `L`): the Assistant's Remote Control link is down."""

    class Home(Message):
        """`◀ back` was chosen: put the staged session back in its own window."""

    class EditFile(Message):
        """`✎ Edit a file` or `e`: open a file in the editor beside the list. `folder` is the
        highlighted session's (or project's) folder -- where a typed file name is looked for."""

        def __init__(self, folder: str) -> None:
            self.folder = folder
            super().__init__()

    DEFAULT_CSS = """
    SessionSidebar { layout: vertical; height: 1fr; width: 34; }
    SessionSidebar #sidebar-list { height: 1fr; }
    /* height: 1 pins every row to a single line so a long title can never wrap the sidebar into
       a wall of text; the Static is width-bounded + ellipsis so a clipped title ends in `…`
       instead of a hard cut. */
    SessionSidebar #sidebar-list > ListItem { padding: 0 1; height: 1; }
    SessionSidebar #sidebar-list > ListItem.two-line { height: 2; }
    SessionSidebar #sidebar-list Static { width: 1fr; text-wrap: nowrap; text-overflow: ellipsis; }
    SessionSidebar #sidebar-list > ListItem.-highlight { background: $primary-muted; text-style: bold; }
    SessionSidebar #sidebar-status { height: 1; padding: 0 1; color: $text-muted; }
    """

    class Approval(Message):
        """`a` / `d` / `v` on a running session's row: the deck hands
        it to the supervisor pane, which owns the answering. `width` is how much of the request
        line this row showed -- a cut line opens the full view before anything is allowed."""

        def __init__(self, session_id: str, key: str, width: int) -> None:
            self.session_id = session_id
            self.key = key
            self.width = width
            super().__init__()

    BINDINGS = [
        Binding("j", "jump", "jump", priority=True),
        Binding("e", "edit_file", "edit a file", show=False),
        Binding("a", "approval('a')", "allow once", show=False),
        Binding("d", "approval('d')", "deny", show=False),
        Binding("v", "approval('v')", "see all of it", show=False),
        *[Binding(str(n), f"jump_number({n})", show=False) for n in range(1, MAX_NUMBERED + 1)],
    ]

    def __init__(
        self,
        cfg: Optional[config_mod.Config] = None,
        rows_source: Optional[Callable[[], list[AgentState]]] = None,
        window_source: Optional[Callable[[], list[TmuxWindow]]] = None,
        id: Optional[str] = "rail",
        entries_source: Optional[Callable[[list[AgentState]], list[SessionEntry]]] = None,
        projects_source: Optional[Callable[[], list]] = None,
        approvals_source: Optional[Callable[[], dict]] = None,
        assistant_source: Optional[Callable[[], object]] = None,
    ) -> None:
        super().__init__(id=id)
        # session_id -> `supervisor.approvals.Shown`: the open permission prompt of each running
        # session, owned by the supervisor pane (the deck hands in `lambda: sup.approvals.shown`).
        self._approvals_source = approvals_source or (lambda: {})
        self.cfg = cfg or config_mod.load()
        # `rows_source`, when given (the deck hands in `lambda: self.sup.rows`), reuses the
        # supervisor pane's own already-folded rows instead of re-parsing the event log and
        # re-polling tmux a second time on the same tick (perf sweep pattern).
        self._rows_source = rows_source
        self._window_source = window_source or (lambda: tmuxctl.list_windows_cached(None))
        # Injectable so a test can hand in fixed `SessionEntry` rows without touching disk
        # (`recent.py` reads real transcript files by default).
        self._entries_source = entries_source or (lambda rows: recent_mod.entries(rows, self.cfg))
        # Every project the `n` picker knows (`dispatch/projects.py`, most recent first). The
        # deck hands the real one in; a bare sidebar lists none rather than scan the disk.
        self._projects_source = projects_source or (lambda: [])
        # the pinned Assistant's line (`pantheon/assistant.py` `view`); None = no line.
        self._assistant_source = assistant_source or (lambda: None)
        appearance = self.cfg.appearance_settings()
        self.glyphs = glyphs_mod.icon_table(appearance.glyphs, appearance.icons)
        self._ascii = (appearance.glyphs or "").lower() == "ascii"
        self.rows: list[AgentState] = []
        self.entries: list[SessionEntry] = []
        # Set by the deck: whether a session is on screen beside it, and which one.
        self.staged = False
        self.staged_session_id = ""
        self.staged_pane_id = ""
        self.editor_open = False              # the editor pane is beside the list
        # The folder of the last session or project row the cursor was on: `✎ Edit a file` is its
        # own row, so by the time it is clicked the cursor has left the session it is about.
        self.context_folder = ""
        self._expanded: set[str] = set()      # project keys whose `…N more` was opened
        # Parallel to the `ListView`'s children: one spec per row, and the `SessionEntry` for a
        # session row (None for every other kind -- `selected()` reads this one).
        self._items: list[tuple] = []
        self._item_entries: list[Optional[SessionEntry]] = []
        # The visible content of the last drawn list. `redraw` compares against it and skips the
        # full clear+rebuild when nothing on screen changed, so the sidebar stops flashing on every
        # tick.
        self._last_signature: Optional[tuple] = None
        self._built = False
        self.border_title = "SESSIONS"

    def compose(self) -> ComposeResult:
        yield ListView(id="sidebar-list")
        yield Static("", id="sidebar-status")

    def on_resize(self, event) -> None:
        # A permission request's line is cut to the sidebar's width (`_approval_width`), which
        # changes when a session is staged beside the list; redraw only if that changed the rows.
        if self._built:
            self.redraw()

    def on_mount(self) -> None:
        self.refresh_rows()
        if self._rows_source is None:
            self.set_interval(max(1, int(self.cfg.refresh_seconds or 5)), self.refresh_rows)

    # ------------------------------------------------------------------ data

    def refresh_rows(self) -> None:
        if self._rows_source is not None:
            self.rows = list(self._rows_source() or [])
        else:
            events_mod.rotate_if_large(self.cfg.events_file)
            evs, _errors = eventcache_mod.read_events_cached(self.cfg.events_file)
            windows = list(self._window_source() or [])
            self.rows = state_mod.fold(
                evs, windows, datetime.now(timezone.utc), self.cfg.tmux_session, self.cfg.projects_root,
                reserved_panes=assistant_mod.reserved_panes(self.cfg),
            )
        self.entries = list(self._entries_source(self.rows) or [])
        self.redraw()

    def set_stage(self, staged: bool, session_id: str = "", pane_id: str = "",
                  editor_open: bool = False) -> None:
        """The deck tells the sidebar what is on screen beside it; redraws only on a change."""
        if (staged, session_id, pane_id, editor_open) == (
                self.staged, self.staged_session_id, self.staged_pane_id, self.editor_open):
            return
        self.staged, self.staged_session_id, self.staged_pane_id = staged, session_id, pane_id
        self.editor_open = editor_open
        if self.is_mounted:
            self.redraw()

    def is_on_stage(self, entry: SessionEntry) -> bool:
        if not self.staged:
            return False
        if self.staged_session_id and entry.session_id == self.staged_session_id:
            return True
        if self.staged_pane_id:
            for row in self.rows:
                if row.session_id == entry.session_id and row.tmux_pane == self.staged_pane_id:
                    return True
        return False

    def _projects(self) -> list:
        try:
            return list(self._projects_source() or [])
        except Exception:
            return []

    def _assistant(self):
        try:
            return self._assistant_source()
        except Exception:
            return None

    def _groups(self, hide: frozenset = frozenset()) -> list[dict]:
        """One group per project (case-insensitive name), running ones first in the order the
        supervisor already sorted them, then by newest finished one.
        `hide`: session ids with a line of their own above."""
        known = {p.name.lower(): p.path for p in self._projects()}
        groups: dict[str, dict] = {}
        order_live: list[str] = []
        order_recent: list[str] = []
        for entry in self.entries:
            if entry.session_id in hide:
                continue
            name = entry.project or "-"
            key = name.lower()
            group = groups.get(key)
            if group is None:
                group = groups[key] = {"key": key, "name": name,
                                       "path": known.get(key) or _norm(entry.cwd), "live": [], "recent": []}
            if entry.group == session_models.LIVE:
                group["live"].append(entry)
                if key not in order_live:
                    order_live.append(key)
            else:
                group["recent"].append(entry)
                if key not in order_recent:
                    order_recent.append(key)
        ordered = order_live + [k for k in order_recent if k not in order_live]
        return [groups[k] for k in ordered]

    def _plan(self) -> list[tuple]:
        """The rows to draw, in order, as specs: `("new",)`, `("home",)`, `("project", name,
        path, live_count)`, `("session", entry, number_or_None)`, `("more", key, n)`,
        `("label", text)`. Kept apart from the widgets so the redraw signature is cheap."""
        plan: list[tuple] = [("new",)]
        if self.staged or self.editor_open:
            plan.append(("home",))
        plan.append(("edit",))
        assistant = self._assistant()
        if assistant is not None:
            plan.append(("assistant", assistant))
            if getattr(assistant, "link_lost", False):
                plan.append(("reconnect",))
        number = 0
        shown: set[str] = set()
        for group in self._groups(getattr(assistant, "hide", frozenset())):
            shown.add(group["key"])
            plan.append(("project", group["name"], group["path"], len(group["live"])))
            for entry in group["live"]:
                number += 1
                plan.append(("session", entry, number if number <= MAX_NUMBERED else None))
            recent = group["recent"]
            limit = len(recent) if group["key"] in self._expanded else RECENT_PER_PROJECT
            for entry in recent[:limit]:
                plan.append(("session", entry, None))
            if len(recent) > limit:
                plan.append(("more", group["key"], len(recent) - limit))
        others = [p for p in self._projects() if p.name.lower() not in shown]
        if others:
            plan.append(("label", MORE_PROJECTS_LABEL))
            for project in others:
                plan.append(("project", project.name, project.path, 0))
        return plan

    def _row_text(self, spec: tuple) -> Text:
        kind = spec[0]
        chrome, dim = theme_mod.TOKENS["chrome"], theme_mod.TOKENS["dim"]
        if kind == "new":
            return self._line(NEW_SESSION_LABEL, style=f"bold {chrome}")
        if kind == "edit":
            return self._line(EDIT_LABEL, style=f"bold {chrome}")
        if kind == "home":
            label = HOME_LABEL if self.staged else HOME_EDITOR_LABEL
            return self._line(label, style=f"bold {theme_mod.TOKENS.get('accent', chrome)}")
        if kind == "assistant":
            view = spec[1]
            if self.staged and view.pane_id and view.pane_id == self.staged_pane_id:
                accent = theme_mod.TOKENS.get("accent", chrome)
                return self._line(f"{ASSISTANT_LABEL}  {ON_SCREEN}", style=f"bold {accent}")
            text = self._line(ASSISTANT_LABEL, style=f"bold {chrome}")
            colour = theme_mod.TOKENS.get(view.style, dim)
            text.append(f"  {view.status}", style=f"bold {colour}" if view.style == "warning" else colour)
            name = self._assistant_name(view)
            if name:
                # Its own dim line under the label, like a running session's second line: the
                # 38-column sidebar has no room for a name beside `phone link off`.
                text.append(f"\n  {name}", style=dim)
            return text
        if kind == "reconnect":
            return self._line(f"  {assistant_mod.RECONNECT_LABEL}",
                              style=theme_mod.TOKENS.get("warning", chrome))
        if kind == "label":
            return self._line(spec[1], style=f"bold {chrome}")
        if kind == "more":
            return self._line(f"    …{spec[2]} more", style=dim)
        if kind == "project":
            text = self._line(spec[1], style="bold")
            if spec[3]:
                text.append(f" · {spec[3]} running", style=dim)
            return text
        entry, number = spec[1], spec[2]
        title = entry.title or entry.project or entry.session_id[:8]
        git = _git_age(entry)
        if entry.group == session_models.LIVE:
            # Title first. A running session gets a
            # second, dim line for what it is doing and when it last pushed.
            if self.is_on_stage(entry):
                text = self._line(f"  ▶ {title}", style=f"bold {theme_mod.TOKENS.get('accent', chrome)}")
                meta = [ON_SCREEN]
            else:
                glyph = self.glyphs.get(self._glyph_key_for(entry), "●")
                num = str(number) if number is not None else " "
                text = self._line(f"  {num} {glyph} ")
                if entry.style:
                    colour = theme_mod.TOKENS.get(entry.style, "")
                    text.stylize(f"bold {colour}" if entry.style == "error" else colour)
                text.append(title, style="bold")
                meta = [summary_mod.status_word(entry)]
            if git:
                meta.append(git)
            shown = self._shown(entry)
            if shown is not None:
                # The request itself as the second line, cut only at the end with `… +N`.
                req_line, _cut = shown.line(self._approval_width(), self._ascii)
                colour = theme_mod.TOKENS["error" if shown.summary.warning else "warning"]
                text.append("\n      " + req_line,
                            style=f"bold {colour}" if shown.summary.warning else colour)
                return text
            text.append("\n      " + " · ".join(m for m in meta if m), style=dim)
            return text
        age = _age(entry.modified_ts)
        text = self._line(f"  · {title}", style=dim)
        tail = "  ".join(x for x in (age, git) if x)
        if tail:
            text.append(f"  {tail}", style=dim)
        return text

    def _assistant_name(self, view) -> str:
        """The Assistant conversation's own name, for the line under the label, when it has one on
        disk and it is not simply the default name (`[assistant] name`); cut to the width."""
        name = (getattr(view, "name", "") or "").strip()
        if not name or name.lower() == (self.cfg.assistant_settings().name or "").lower():
            return ""
        try:
            width = self.size.width or 38
        except Exception:
            width = 38
        room = max(8, width - 4)
        return name if len(name) <= room else name[:room - 1] + "…"

    def _signature(self, plan: list[tuple], texts: list[Text]) -> tuple:
        """Everything the plan actually draws -- so two ticks with the same visible content
        compare equal and the second one skips the rebuild that causes the flash."""
        sig = []
        for spec, text in zip(plan, texts):
            ident = spec[1].session_id if spec[0] == "session" else spec[1:2]
            sig.append((spec[0], ident, text.plain, str(text.style), tuple(str(s.style) for s in text.spans)))
        return tuple(sig)

    def redraw(self) -> None:
        list_view = self.query_one("#sidebar-list", ListView)
        plan = self._plan()
        texts = [self._row_text(spec) for spec in plan]
        live_count = sum(1 for e in self.entries if e.group == session_models.LIVE)
        signature = self._signature(plan, texts)

        if self._built and signature == self._last_signature:
            # Nothing on screen changed since the last tick -- do NOT clear+rebuild the ListView
            # (that empty-then-refill is the blink). Just keep the status line honest.
            self._update_status(live_count)
            return
        self._last_signature = signature
        self._built = True

        keep = self._current_key(list_view)
        list_view.clear()
        self._items = list(plan)
        self._item_entries = [spec[1] if spec[0] == "session" else None for spec in plan]
        for text in texts:
            list_view.append(ListItem(Static(text), classes="two-line" if "\n" in text.plain else ""))
        self._restore_cursor(list_view, keep)
        self._update_status(live_count)

    @staticmethod
    def _key_of(spec: tuple) -> tuple:
        if spec[0] in ("assistant", "reconnect"):
            return (spec[0],)
        if spec[0] == "session":
            return ("session", spec[1].session_id)
        return spec[:2]

    def _current_key(self, list_view: ListView) -> Optional[tuple]:
        idx = list_view.index
        if idx is None or not (0 <= idx < len(self._items)):
            return None
        return self._key_of(self._items[idx])

    def _restore_cursor(self, list_view: ListView, keep: Optional[tuple]) -> None:
        if keep is not None:
            idx = next((i for i, s in enumerate(self._items) if self._key_of(s) == keep), None)
            if idx is not None:
                list_view.index = idx
                return
        # First choice: the first session row; else the first thing that can be chosen.
        idx = next((i for i, e in enumerate(self._item_entries) if e is not None), None)
        if idx is None:
            idx = next((i for i, s in enumerate(self._items) if s[0] != "label"), None)
        if idx is not None:
            list_view.index = idx

    def _update_status(self, live_count: int) -> None:
        status = self.query_one("#sidebar-status", Static)
        status.update(NO_AGENTS if not live_count else f"{live_count} running")

    # ------------------------------------------------------------------ rows

    def _glyph_key_for(self, entry: SessionEntry) -> str:
        if entry.needs_human:
            return "attention"
        if entry.style == "error":
            return "fail"
        if entry.style == "muted":
            return "idle"
        return "working"

    def _line(self, plain: str, style: str = "") -> Text:
        """One-line, clip-with-ellipsis Text -- no row may ever wrap the sidebar."""
        return Text(plain, style=style, no_wrap=True, overflow="ellipsis")

    # ------------------------------------------------------------------ selection

    def selected(self) -> Optional[SessionEntry]:
        list_view = self.query_one("#sidebar-list", ListView)
        idx = list_view.index
        if idx is None or not (0 <= idx < len(self._item_entries)):
            return None
        return self._item_entries[idx]

    def focus_table(self) -> None:
        """Kept for parity with the old rail's helper name -- puts the cursor in the list."""
        self.query_one("#sidebar-list", ListView).focus()

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        idx = event.list_view.index
        if idx is None or not (0 <= idx < len(self._items)):
            return
        if self._items[idx][0] == "label":
            self._skip_label(event.list_view, idx)
            return
        folder = self._folder_of(self._items[idx])
        if folder:
            self.context_folder = folder
        entry = self._item_entries[idx]
        if entry is not None:
            self.post_message(self.Selected(entry))

    def _skip_label(self, list_view: ListView, from_idx: int) -> None:
        """A section label is not choosable -- move to the nearest row below it, else above."""
        n = len(self._items)
        for i in list(range(from_idx + 1, n)) + list(range(from_idx - 1, -1, -1)):
            if self._items[i][0] != "label":
                list_view.index = i
                return

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        event.stop()
        idx = event.list_view.index
        if idx is None or not (0 <= idx < len(self._items)):
            return
        self._choose(self._items[idx])

    def _choose(self, spec: tuple) -> None:
        kind = spec[0]
        if kind == "session":
            self.post_message(self.Activated(spec[1]))
        elif kind == "new":
            self.post_message(self.NewSession())
        elif kind == "home":
            self.post_message(self.Home())
        elif kind == "edit":
            self.post_message(self.EditFile(self.context_folder))
        elif kind == "assistant":
            self.post_message(self.AssistantChosen())
        elif kind == "reconnect":
            self.post_message(self.AssistantReconnect())
        elif kind == "project":
            self.post_message(self.ProjectChosen(spec[1], spec[2]))
        elif kind == "more":
            self._expanded.add(spec[1])
            self.redraw()

    @staticmethod
    def _folder_of(spec: tuple) -> str:
        if spec[0] == "session":
            return _norm(spec[1].cwd)
        if spec[0] == "project":
            return _norm(spec[2])
        return ""

    # ------------------------------------------------------------------ keys

    def action_edit_file(self) -> None:
        """`e`: the editor, for the highlighted session's folder (or the last one highlighted)."""
        list_view = self.query_one("#sidebar-list", ListView)
        idx = list_view.index
        folder = ""
        if idx is not None and 0 <= idx < len(self._items):
            folder = self._folder_of(self._items[idx])
        self.post_message(self.EditFile(folder or self.context_folder))

    def _shown(self, entry: SessionEntry):
        try:
            return (self._approvals_source() or {}).get(entry.session_id)
        except Exception:
            return None

    def _approval_width(self) -> int:
        """Columns the second line has: the row's width less its padding and 6-column indent."""
        content = self.content_size.width or 30      # inside the sidebar's own border
        return max(12, content - 2 - 6)              # less the row's padding and the indent

    def action_approval(self, key: str) -> None:
        """`a` allow once / `d` deny / `v` see all of it, for the highlighted running session."""
        entry = self.selected()
        if entry is None or entry.group != session_models.LIVE:
            return
        self.post_message(self.Approval(entry.session_id, key, self._approval_width()))

    def action_jump(self) -> None:
        """`j`: the running session's own full-screen tmux window (not the stage)."""
        entry = self.selected()
        if entry is None or entry.group != session_models.LIVE or entry.window_index is None:
            return
        tmuxctl.select_window(self.cfg.tmux_session, entry.window_index, tmux=self.cfg.tools.tmux)

    def action_jump_number(self, n: int) -> None:
        """`1`-`9`: choose that running session, exactly as a click on it would."""
        numbered = [i for i, s in enumerate(self._items) if s[0] == "session" and s[2] == n]
        if not numbered:
            return
        list_view = self.query_one("#sidebar-list", ListView)
        list_view.index = numbered[0]
        self._choose(self._items[numbered[0]])


# Kept so anything importing the old name keeps working.
SwitcherRail = SessionSidebar
