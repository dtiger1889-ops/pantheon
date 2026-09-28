"""The prompt scratchpad: F4 window / `pantheon --notes`.

Tabbed plain-text drafts that autosave on a 1-second idle debounce (flushed immediately on
tab-switch, on the terminal losing focus, and on quit) so nothing typed here is ever lost and there
is nothing to save by hand. A tab IS a file in `state/notes/` (`notes/store.py`); this
module is only the Textual widget wrapped around that store, plus the "insert this draft into the
selected agent's tmux pane" button and the `/slash` completion picker.

Nothing here ever sends a draft anywhere on its own. `action_insert` types the draft into the
target window with `tmux send-keys -l` and presses no key that would submit it -- an accidental
send is the risk this avoids, so sending stays manual.
"""
from __future__ import annotations

import logging
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from rich.text import Text
from textual import events as tevents
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widget import Widget
from textual.widgets import Button, Static, TextArea

from .. import config as config_mod
from .. import eventcache as eventcache_mod
from .. import orphan as orphan_mod
from .. import theme as theme_mod
from .. import tmuxctl
from ..models import AgentState, TmuxWindow, utcnow_iso
from ..supervisor import state as state_mod
from ..widgets.footer import PhoneFooter, WindowBar, fit_footer, make_footer
from ..widgets.modal import Confirm, Pick, TextPrompt, numbered
from . import complete as complete_mod
from . import store

log = logging.getLogger("pantheon.notes")

DEBOUNCE_SECONDS = 1.0   # module-level so a test can shrink it
NEW_TAB_TITLE = "untitled"

NO_TABS_MESSAGE = "no drafts yet - press New tab (Ctrl+T) to start one"
NO_AGENT_MESSAGE = "no agent selected - jump to one first"


# --------------------------------------------------------------------------- picking a target


def selectable_target(
    cfg: config_mod.Config,
    window_source: Optional[Callable[[], list[TmuxWindow]]] = None,
) -> Optional[AgentState]:
    """The agent `Insert` types into: the top row of the same "needs you first" fold the
    supervisor sorts by (`pantheon.supervisor.state.fold`), restricted to a row with a live tmux
    window in THIS session."""
    windows = list((window_source or (lambda: tmuxctl.list_windows_cached(None)))() or [])
    evs, _errors = eventcache_mod.read_events_cached(cfg.events_file)
    rows = state_mod.fold(evs, windows, datetime.now(timezone.utc), cfg.tmux_session, cfg.projects_root)
    for row in rows:
        if row.window_index is not None and row.in_pantheon:
            return row
    return None


def insert_target(row: AgentState, cfg: config_mod.Config) -> str:
    return f"{row.tmux_session or cfg.tmux_session}:{row.window_index}"


def insert_into_agent(target: str, text: str, tmux: Optional[str] = None) -> bool:
    """`tmux send-keys -l` the draft into `target`, one line at a time, with a literal newline
    (never the Enter key) between lines. No call here ever sends Enter -- the user presses send himself."""
    lines = text.split("\n")
    ok = True
    for i, line in enumerate(lines):
        if line:
            ok = tmuxctl.run("send-keys", "-t", target, "-l", "--", line, tmux=tmux).returncode == 0 and ok
        if i < len(lines) - 1:
            ok = tmuxctl.run("send-keys", "-t", target, "-l", "--", "\n", tmux=tmux).returncode == 0 and ok
    return ok


