"""The deck side of answering permission prompts.

`session_view/approval.py` holds the pure rules (what is asked, the summary line, which key the
dialog wants). This file joins them to live rows: each tick it reads the screen of every row that
is blocked on a prompt (one `capture-pane` each, only for those rows), keeps what each shows, and
answers a key press through one fresh read and one key. It also owns the full view (`v`).

A row stuck on a startup question (folder trust, a new MCP server; `pantheon/dialogs.py`) is
shown through the same surfaces: trust is answered with `dialogs.answer` (the existing path, never
a second one), every other startup question is `j` only.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Optional

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

from .. import dialogs as dialogs_mod
from .. import theme as theme_mod
from .. import tmuxctl
from ..models import AgentState, AgentStatus, TmuxWindow, format_age
from ..session_view import approval as ap
from . import state as state_mod

PERMISSION = "permission"
TRUST = "trust"
STARTUP = "startup"          # any other startup question: shown, answered only in the window

KEYS_HINT = "a allow once · d deny · v see all of it · j go to the window"
KEYS_HINT_FULL_FIRST = "a see all of it first · d deny · v see all of it · j go to the window"
KEYS_HINT_TRUST = "a trust it · d exit · v see all of it · j go to the window"
KEYS_HINT_THERE = "v see all of it · j go to the window to answer it"
NOTHING_WAITING = "nothing on that row is waiting for an answer"
TRUST_NOTE = "Claude asks before it loads this folder's own settings"


@dataclass
class Shown:
    """One row's open prompt, as the deck shows it."""
    session_id: str
    kind: str                                  # PERMISSION | TRUST | STARTUP
    request: ap.Request
    summary: ap.Summary
    target: Optional[str] = None               # tmux target to read and type into; None = no window
    screen: Optional[ap.ScreenPrompt] = None
    dialog: Optional[dialogs_mod.Dialog] = None
    project: str = ""
    where: str = ""

    @property
    def answerable(self) -> bool:
        """Can `a`/`d` answer it from the deck at all (before the fresh read)?"""
        if self.target is None:
            return False
        if self.kind == TRUST:
            return True
        if self.kind == STARTUP:
            return False
        return self.summary.risk != "question"

    def line(self, width: int = ap.WIDTH, ascii_only: bool = False) -> tuple[str, int]:
        return ap.line(self.summary, width, ascii_only)

    def needs_full_view(self, width: int = ap.WIDTH, ascii_only: bool = False) -> bool:
        """True when a first `a` from a surface `width` wide opens the full view instead."""
        if self.kind == TRUST:
            return self.line(width, ascii_only)[1] > 0
        return ap.needs_full_view(self.summary, self.line(width, ascii_only)[1])

    def hint(self, width: int = ap.WIDTH, ascii_only: bool = False) -> str:
        if self.kind == TRUST:
            text = KEYS_HINT_TRUST
        elif not self.answerable:
            text = KEYS_HINT_THERE
        elif self.needs_full_view(width, ascii_only):
            text = KEYS_HINT_FULL_FIRST
        else:
            text = KEYS_HINT
        return text.replace("·", "-") if ascii_only else text

    def refusal(self) -> Optional[str]:
        """Why `a`/`d` will not answer this one from the deck, or None when they may try."""
        if self.target is None:
            return ap.ANSWER_IN_DESKTOP
        if self.kind == STARTUP:
            return f"{self.dialog.reason if self.dialog else 'a startup question'} -- {ap.ANSWER_THERE}"
        if self.summary.risk == "question":
            return f"that is a question, not a yes/no -- {ap.ANSWER_THERE}"
        return None


def target_for(row: AgentState, windows: list[TmuxWindow], session: str) -> Optional[str]:
    """The pane to read and type into: its pane id when known (a session staged beside the deck
    lives in window 0, so `session:index` would hit the deck), else `session:index`."""
    if row.window_index is None:
        return None
    w = state_mod.match_window(row.cwd, row.tmux_pane, windows, session, provider=row.provider)
    if w is not None and w.pane_id:
        return w.pane_id
    return f"{row.tmux_session or session}:{row.window_index}"