class TabBar(Static):
    """One line of tab pills, click-to-switch, the focused one lit. A plain `Static` with hand-rolled hit-testing, the same shape as this codebase's
    other hand-built status lines (`WindowBar`, the supervisor's header) -- Textual's own `Tabs`
    widget mounts and highlights its children asynchronously, which fought this pane's synchronous
    "switch tabs, flush the old one, load the new one" flow more than it helped."""

    DEFAULT_CSS = "TabBar { height: 1; background: $panel; padding: 0 1; }"

    def __init__(self, on_pick, id: Optional[str] = "tabs") -> None:
        super().__init__(id=id, markup=False)
        self._on_pick = on_pick
        self._ranges: list[tuple[int, int, str]] = []

    def set_tabs(self, tabs: list[str], focused: Optional[str]) -> None:
        chrome = theme_mod.TOKENS.get("chrome", "")
        line = Text()
        ranges: list[tuple[int, int, str]] = []
        for slug in tabs:
            start = len(line)
            label = f" {slug} "
            if slug == focused:
                line.append(label, style=f"bold black on {chrome}")
            else:
                line.append(label)
            ranges.append((start, start + len(label), slug))
            line.append(" ")
        self._ranges = ranges
        self.update(line)

    def on_click(self, event: tevents.Click) -> None:
        for start, end, slug in self._ranges:
            if start <= event.x < end:
                self._on_pick(slug)
                return


class Editor(TextArea):
    """`TextArea`, plus a direct call into the pane on blur -- `events.Blur` does not bubble, so a
    plain `on_blur` on `NotesPane` would never fire for its child losing focus."""

    def __init__(self, pane: "NotesPane", **kwargs) -> None:
        super().__init__(**kwargs)
        self._pane = pane

    def _on_blur(self, event: tevents.Blur) -> None:
        super()._on_blur(event)
        self._pane._on_editor_blur()


# --------------------------------------------------------------------------- the pane


class NotesPane(Widget):
    """The scratchpad: a tab bar, one big `TextArea`, and a row of buttons."""

    DEFAULT_CSS = """
    NotesPane { layout: vertical; height: 1fr; width: 1fr; }
    NotesPane #editor { height: 1fr; border: none; }
    NotesPane #buttons { height: 3; padding: 0 1; }
    NotesPane #buttons Button { margin: 0 1 0 0; min-width: 0; }
    NotesPane #status { height: 1; padding: 0 1; color: $text-muted; }
    """

    BINDINGS = [
        Binding("ctrl+t", "new_tab", "new tab"),
        Binding("f2", "rename_tab", "rename"),
        Binding("ctrl+tab", "next_tab", "next tab", show=False),
        Binding("ctrl+shift+tab", "prev_tab", "prev tab", show=False),
        # step 6 (phone footer: "Insert (i)"). Non-priority: while the editor has focus, its
        # own key handling consumes the printable `i` as typed text first (same reasoning as the
        # standing `q`-quits-every-window binding, safe for the identical reason) -- this key only
        # fires when focus is elsewhere, e.g. a button. The button is the reliable path on the
        # phone; this is the accelerator the footer advertises for it.
        Binding("i", "insert", "insert", show=False),
        # priority=True: the focused TextArea's own key handling never sees these two, the same
        # way the supervisor pane's `enter` binding wins over DataTable's (pantheon/supervisor/pane.py).
        Binding("ctrl+space", "complete", "slash commands", show=False, priority=True),
        Binding("escape", "dismiss_completion", "dismiss", show=False, priority=True),
    ]

    def __init__(self, cfg: Optional[config_mod.Config] = None,
                 window_source: Optional[Callable[[], list[TmuxWindow]]] = None,
                 id: Optional[str] = "notes") -> None:
        super().__init__(id=id)
        self.cfg = cfg or config_mod.load()
        self._window_source = window_source
        self.dir = Path(self.cfg.state_dir) / "notes"
        self.tabs: list[str] = []
        self.focused: Optional[str] = None
        self._dirty = False
        self._loading = False
        self._debounce_timer = None
        self._message = ""
        self._message_role: Optional[str] = None
        self._commands: list[complete_mod.Command] = []

    # ------------------------------------------------------------------ layout

    def compose(self) -> ComposeResult:
        yield TabBar(self._tab_clicked, id="tabs")
        yield Editor(self, id="editor", soft_wrap=True)
        with Horizontal(id="buttons"):
            yield self._button("New tab", "new_tab")
            yield self._button("Rename", "rename_tab")
            yield self._button("Delete", "delete_tab", variant="error")
            yield self._button("Copy", "copy_tab")
            yield self._button(self._insert_label(), "insert", variant="primary", id="btn-insert")
        yield Static("", id="status")

    def _button(self, label: str, action: str, variant: str = "default", id: Optional[str] = None) -> Button:
        return Button(label, id=id or f"btn-{action}", variant=variant)

    def _insert_label(self) -> str:
        return "Insert into agent"

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Buttons and keys share one code path: a button id `btn-<name>` calls `action_<name>`, exactly what its key
        would do."""
        event.stop()
        button_id = event.button.id or ""
        if not button_id.startswith("btn-"):
            return
        method = getattr(self, f"action_{button_id[len('btn-'):]}", None)
        if method is not None:
            method()

    def on_mount(self) -> None:
        self._load_commands()
        self.dir.mkdir(parents=True, exist_ok=True)
        self.tabs, self.focused = store.restore(self.dir)
        if not self.tabs:
            self.focused = store.create(self.dir, NEW_TAB_TITLE)
            self.tabs = [self.focused]
        self._rebuild_tabs()
        self._load_into_editor(self.focused)
        self.query_one("#editor", TextArea).focus()
        self._refresh_insert_button()
        self.set_interval(max(1, int(self.cfg.refresh_seconds or 5)), self._refresh_insert_button)

    def _load_commands(self) -> None:
        try:
            extra = complete_mod.default_skills_dirs(self.cfg.claude_home)
            self._commands = complete_mod.list_commands(self.cfg.claude_home, extra)
        except Exception as exc:  # a bad commands/skills folder must never block the scratchpad
            self._log(exc)
            self._commands = []

    # ------------------------------------------------------------------ tabs

    def _rebuild_tabs(self) -> None:
        self.query_one("#tabs", TabBar).set_tabs(self.tabs, self.focused)

    def _tab_clicked(self, slug: str) -> None:
        if slug != self.focused:
            self._switch_to(slug)

    def _switch_to(self, slug: str) -> None:
        self._flush_now()
        self.focused = slug
        self._load_into_editor(slug)
        self._rebuild_tabs()
        store.write_session(self.dir, self.tabs, self.focused)

    def _load_into_editor(self, slug: Optional[str]) -> None:
        editor = self.query_one("#editor", TextArea)
        self._loading = True
        editor.load_text(store.read(self.dir, slug) if slug else "")
        self._loading = False

    # ------------------------------------------------------------------ autosave

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if self._loading or self.focused is None:
            return
        self._arm_debounce()

    def _arm_debounce(self) -> None:
        if self._debounce_timer is not None:
            self._debounce_timer.stop()
        self._dirty = True
        self._debounce_timer = self.set_timer(DEBOUNCE_SECONDS, self._on_debounce_fire)

    def _on_debounce_fire(self) -> None:
        self._debounce_timer = None
        self._flush_now()

    def _flush_now(self) -> None:
        """Write the editor's current text to the focused tab's file right now -- the debounce
        timer calls this on idle, and every interruption path (tab-switch, blur, quit) calls it
        directly so nothing typed is ever left only in memory."""
        if not self._dirty or self.focused is None:
            return
        editor = self.query_one("#editor", TextArea)
        store.write(self.dir, self.focused, editor.text)
        self._dirty = False

    def _on_editor_blur(self) -> None:
        """The editor itself lost focus -- `events.Blur` does not bubble (`bubble=False` in
        Textual), so `Editor` (below) calls this directly rather than the pane getting an
        `on_blur` of its own."""
        self._flush_now()

    def _on_app_blur(self) -> None:
        """The terminal itself lost focus (`events.AppBlur`, wired up by `NotesApp`) -- flush the
        same as a tab-switch or a plain widget blur."""
        self._flush_now()

    def on_unmount(self) -> None:
        self._flush_now()
        store.write_session(self.dir, self.tabs, self.focused)

    # ------------------------------------------------------------------ tab actions

    def action_new_tab(self) -> None:
        self._flush_now()
        slug = store.create(self.dir, NEW_TAB_TITLE)
        self.tabs.insert(0, slug)
        self.focused = slug
        self._rebuild_tabs()
        self._load_into_editor(slug)
        store.write_session(self.dir, self.tabs, self.focused)
        self.say(f"new draft '{slug}'")

    def action_rename_tab(self) -> None:
        if self.focused is None:
            return self.say(NO_TABS_MESSAGE, "warning")
        self.app.push_screen(
            TextPrompt("rename this draft", placeholder=self.focused, hint="Enter renames · Esc cancels"),
            self._renamed,
        )

    def _renamed(self, title: Optional[str]) -> None:
        if not title or self.focused is None:
            return
        old = self.focused
        new_slug = store.rename(self.dir, old, title)
        self.tabs = [new_slug if s == old else s for s in self.tabs]
        self.focused = new_slug
        self._rebuild_tabs()
        store.write_session(self.dir, self.tabs, self.focused)
        self.say(f"renamed to '{new_slug}'")

    def action_delete_tab(self) -> None:
        if self.focused is None:
            return self.say(NO_TABS_MESSAGE, "warning")
        self.app.push_screen(
            Confirm(f"delete this draft ('{self.focused}')? it cannot be undone", danger=True),
            self._delete_confirmed,
        )

    def _delete_confirmed(self, yes: Optional[bool]) -> None:
        if not yes or self.focused is None:
            return
        gone = self.focused
        store.delete(self.dir, gone)
        self.tabs = [s for s in self.tabs if s != gone]
        self._dirty = False
        if self.tabs:
            self.focused = self.tabs[0]
        else:
            self.focused = store.create(self.dir, NEW_TAB_TITLE)
            self.tabs = [self.focused]
        self._rebuild_tabs()
        self._load_into_editor(self.focused)
        store.write_session(self.dir, self.tabs, self.focused)
        self.say(f"deleted '{gone}'")

    def action_copy_tab(self) -> None:
        editor = self.query_one("#editor", TextArea)
        try:
            self.app.copy_to_clipboard(editor.text)
            self.say("copied to clipboard")
        except Exception as exc:  # pragma: no cover - clipboard access varies by terminal
            self._log(exc)
            self.say("could not reach the clipboard from here", "warning")

    def action_next_tab(self) -> None:
        self._step_tab(1)

    def action_prev_tab(self) -> None:
        self._step_tab(-1)

    def _step_tab(self, direction: int) -> None:
        if not self.tabs or self.focused not in self.tabs:
            return
        i = (self.tabs.index(self.focused) + direction) % len(self.tabs)
        self._switch_to(self.tabs[i])

    # ------------------------------------------------------------------ insert into agent

    def action_insert(self) -> None:
        editor = self.query_one("#editor", TextArea)
        text = editor.text
        if not text.strip():
            return self.say("nothing to insert - this draft is empty", "warning")
        row = selectable_target(self.cfg, self._window_source)
        if row is None:
            return self.say(NO_AGENT_MESSAGE, "warning")
        target = insert_target(row, self.cfg)
        ok = insert_into_agent(target, text, tmux=self.cfg.tools.tmux)
        if not ok:
            return self.say("tmux would not accept that text", "warning")
        name = row.project or row.session_id
        self.say(
            f"inserted {len(text)} chars into window {row.window_index} ({name}); "
            f"press j to go there, then send it yourself"
        )

    def _refresh_insert_button(self) -> None:
        try:
            btn = self.query_one("#btn-insert")
        except Exception:  # pragma: no cover - not mounted yet
            return
        row = selectable_target(self.cfg, self._window_source)
        btn.disabled = row is None
        btn.tooltip = None if row is not None else NO_AGENT_MESSAGE

    # ------------------------------------------------------------------ slash completion

    def action_complete(self) -> None:
        """`Ctrl+Space`: open a `Pick` of the commands matching the `/token` at the caret. Opened explicitly rather than popped up mid-keystroke, so it never fights the
        editor for focus -- picking one replaces the token in place; nothing is ever sent."""
        editor = self.query_one("#editor", TextArea)
        row, col = editor.cursor_location
        line = editor.document.get_line(row)
        token = complete_mod.token_at(line[:col]) or "/"
        matches = complete_mod.match(token, self._commands)
        if not matches:
            return self.say(f"no command starts with '{token}'", "warning")
        options = numbered(
            [(c.name, c.name) for c in matches],
            {c.name: c.description for c in matches},
        )
        start = (row, col - len(token))
        self.app.push_screen(
            Pick(f"commands matching '{token}'", options),
            lambda chosen: self._completed(chosen, start, (row, col)),
        )

    def _completed(self, chosen: Optional[str], start: tuple, end: tuple) -> None:
        if chosen is None:
            return
        editor = self.query_one("#editor", TextArea)
        editor.replace(chosen + " ", start, end)
        editor.focus()

    def action_dismiss_completion(self) -> None:
        # Nothing is kept open by this pane itself (the picker is a modal that owns its own
        # Escape) -- this exists so `Escape` never falls through to the TextArea when it has
        # nothing useful to do with it here either.
        return

    # ------------------------------------------------------------------ status line

    def say(self, message: str, role: Optional[str] = None) -> None:
        self._message = message
        self._message_role = role
        try:
            line = Text()
            colour = theme_mod.TOKENS.get(role or "", "")
            line.append(message, style=colour or None)
            self.query_one("#status", Static).update(line)
        except Exception:  # pragma: no cover - not mounted yet
            pass

    def _log(self, exc: BaseException) -> None:
        try:
            p = Path(self.cfg.log_file)
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "a", encoding="utf-8") as fh:
                fh.write(f"{utcnow_iso()} notes: {exc!r}\n{traceback.format_exc()}\n")
        except OSError:
            pass


# --------------------------------------------------------------------------- the app


class NotesApp(App):
    """`python -m pantheon.notes` -- tmux window 3 (F4)."""

    AUTO_FOCUS = None

    CSS = """
    Screen { layout: vertical; background: $background; }
    NotesPane { height: 1fr; }
    """

    BINDINGS = [
        Binding("q", "quit", "quit", show=False),
    ]

    def __init__(self, cfg=None, window_source: Optional[Callable[[], list[TmuxWindow]]] = None) -> None:
        super().__init__()
        self.cfg = cfg or config_mod.load()
        self.pane = NotesPane(self.cfg, window_source=window_source, id="notes")

    def compose(self) -> ComposeResult:
        yield WindowBar("notes")
        yield self.pane
        yield PhoneFooter("notes")
        yield make_footer()

    def on_mount(self) -> None:
        theme_mod.apply(self)
        self.title = "PANTHEON - notes"
        fit_footer(self, self.size.width)
        orphan_mod.install(self)

    def on_resize(self, event) -> None:
        fit_footer(self, event.size.width)

    def on_app_blur(self, event: tevents.AppBlur) -> None:
        self.pane._on_app_blur()

    def action_quit(self) -> None:
        self.pane._flush_now()
        store.write_session(self.pane.dir, self.pane.tabs, self.pane.focused)
        self.exit()


def main() -> int:
    cfg = config_mod.load()
    app = NotesApp(cfg)
    try:
        app.run()
    except Exception:
        log.exception("the notes pane stopped")
        print(f"the notes pane stopped; details are in {cfg.log_file}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