class Approvals:
    """Per-tick state: session_id -> `Shown`, plus the reads that keep it honest."""

    def __init__(self, capture: Optional[Callable[..., str]] = None,
                 press: Optional[Callable[..., bool]] = None, max_reads: int = 6,
                 sleep: Optional[Callable[[float], None]] = None) -> None:
        # None = `tmuxctl.capture` / `dialogs.send_key` / `time.sleep`, looked up at call time so
        # a test or the render script can replace the module function.
        self._capture_fn = capture
        self._press = press
        self.max_reads = max_reads
        self._sleep = sleep
        self.shown: dict[str, Shown] = {}
        self.busy = False      # one answer at a time, one window at a time

    def _capture(self, target: str, tmux: Optional[str] = None) -> str:
        return (self._capture_fn or tmuxctl.capture)(target, tmux=tmux)

    # ------------------------------------------------------------------ each tick

    def update(self, rows: list[AgentState], events: list, windows: list[TmuxWindow],
               row_dialogs: dict, session: str, tmux: Optional[str] = None,
               ascii_only: bool = False) -> list[AgentState]:
        """Rebuild `shown` and return the rows (re-sorted when a read found a prompt answered)."""
        requests = ap.pending(events)
        shown: dict[str, Shown] = {}
        reads = 0
        changed = False
        for row in rows:
            sid = row.session_id
            where = "no window" if row.where == "desktop" else row.where
            found = row_dialogs.get(sid)
            if found is not None:
                _t, dialog = found
                tgt = target_for(row, windows, session)
                name = _short(dialog.folder) or row.project or "this folder"
                if dialog.is_trust:
                    summary = ap.Summary("trust", "folder", name, True)
                    kind = TRUST
                else:
                    summary = ap.Summary("question", "startup", dialog.reason, True)
                    kind = STARTUP
                req = ap.Request(sid, "", None, row.cwd or "", row.mode or "", row.last_event_ts)
                shown[sid] = Shown(sid, kind, req, summary, tgt, None, dialog, row.project or "", where)
                continue
            if row.status is not AgentStatus.BLOCKED_PERMISSION:
                continue
            req = requests.get(sid) or ap.Request(sid, "", None, row.cwd or "", row.mode or "",
                                                  row.last_event_ts, row.last_action)
            if not req.cwd and row.cwd:
                req = ap.Request(req.session_id, req.tool, req.tool_input, row.cwd, req.mode,
                                 req.since, req.message, req.subagent)
            tgt = target_for(row, windows, session)
            screen_prompt = None
            if tgt is not None and reads < self.max_reads and row.provider == "claude":
                reads += 1
                screen = self._capture(tgt, tmux=tmux)
                if screen and screen.strip():
                    screen_prompt = ap.read_screen(screen)
                    if screen_prompt is None:
                        # Answered in the window (or on the phone, or Remote Control): no prompt
                        # on screen any more. A "No" there writes no event at all, so without
                        # this the row would say "blocked" until the next one.
                        row.status = AgentStatus.WORKING if ap.busy(screen) else AgentStatus.WAITING_INPUT
                        row.last_action = "prompt answered"
                        changed = True
                        continue
            summary = ap.summarize(req, screen_prompt, ascii_only)
            item = Shown(sid, PERMISSION, req, summary, tgt, screen_prompt, None, row.project or "", where)
            shown[sid] = item
            row.last_action = f"{summary.risk} · {summary.tool} · {summary.target}"[:state_mod.MESSAGE_CHARS]
        self.shown = shown
        if changed:
            rows = sorted(rows, key=state_mod._sort_key)
        return rows

    # ------------------------------------------------------------------ one answer

    def answer(self, session_id: str, allow: bool, tmux: Optional[str] = None,
               settle: float = 0.6) -> tuple[bool, str]:
        """One key, into one window, after a fresh read. Never `Yes, and don't ask again`."""
        item = self.shown.get(session_id)
        if item is None:
            return False, NOTHING_WAITING
        refusal = item.refusal()
        if refusal:
            return False, refusal
        if self.busy:
            return False, "still answering the last one -- try again in a moment"
        self.busy = True
        try:
            capture = lambda t: self._capture(t, tmux=tmux)
            sleep = self._sleep or time.sleep
            if item.kind == TRUST:
                ok, why = dialogs_mod.answer(item.target, item.dialog, allow, tmux=tmux, sleep=sleep,
                                             capture=self._capture, press=self._press or dialogs_mod.send_key,
                                             settle=min(settle, 0.3))
                return ok, why
            press = self._press or dialogs_mod.send_key
            shown_text = item.screen.text if (item.screen is not None and not item.request.known) else ""
            ok, why = ap.answer(item.target, item.request, allow, capture,
                                lambda t, k: press(t, k, tmux=tmux), sleep, shown_text, settle)
            if ok or why in (ap.ANSWERED_ELSEWHERE,):
                self.shown.pop(session_id, None)
            return ok, why
        finally:
            self.busy = False

    # ------------------------------------------------------------------ the full view

    def full_text(self, session_id: str, now: datetime) -> str:
        item = self.shown.get(session_id)
        if item is None:
            return NOTHING_WAITING
        waited = "?"
        if item.request.since:
            from ..models import parse_ts
            t = parse_ts(item.request.since)
            if t is not None:
                waited = format_age(max(0.0, (now - t).total_seconds()))
        if item.dialog is not None:
            body = [item.dialog.reason, "", *("    " + l for l in item.dialog.text.splitlines())]
            if item.kind == TRUST:
                body += ["", f"a = \"Yes, I trust this folder\" · d = No ({item.dialog.no_effect})"]
            body += ["", f"waiting {waited} · {item.where}"]
            return "\n".join(body)
        return ap.full_text(item.request, item.screen, item.project, item.where, waited)


def _short(folder: Optional[str]) -> str:
    if not folder:
        return ""
    return folder.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


# --------------------------------------------------------------------------- the `v` screen


class RequestView(ModalScreen[Optional[str]]):
    """The whole request, word for word (`v`, or the first `a` on a cut line). Returns "allow",
    "deny", "jump" or None (Esc). `a`/`d` here are the SECOND press: the deck re-reads the window
    before typing anything, exactly as for a first press."""

    BINDINGS = [
        Binding("escape", "dismiss(None)", "back"),
        Binding("a", "choose('allow')", "allow once"),
        Binding("d", "choose('deny')", "deny"),
        Binding("j", "choose('jump')", "go to the window"),
    ]

    DEFAULT_CSS = """
    RequestView { align: center middle; }
    RequestView > Vertical#box {
        width: 84; height: auto; max-height: 90%;
        border: round $warning; border-title-color: $warning; border-title-style: bold;
        background: $panel; padding: 0 1;
    }
    RequestView #request-body { height: auto; max-height: 30; }
    RequestView #request-keys { height: 1; color: $text-muted; }
    """

    def __init__(self, title: str, body: str, answerable: bool, trust: bool = False,
                 note: str = "") -> None:
        super().__init__()
        self._title = title
        self._body = body
        self.answerable = answerable
        self.trust = trust
        self.note = note

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            with VerticalScroll(id="request-body"):
                yield Static(Text(self._body), id="request-text")
            if self.note:
                yield Static(Text(self.note, style=theme_mod.TOKENS["warning"]), id="request-note")
            yield Static(self._keys(), id="request-keys")

    def _keys(self) -> str:
        if self.trust:
            return "a trust it · d exit · Esc back · j go to the window"
        if self.answerable:
            return "a allow once · d deny · Esc back · j go to the window"
        return "Esc back · j go to the window to answer it"

    def on_mount(self) -> None:
        box = self.query_one("#box", Vertical)
        box.border_title = self._title
        box.styles.width = min(84, max(40, self.app.size.width - 2))

    def action_choose(self, what: str) -> None:
        if what in ("allow", "deny") and not self.answerable:
            self.query_one("#request-keys", Static).update(f"{ap.ANSWER_THERE} -- Esc back")
            return
        self.dismiss(what)
