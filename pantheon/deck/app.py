"""The desk view: one screen with everything on it.

Three layouts:

- Home, 120 columns and wider: the BUDGET strip on top, the SESSIONS sidebar on the left (projects
  with their sessions nested under them) and THE PIT's columns table on the right. `t` adds the
  task list as a right-hand column (F2 opens it in its own window).
- Staged: a session's REAL terminal sits beside the deck in this same tmux window
  (`pantheon/stage.py` moves its pane in). The deck pane is then sidebar-wide and shows a two-line
  usage block and the sidebar, nothing else. Staged is decided by `stage.current`, never by
  width -- the deck pane is narrow then, and narrow must not fall into the phone tier.
- Phone, narrower than 120 columns with nothing staged: only the agents panel, exactly what tmux
  window 0 always showed; F2 / F3 reach the queue and the usage window.

`✎ Edit a file` (or `e` in the list, `E` anywhere) opens a terminal text editor as one more real
tmux pane beside the list; while it is open the deck keeps
the staged layout, and `b`, quit and restart move it to its own window with its text, never close it.

Clicking a running session stages it (or, on a narrow window, switches to its own window);
clicking a finished Claude conversation resumes it in a new window and stages that. The 
conversations wall is gone from the deck.

Panel names follow the deck fiction and each is glossed in `?`: THE PIT is the agents table;
SESSIONS is the project/session list; QUEUE is the task list; BUDGET is usage.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from rich.text import Text
from textual import events as tevents
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Footer, Static

from .. import assistant as assistant_mod
from .. import config as config_mod
from .. import connectors as connectors_mod
from .. import orphan as orphan_mod
from .. import restart as restart_mod
from ..widgets.footer import PhoneFooter, fit_footer, make_footer
from .. import keys as keys_mod
from .. import theme as theme_mod
from ..hud.strip import HudStrip
from ..tools.app import render_report as tools_report
from ..models import AgentStatus, format_clock, parse_ts
from ..notify import line as notify_line_mod
from ..notify import presence as presence_mod
from ..pit import tmux_pit
from .. import stage as stage_mod
from .. import editor as editor_mod
from .. import tmuxctl
from ..dispatch import projects as projects_mod
from ..queue.pane import QueuePane
from ..session_view import models as session_models
from ..supervisor.pane import SupervisorPane
from ..supervisor.rail import SessionSidebar
from ..widgets.modal import Confirm, Pick, PickOption, TextPrompt

log = logging.getLogger("pantheon.deck")

WIDE_AT = 120          # columns; below this only the agents panel shows
HEADER_REFRESH_S = 5
PRESENCE_TOUCH_SECONDS = 30   #, "every 30s while it has focus"
PROJECTS_CACHE_SECONDS = 30   # the sidebar's project list scans the workspace folder

PIT_TITLE = "THE PIT · agents"
QUEUE_TITLE = "QUEUE"
BUDGET_TITLE = "BUDGET"
SESSIONS_TITLE = "SESSIONS"

# Layouts (see the module docstring).
DESK, PHONE, STAGED = "desk", "phone", "staged"

# words the user reads, in one place so the tests check the exact text.
PROJECT_MENU_TITLE = "{name}: what now?  (Esc = never mind)"
PROJECT_MENU = [
    ("c", "claude", "New Claude session here"),
    ("x", "codex", "New Codex job here", "needs a first message"),
    ("r", "resume", "Resume a conversation here"),
    ("o", "folder", "Open folder", "a shell window in that folder"),
]
PROJECT_MENU_WHO = {"claude": "claude", "codex": "codex-headless", "resume": "claude-resume"}
CONFIRM_LIVE_ELSEWHERE = "resume this conversation here too?"
CONFIRM_LIVE_ELSEWHERE_BODY = ("it is still open in the Desktop app; resuming here means both "
                               "copies write to the same conversation")
CODEX_NO_RESUME = ("Codex conversations can't be reopened here: Codex's own screen doesn't run "
                   "in tmux on this PC. Start a new Codex job from its project heading.")
OTHER_TMUX = "that session runs in another tmux ({name}); j in the agents table goes there"
# the one question `e` asks when nothing names a file yet.
EDIT_PROMPT_TITLE = "edit which file?"
EDIT_PROMPT_PLACEHOLDER = "in {folder}\ntype a file name there, or a whole path"
EDIT_PROMPT_HINT = "Enter opens it beside the list · Esc cancels"
EDIT_NOTHING = "no file named; nothing opened"


def _sent_channel_text(record: dict) -> str:
    """Which channel(s) to name in the deck's mirror of a `sent.jsonl` line: the channels that
    actually succeeded when a real send was attempted (`record["results"]`), else the channels
    the notifier decided to use (a dry run, or a quiet-hours suppression never reaches `results`
    at all)."""
    results = record.get("results")
    if isinstance(results, list) and results:
        ok_names = [r.get("channel", "") for r in results if isinstance(r, dict) and r.get("ok")]
        if ok_names:
            return "+".join(ok_names)
    channels = record.get("channels") or []
    return "+".join(channels) if channels else "?"


def _status_tally(rows) -> dict[str, int]:
    """The command bar's rollup: needing the user, failed, working,
    and the quiet dim group -- parked / idle / quiet / done. `needs_human` (blocked-permission,
    waiting-input, resume-failed) and `failed` are their own pills and never fall into the dim
    group; every other status lands in exactly one bucket. `quiet` is its own dim bucket, never `working`
    -- that inflated count was the bug."""
    tally = {"need_you": 0, "failed": 0, "working": 0, "parked": 0, "idle": 0, "quiet": 0, "done": 0}
    for r in rows:
        if r.needs_human:
            tally["need_you"] += 1
        elif r.status == AgentStatus.FAILED:
            tally["failed"] += 1
        elif r.status in (AgentStatus.WORKING, AgentStatus.RUNNING, AgentStatus.WINDING_DOWN):
            tally["working"] += 1
        elif r.status == AgentStatus.PARKED:
            tally["parked"] += 1
        elif r.status in (AgentStatus.IDLE, AgentStatus.UNKNOWN, AgentStatus.QUEUED):
            tally["idle"] += 1
        elif r.status == AgentStatus.QUIET:
            tally["quiet"] += 1
        elif r.status == AgentStatus.DONE:
            tally["done"] += 1
        # AgentStatus.GONE rows never reach here -- state_mod.fold() drops them after the
        # hide-a-finished-row window, so there is no bucket for them.
    return tally


def _pct_segment(name: str, pct: Optional[float], dim: str) -> Text:
    """`claude 42%`, coloured by `theme.gauge_colour` -- or `claude —` in the dim colour when the
    percentage is not known yet (no `state/hud.json`, or build D's `HudStrip.picture` has not
    landed in this worktree)."""
    seg = Text()
    if pct is None:
        seg.append(f"{name} —", style=dim)
    else:
        seg.append(f"{name} {pct:.0f}%", style=theme_mod.gauge_colour(pct))
    return seg


class DeckApp(App):
    """`python -m pantheon.deck` runs this in tmux window 0."""

    CSS = """
    Screen { layout: vertical; background: $background; }
    /* step 3, bold diet: the command bar's own words already carry their own weight (the
       wordmark and the state pills are bold in `_header_text`), so the whole line no longer is --
       the dim rollup beside them was competing with the pills for the same attention. */
    #deck-header {
        height: 1; padding: 0 1; background: $panel; color: $foreground;
        text-wrap: nowrap; text-overflow: clip;
    }
    /* No frame of its own since the strip is a row of framed cards (hud/strip.py), and a
       second border around them cost two rows and four columns for nothing. */
    #hud-strip { height: auto; padding: 0; margin: 0; border: none; }
    /* one blank row between the BUDGET strip and the panes. The phone tier has no
       strip and no row to spare, so `.narrow` takes it straight back off (the 65x26 render must
       not move by a single character). */
    #panes { height: 1fr; margin-top: 1; }
    Screen.narrow #panes, Screen.staged #panes { margin-top: 0; }
    SupervisorPane, QueuePane {
        height: 1fr;
        /* an unfocused panel is a hairline in chrome_dim, the focused one is heavy
           and cyan -- so exactly one border on screen says "the keys go here". */
        border: round #1D5B6E; border-title-color: $primary; border-title-style: bold;
        border-subtitle-color: $text-muted;
    }
    /* decision 6: the deck's own split, measured against the SCREEN (not each panel), so
       the desk width tiers inside THE PIT and QUEUE can be measured against their own panel
       width and actually reach the desk-sized columns (the desk-first review, findings 2/5).
       THE PIT gets the larger share; QUEUE narrows its summary column instead of losing rows. */
    SupervisorPane { width: 1fr; min-width: 66; }
    QueuePane { width: 43%; min-width: 54; max-width: 78; }
    /* the sidebar is projects with their sessions nested -- always there at desk width, and
       the whole deck pane while a session is staged beside it. */
    SessionSidebar {
        width: 34; min-width: 24;
        border: round #1D5B6E; border-title-color: $primary; border-title-style: bold;
    }
    Screen.staged SessionSidebar { width: 1fr; }
    SupervisorPane:focus-within, QueuePane:focus-within, QueuePane:focus,
    SessionSidebar:focus-within {
        border: heavy $primary; border-title-color: $primary; border-title-style: bold;
    }
    /* The phone is narrower than the desk minimum (Termius portrait is about 46 columns): the pit
       takes exactly the screen there, so its two-line rows clip at the real edge. */
    Screen.narrow SupervisorPane { border: none; min-width: 0; }
    /* staged: claude/codex usage in two lines above the sidebar. */
    #stage-usage { height: auto; padding: 0 1; display: none; }
    #deck-keys { display: none; height: 1fr; padding: 1 2; background: $panel; }
    #deck-status-row { height: 1; }
    #deck-status { width: 1fr; padding: 0 1; color: $text-muted; }
    #deck-status-right { width: auto; padding: 0 1; color: $text-muted; }
    """

    BINDINGS = [
        Binding("tab", "next_pane", "switch panel", priority=True),
        Binding("shift+tab", "prev_pane", "switch panel", show=False, priority=True),
        Binding("question_mark", "toggle_keys", "keys"),
        # `n` works from either panel: a new session is not a row action.
        Binding("n", "new_session", "new session", priority=True),
        # the pit toggle -- capital `P` (not `p`), because lower-case `p` is already
        # the row toolbar's Mode… action in the supervisor pane AND the queue's project filter;
        # no priority needed since nothing else in the deck claims Shift+P.
        Binding("P", "toggle_pit", "pit", show=False),
        # the QUEUE is off the desk view by default -- `t` brings the task list back as a
        # right-hand column.
        Binding("t", "toggle_queue", "task list", show=False),
        # `b` is the sidebar's `◀ back` line -- the staged session goes back to its own
        # window and the deck gets the whole screen again.
        Binding("b", "unstage", "back", show=False),
        # capital `E` from anywhere in the deck (lower-case `e` is the agents table's effort
        # key; in the SESSIONS list `e` itself opens the editor).
        Binding("E", "edit_file", "edit a file", show=False),
        # capital `A` puts the pinned Assistant on screen (starting it first when it is not
        # running). No priority, same as `R`: a type-to-agent box must still take a capital A.
        Binding("A", "assistant", "assistant", show=False),
        # Capital `L`: re-make the Assistant's phone link (Remote Control) when it is down. Free
        # everywhere in the deck (lower-case `l` is the table's log key); no priority, same as `A`.
        Binding("L", "assistant_reconnect", "reconnect phone link", show=False),
        Binding("escape", "close_keys", "close keys", show=False),
        # capital `R` (lower-case `r` is the table's and the queue's refresh). No priority:
        # a conversation's type-to-agent box must still take a capital R as a letter.
        Binding("R", "restart", "restart"),
        Binding("q", "quit_deck", "quit window"),
    ]

    def __init__(
        self,
        cfg: Optional[config_mod.Config] = None,
        window_source: Optional[Callable] = None,
        source=None,
    ) -> None:
        super().__init__()
        self.cfg = cfg or config_mod.load()
        # `list-windows` only reports a window's ACTIVE pane, so a session staged beside the
        # deck would drop out of every list the moment the keyboard is back on the sidebar;
        # `stage.with_staged` adds it back as window 0 (no tmux call when nothing is staged).
        base_windows = window_source or (lambda: tmuxctl.list_windows_cached(None))
        windows = lambda: stage_mod.with_staged(list(base_windows() or []), self.cfg)
        self.sup = SupervisorPane(self.cfg, window_source=windows, id="supervisor")
        # the switcher rail reuses the supervisor pane's own already-folded rows
        # (`rows_source`) instead of re-parsing the event log and re-polling tmux a second time
        # on the same tick -- same pattern as the queue pane's `agent_rows_source` just below.
        self._projects_cache: tuple[float, list] = (0.0, [])
        self.rail = SessionSidebar(self.cfg, rows_source=lambda: self.sup.rows,
                                   approvals_source=lambda: self.sup.approvals.shown,
                                   window_source=windows, id="rail",
                                   projects_source=self._projects,
                                   assistant_source=self._assistant_view)
        self._assistant_starting = False   # a start is running in a worker thread
        self.stage_usage = Static("", id="stage-usage", markup=False)
        self._staged: Optional[stage_mod.Staged] = None
        self._editor_beside = False   # the editor pane is in window 0 beside the list
        self._mode: Optional[str] = None
        # perf sweep item 1: the queue's dispatch concurrency guard reuses the supervisor pane's
        # own last fold instead of folding the whole event log again for every `w`/`x` press.
        # decision 9 (the review's "one thing found that is not about looks"): hand the queue
        # pane the supervisor's own already-fetched window list instead of letting it fall back
        # to a live tmux call on the drawing thread -- the agents panel already avoids this by
        # having its window source handed in; the queue panel gets the same one, for free, via
        # the supervisor pane it already shares a fold with.
        self.queue = QueuePane(self.cfg, source=source, standalone=False, id="queue",
                               agent_rows_source=lambda: self.sup.rows,
                               window_source=lambda: self.sup.windows)
        #: the command bar's "N need you" pill and the NEEDS YOU card must
        # read the SAME folded rows -- without `agents_source` the card never saw a real agent at
        # all (`HudStrip._agents` returns `[]` when it is `None`), so it could say "nothing
        # needs you" while the pill, built straight from `self.sup.rows`, said otherwise.
        self.strip = HudStrip(self.cfg, id="hud-strip", agents_source=lambda: self.sup.rows,
                              attention_source=self._assistant_attention)
        self._assistant_last = None        # the last `assistant.view` drawn (the NEEDS YOU line)
        self._assistant_reconnecting = False
        self._wide: Optional[bool] = None
        # at desk width the QUEUE starts hidden (`t` brings it back). Meaningless at phone
        # width, where the tree is exactly what it always was.
        self._queue_open = False
        self._message = ""
        self._keys_page = 0   # `?` overlay: 0 = the key list, 1 = the connector gap list, 2 = the tool inventory (skills/hooks/guards sync report)
        self._sent_seen_size = 0   # how much of state/notify/sent.jsonl we have read

    # ------------------------------------------------------------------ layout

    def compose(self) -> ComposeResult:
        yield Static("PANTHEON", id="deck-header", markup=False)
        yield self.strip
        yield self.stage_usage
        with Horizontal(id="panes"):
            yield self.rail
            yield self.sup
            yield self.queue
        with VerticalScroll(id="deck-keys"):
            # `markup=True` here (and ONLY here): the nav line is the one place this overlay
            # needs a Rich action link. The body keeps `markup=False` -- `keys_mod.KEYS_TEXT`
            # spells out `] [` for the tab-cycle keys, which Rich would otherwise try to parse.
            yield Static("", id="deck-keys-nav", markup=True)
            yield Static(keys_mod.KEYS_TEXT, id="deck-keys-body", markup=False)
        with Horizontal(id="deck-status-row"):
            yield Static("", id="deck-status", markup=False)
            yield Static("", id="deck-status-right", markup=False)
        yield PhoneFooter("deck")
        yield make_footer()

    def on_mount(self) -> None:
        theme_mod.apply(self)
        self.title = "PANTHEON"
        self.strip.border_title = BUDGET_TITLE
        self.sup.border_title = PIT_TITLE
        self.queue.border_title = QUEUE_TITLE
        self.rail.border_title = SESSIONS_TITLE
        # a record left by a deck that did not close cleanly names a pane that may be gone.
        self._sync_stage(verify=True, relayout=False)
        self._apply_width(self.size.width)
        # lines already in sent.jsonl before the deck opened are history, not news --
        # only lines appended from here on show in the status line.
        self._sent_seen_size = self._sent_file_size()
        self._refresh_header()
        self.set_interval(HEADER_REFRESH_S, self._refresh_header)
        self.set_interval(PRESENCE_TOUCH_SECONDS, self._touch_presence_if_focused)
        orphan_mod.install(self)     # leave when the tmux server is gone (pantheon/orphan.py)
        self._focus_default()
        # the pinned Assistant comes back whenever the deck starts -- after `R`, after
        # `pantheon --restart --all`, after a tmux server death -- resuming the same conversation.
        # Only a deck running INSIDE tmux does this: a deck opened anywhere else (a render, a
        # script, a Desktop-app session) must never open windows in a tmux server it can see --
        # a render did exactly that to the live server on 2026-09-27.
        if self.cfg.assistant_settings().enabled and tmuxctl.in_tmux():
            self.run_worker(lambda: self._assistant_worker(stage_after=False), thread=True)

    def on_resize(self, event: tevents.Resize) -> None:
        self._apply_width(event.size.width)

    # ------------------------------------------------------------------ presence + the sent line
    #

    async def on_event(self, event: tevents.Event) -> None:
        """Every key press and mouse click means the user is at the controls
        -- checked here, at the very first point
        the raw event reaches the app. A plain `on_key`/`on_click` handler would miss a
        PRIORITY-bound key (this app's own Tab/Shift+Tab, the supervisor pane's Enter):
        `App.on_event` resolves priority bindings itself before a key ever reaches a normal
        handler, so a `Binding(..., priority=True)` key would otherwise touch nothing. This
        override never swallows or stops anything -- `super.on_event(event)` always runs
        after it, doing exactly what it would have done without this override in the way."""
        if isinstance(event, (tevents.Key, tevents.Click)):
            presence_mod.touch(self.cfg)
        await super().on_event(event)

    def _touch_presence_if_focused(self) -> None:
        if self.app_focus:
            presence_mod.touch(self.cfg)

    def _sent_file_size(self) -> int:
        try:
            return self.cfg.notify_sent_file.stat().st_size
        except OSError:
            return 0

    def _check_sent_notifications(self) -> None:
        """`sent: Claude needs you in loom-os -> toast` in the status line, once, when
        `state/notify/sent.jsonl` gains a line while the deck is open -- no timer of its own (this
        rides the existing 5-second header refresh) and no toast (the notifier already sent one,
        or decided not to; this is the deck's own mirror of that decision, nothing more)."""
        size = self._sent_file_size()
        if size <= self._sent_seen_size:
            self._sent_seen_size = size
            return
        path = self.cfg.notify_sent_file
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                fh.seek(self._sent_seen_size)
                new_text = fh.read(size - self._sent_seen_size)
        except OSError:
            self._sent_seen_size = size
            return
        self._sent_seen_size = size
        last_record: Optional[dict] = None
        for raw_line in new_text.splitlines():
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                record = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                last_record = record
        if last_record is not None:
            self._say(notify_line_mod.sent_line(
                str(last_record.get("title", "")), str(last_record.get("body", "")),
                _sent_channel_text(last_record), str(last_record.get("detail", "")),
            ))

    def _apply_panes(self) -> None:
        """Which panes are on screen right now.

        Desk: SESSIONS sidebar | THE PIT's columns table, with the QUEUE off unless `t` asked
        for it. Staged: the usage block and the sidebar only (the session's real terminal is the
        tmux pane to the right). Phone (< WIDE_AT, nothing staged): the agents panel alone,
        exactly the tree it has always been."""
        mode = self._mode or PHONE
        self.rail.display = mode in (DESK, STAGED)
        self.sup.display = mode in (DESK, PHONE)
        self.queue.display = mode == DESK and self._queue_open
        self.strip.display = mode == DESK
        self.stage_usage.display = mode == STAGED
        if mode == STAGED:
            # "the usage and the sidebar, nothing else": no key footer either -- every line in
            # the sidebar is its own control.
            for footer in self.query(Footer):
                footer.display = False
            for line in self.query(PhoneFooter):
                line.display = False

    def _focus_default(self) -> None:
        """Where the keys go when the layout changes: the agents table on the desk and the
        phone, the sidebar while a session is staged (it is all there is)."""
        if self._mode == STAGED:
            self.rail.focus_table()
        elif self.sup.display:
            self.sup.focus_table()

    def _mode_for(self, width: int) -> str:
        if self._staged is not None or self._editor_beside:
            return STAGED
        return DESK if width >= WIDE_AT else PHONE

    def _apply_width(self, width: int) -> None:
        fit_footer(self, width)   # the four-key phone line under 80 columns (S-UI section 8)
        mode = self._mode_for(width)
        if mode == self._mode:
            if mode == STAGED:
                self._apply_panes()   # fit_footer just showed a footer again; staged has none
            return
        self._mode = mode
        wide = mode == DESK
        self._wide = wide
        # A width change always returns to the tier's own default layout: a phone-width deck has
        # no queue column to open.
        if mode != DESK:
            self._queue_open = False
        self._apply_panes()
        self.screen.set_class(mode == PHONE, "narrow")
        self.screen.set_class(mode == STAGED, "staged")
        if mode == STAGED:
            try:
                self.query_one("#deck-header").display = False
                self.query_one("#deck-status-right", Static).display = False
            except Exception:
                pass
            self._repaint_stage_usage()
            self._say("")
            self._focus_default()
            return
        # At desk width the deck's own header carries the counts; the agents panel's header
        # would just say the same thing again one line lower.
        try:
            self.sup.query_one("#header").display = not wide
            self.query_one("#deck-header").display = wide
            # The right-hand standing context is desk-only: at phone width the
            # status row stays exactly the one hand-written message it always was, so the
            # 65x26 render's characters do not change.
            self.query_one("#deck-status-right", Static).display = wide
        except Exception:
            pass
        if wide:
            self._repaint_status_right()
        if not wide:
            # 57 characters. The status row has `padding: 0 1`, so at 65 columns it has 63 to
            # draw in -- the earlier wording (with a leading `phone: `) came to 64 and the user's
            # phone showed `F3 =` with the word budget cut off. Any edit here counts characters first.
            self._say("agents only; n = new session, F2 = task list, F3 = budget")
            self.sup.focus_table()
        else:
            self._say("")
            self._focus_default()

    # ------------------------------------------------------------------ header / status

    def _refresh_header(self) -> None:
        try:
            self.query_one("#deck-header", Static).update(self._header_text())
            tab = getattr(self.queue, "tab", None)
            if tab is not None and self._wide:
                n = len(getattr(self.queue, "rows_on_screen", []) or [])
                self.queue.border_title = f"{QUEUE_TITLE} · {tab.name} {n}"
        except Exception as exc:  # the header is decoration; never let it take the deck down
            log.debug("header refresh failed: %r", exc)
        # the rail has no timer of its own when it reuses the supervisor's rows
        # (`rows_source`, set in `__init__`) -- it redraws from `self.sup.rows` on this same
        # 5-second tick instead, no second tmux poll or event-log read.
        # is the staged session still beside the deck? One tmux question, and only while
        # something is staged; an agent that exited drops the deck back to the home layout.
        try:
            self._sync_stage(verify=True)
        except Exception as exc:
            log.debug("stage check failed: %r", exc)
        if self.rail.display:
            try:
                self.rail.refresh_rows()
            except Exception as exc:
                log.debug("rail refresh failed: %r", exc)
        if self.stage_usage.display:
            self._repaint_stage_usage()
        # Border subtitles: each pane's own live count in
        # its bottom border, e.g. `8 running · sorted by who needs you`, `1 of 3 · every 5s`.
        # `getattr` because builds B/C add `subtitle` in the same window this build lands in.
        for pane in (self.sup, self.queue):
            subtitle_fn = getattr(pane, "subtitle", None)
            if callable(subtitle_fn):
                try:
                    pane.border_subtitle = subtitle_fn()
                except Exception as exc:
                    log.debug("%r subtitle failed: %r", pane, exc)
        try:
            self._repaint_status_right()
        except Exception as exc:
            log.debug("status-right repaint failed: %r", exc)
        # polish list: the NEEDS YOU card otherwise only redraws on the strip's own 30-second
        # file-read recompose, so a new needs-you agent could sit off the card for up to 30s
        # after it already shows on THE PIT's table. This repaints only that card, from the
        # supervisor rows already in hand -- no file read, no recompose.
        refresh_needs_you = getattr(self.strip, "refresh_needs_you", None)
        if callable(refresh_needs_you):
            try:
                refresh_needs_you()
            except Exception as exc:
                log.debug("needs-you refresh failed: %r", exc)
        # no timer of its own -- piggybacks on this existing 5-second poll.
        try:
            self._check_sent_notifications()
        except Exception as exc:
            log.debug("sent-notification check failed: %r", exc)

    def _header_text(self) -> Text:
        """The command bar: wordmark, state pills, a dim rollup
        of the rest, then a right-hand identity block -- truncated from the right when the
        window is narrow (the convention the tmux-status-line research already used: most
        important on the left, because truncation eats from the far end)."""
        chrome = theme_mod.TOKENS["chrome"]
        dim = theme_mod.TOKENS["dim"]
        rows = self.sup.rows
        tally = _status_tally(rows)

        line = Text()
        line.append(" ▰▰ PANTHEON ", style=f"bold {chrome}")
        line.append("│", style=dim)
        # The other two windows and the key that reaches each. F1 is this window.
        for key, name in (("F2", "queue"), ("F3", "budget"), ("n", "new session")):
            line.append(f" {key} ", style=f"bold {chrome}")
            line.append(name, style=dim)
        line.append("  │ ", style=dim)
        if tally["need_you"]:
            line.append(f" {tally['need_you']} need you ", style=f"bold black on {theme_mod.TOKENS['warning']}")
            line.append(" ")
        if tally["failed"]:
            line.append(f" {tally['failed']} failed ", style=f"bold white on {theme_mod.TOKENS['error']}")
            line.append(" ")
        line.append(f" {tally['working']} working ", style=f"bold black on {chrome}")
        dim_bits = [f"{tally[k]} {word}" for k, word in
                    (("parked", "parked"), ("idle", "idle"), ("quiet", "quiet"), ("done", "done")) if tally[k]]
        if dim_bits:
            line.append("  " + " · ".join(dim_bits), style=dim)

        picture = getattr(self.strip, "picture", None) or {}
        right = Text()
        right.append_text(_pct_segment("claude", (picture.get("claude") or {}).get("five_hour_pct"), dim))
        right.append(" · ", style=dim)
        right.append_text(_pct_segment("codex", (picture.get("codex") or {}).get("five_hour_pct"), dim))
        right.append(" · ", style=dim)
        clock = format_clock(datetime.now().astimezone(), self.cfg)
        right.append(f"tmux {self.cfg.tmux_session} · {len(rows)} agents · {clock} ", style=dim)

        # `#deck-header` has `padding: 0 1`, so its own content box is 2 columns narrower than
        # the app's width; `text-overflow: clip` (CSS, above) is the safety net if this still
        # runs long, but subtracting the padding here keeps the clock from losing a digit to it.
        content_width = max(0, self.size.width - 2)
        pad = max(1, content_width - len(line.plain) - len(right.plain)) if content_width > 0 else 1
        line.append(" " * pad)
        line.append_text(right)
        return line

    def _repaint_status_right(self) -> None:
        """The status row's right side: standing context that never
        goes blank -- `live: tmux · vault read HH:MM · usage read HH:MM`. Desk-only (see
        `_apply_width`): the phone status row stays exactly the one hand-written message it
        always was, so the 65x26 render's characters do not change."""
        if not self._wide:
            return
        dim = theme_mod.TOKENS["dim"]
        live = bool(getattr(self.sup, "live_tmux", False))
        right = Text()
        right.append("live: tmux" if live else "no tmux",
                      style=theme_mod.TOKENS["success"] if live else dim)
        last_read_at = getattr(self.queue, "last_read_at", None)
        if last_read_at is not None:
            right.append(" · vault read " + format_clock(last_read_at, self.cfg), style=dim)
        picture = getattr(self.strip, "picture", None) or {}
        when = parse_ts(picture.get("fetched_at")) if picture else None
        if when is not None:
            right.append(" · usage read " + format_clock(when, self.cfg), style=dim)
        self.query_one("#deck-status-right", Static).update(right)

    def _say(self, message: str, tone: Optional[str] = None) -> None:
        self._message = message
        text = Text(message)
        if tone:
            text.stylize(theme_mod.TOKENS.get(tone, ""))
        self.query_one("#deck-status", Static).update(text)

    # ------------------------------------------------------------------ keys

    def _panes(self) -> list:
        # Left to right, exactly as they are drawn: SESSIONS -> the agents table -> QUEUE,
        # skipping whichever are not on screen right now.
        return [p for p in (self.rail, self.sup, self.queue) if p.display]

    def _focus_pane(self, pane) -> None:
        if pane in (self.sup, self.rail):
            pane.focus_table()
        else:
            pane.focus()

    def _focus_is_in(self, pane) -> bool:
        focused = self.focused
        if focused is None:
            return False
        return focused is pane or focused in list(pane.walk_children(with_self=True))

    def _pane_containing_focus(self, panes: list) -> int:
        """Which of `panes` the currently focused widget lives inside; falls back to the last
        pane when focus is somewhere else entirely (or nothing is focused yet)."""
        if self.focused is not None:
            for i, pane in enumerate(panes):
                if self.focused is pane or self.focused in pane.walk_children(with_self=True):
                    return i
        return len(panes) - 1

    def action_next_pane(self) -> None:
        panes = self._panes()
        if len(panes) < 2:
            return
        current = self._pane_containing_focus(panes)
        self._focus_pane(panes[(current + 1) % len(panes)])

    def action_prev_pane(self) -> None:
        panes = self._panes()
        if len(panes) < 2:
            return
        current = self._pane_containing_focus(panes)
        self._focus_pane(panes[(current - 1) % len(panes)])

    def action_toggle_queue(self) -> None:
        """`t`: the task list, back as a right-hand column (and away again)."""
        if self._mode != DESK:
            return self._say("the task list is F2 on the phone", "warning")
        self._queue_open = not self._queue_open
        self._apply_panes()
        if self._queue_open:
            self.queue.focus()
            self._say("task list open; t closes it again")
        else:
            self._focus_default()
            self._say("task list hidden; t brings it back, F2 opens it in its own window")

    # ------------------------------------------------------------------ the stage

    def _projects(self) -> list:
        """Every project the `n` picker knows, most recent first -- re-scanned at most every
        30 seconds (it walks the workspace folder)."""
        import time as _time

        stamp, cached = self._projects_cache
        now = _time.monotonic()
        if cached and now - stamp < PROJECTS_CACHE_SECONDS:
            return cached
        try:
            projects = projects_mod.sort_projects(
                projects_mod.list_projects(self.cfg, getattr(self.sup, "_events", []) or []), "recent")
        except Exception as exc:
            log.debug("project list failed: %r", exc)
            projects = []
        self._projects_cache = (now, projects)
        return projects

    def _sync_stage(self, verify: bool = False, relayout: bool = True) -> None:
        """Read what is staged (asking tmux only when something is, and only with `verify`), tell
        the sidebar, and switch layout when that changed."""
        staged = stage_mod.current(self.cfg, verify=verify)
        changed = (staged is None) != (self._staged is None) or (
            staged is not None and self._staged is not None and staged.pane_id != self._staged.pane_id)
        self._staged = staged
        # the editor pane counts as "something beside the list" too. One tmux question, and
        # only while an editor is open; tmux not answering keeps what we last knew.
        found = editor_mod.where(self.cfg)
        beside = self._editor_beside if (found and found["window_index"] is None) else bool(
            found and found["beside"])
        changed = changed or beside != self._editor_beside
        self._editor_beside = beside
        self.rail.set_stage(staged is not None, staged.session_id if staged else "",
                            staged.pane_id if staged else "", editor_open=beside)
        if changed and relayout:
            self._apply_width(self.size.width)

    def _repaint_stage_usage(self) -> None:
        """Two lines while staged: `claude  5h 42% · week 18%`, the same for codex, from the
        picture the header already reads (`HudStrip.picture`)."""
        dim = theme_mod.TOKENS["dim"]
        picture = getattr(self.strip, "picture", None) or {}
        text = Text(no_wrap=True, overflow="ellipsis")
        for i, name in enumerate(("claude", "codex")):
            usage = picture.get(name) or {}
            if i:
                text.append("\n")
            text.append(f"{name:<7}", style=f"bold {theme_mod.TOKENS['chrome']}")
            for label, key in (("5h", "five_hour_pct"), ("week", "seven_day_pct")):
                if label == "week":
                    text.append(" · ", style=dim)
                text.append(f"{label} ", style=dim)
                pct = usage.get(key)
                if isinstance(pct, (int, float)):
                    text.append(f"{pct:.0f}%", style=theme_mod.gauge_colour(pct))
                else:
                    text.append("—", style=dim)
        self.stage_usage.update(text)

    def on_session_sidebar_activated(self, event) -> None:
        """A click (or Enter, or 1-9) on a session: its real terminal. Running in this tmux ->
        staged beside the deck (or, on a narrow window, switched to). Anywhere else -> resumed
        in a new window, then staged. THIS is the "I can't jump to this session" fix."""
        event.stop()
        entry = event.entry
        if self.rail.is_on_stage(entry):
            return self._show_session(stage_mod.DECK_WINDOW, entry.session_id)
        if entry.group == session_models.LIVE and entry.window_index is not None:
            other = entry.tmux_session or self.cfg.tmux_session
            base = other.rsplit("-", 1)[0] if other.rsplit("-", 1)[-1].isdigit() else other
            if base != self.cfg.tmux_session:
                return self._say(OTHER_TMUX.format(name=other), "warning")
            return self._show_session(entry.window_index, entry.session_id)
        if entry.provider != "claude":
            return self._say(CODEX_NO_RESUME, "warning")
        if entry.group == session_models.LIVE:
            # Hooks say it is running, but not in any tmux: open in the Desktop app.
            return self.push_screen(
                Confirm(CONFIRM_LIVE_ELSEWHERE, yes_label="Yes, resume here (y)",
                        body=CONFIRM_LIVE_ELSEWHERE_BODY),
                lambda sure: self._resume(entry) if sure else self._say("nothing started"))
        self._resume(entry)

    def _show_session(self, window_index: int, session_id: str = "") -> None:
        self.run_worker(lambda: self._stage_worker(window_index, session_id), thread=True)

    def _stage_worker(self, window_index: int, session_id: str) -> None:
        try:
            result = stage_mod.stage(self.cfg, window_index, session_id=session_id)
            if result.get("action") == "staged" and editor_mod.is_beside(self.cfg):
                editor_mod.rebalance(self.cfg)   # the session and the editor share the space
        except Exception:
            log.exception("staging window %s failed", window_index)
            result = {"ok": False, "action": "none",
                      "message": f"could not show that session; details are in {self.cfg.log_file}"}
        self.call_from_thread(self._after_stage, result)

    def _after_stage(self, result: dict) -> None:
        self._sync_stage()
        self._say(result.get("message", ""), None if result.get("ok") else "warning")

    def _resume(self, entry) -> None:
        provider = (getattr(self.sup, "providers", None) or {}).get("claude")
        if provider is None:
            return self._say("claude is not switched on in pantheon.toml", "warning")
        if not entry.cwd:
            return self._say("no folder is recorded for that conversation", "warning")
        self._say(f"reopening {entry.title or entry.project} in {entry.project}...")
        self.run_worker(lambda: self._resume_worker(entry, provider), thread=True)

    def _resume_worker(self, entry, provider) -> None:
        try:
            result = projects_mod.start_session(entry.cwd, self.cfg, provider, True, "",
                                                {"resume": entry.session_id})
        except Exception:
            log.exception("resume failed")
            self.call_from_thread(self._say, f"could not reopen it; details are in {self.cfg.log_file}",
                                  "error")
            return
        self.call_from_thread(self._after_resume, result, entry)

    def _after_resume(self, result, entry) -> None:
        # A window that came up asking to trust the folder is shown too: the answer is typed there.
        if result.window_index is not None and self._mode != PHONE:
            self._show_session(result.window_index, entry.session_id)
        self._say(result.message or ("reopened" if result.ok else "that conversation did not reopen"),
                  None if result.ok else "warning")
        self.sup.refresh_rows()

    def on_session_sidebar_approval(self, event) -> None:
        """`a`/`d`/`v` on a session in the SESSIONS list: the same
        answer path as the agents table, so every check (fresh read, same prompt, one key) holds."""
        event.stop()
        self.sup.approval_key(event.session_id, event.key, event.width)
        if not self.sup.display and self.sup._message:
            # A session is staged beside the list, so the table's footer is hidden: say it here.
            self._say(self.sup._message, self.sup._message_role)
        self.rail.refresh_rows()
    # ------------------------------------------------------------------ the Assistant

    def _assistant_view(self):
        """The sidebar's `◆ Assistant` line: learns the conversation id off the (cached) event
        log, then reads what the supervisor already folded -- no tmux call of its own."""
        try:
            if not self.cfg.assistant_settings().enabled:
                return None
            assistant_mod.learn(self.cfg)
            lost = getattr(self.sup, "link_lost", None)
            self._assistant_last = assistant_mod.view(
                self.cfg, self.sup.rows, getattr(self.sup, "windows", []),
                is_starting=self._assistant_starting, link_lost=lost if callable(lost) else None)
            return self._assistant_last
        except Exception:
            log.exception("the Assistant line failed")
            return None

    def _assistant_attention(self) -> list:
        view = getattr(self, "_assistant_last", None)
        if view is not None and getattr(view, "link_lost", False):
            return [assistant_mod.ATTENTION_PHONE_OFF]
        return []

    def on_session_sidebar_assistant_reconnect(self, event) -> None:
        event.stop()
        self.action_assistant_reconnect()

    def action_assistant_reconnect(self) -> None:
        """`L` or the `↻ reconnect` line: type `/remote-control` into the Assistant, only after a
        fresh screen read shows it idle at an empty prompt with its phone link down."""
        if not self.cfg.assistant_settings().enabled:
            return self._say(assistant_mod.MSG_OFF, "warning")
        if self._assistant_reconnecting:
            return self._say("already reconnecting the Assistant's phone link")
        self._assistant_reconnecting = True
        self._say("reconnecting the Assistant's phone link...")
        self.run_worker(self._assistant_reconnect_worker, thread=True)

    def _assistant_reconnect_worker(self) -> None:
        try:
            result = assistant_mod.reconnect(self.cfg)
        except Exception:
            log.exception("reconnecting the Assistant failed")
            result = {"ok": False, "message": f"could not reconnect; details are in {self.cfg.log_file}"}
        self.call_from_thread(self._after_assistant_reconnect, result)

    def _after_assistant_reconnect(self, result: dict) -> None:
        self._assistant_reconnecting = False
        if "link_lost" in result:
            pane = assistant_mod.read_state(self.cfg).get("pane_id") or ""
            scanner = getattr(self.sup, "_dialog_scanner", None)
            if pane and scanner is not None:
                scanner.set_link_lost(pane, bool(result["link_lost"]))
        self._say(result.get("message", ""), None if result.get("ok") else "warning")
        try:
            self.rail.redraw()
        except Exception:
            pass

    def on_session_sidebar_assistant_chosen(self, event) -> None:
        event.stop()
        self.action_assistant()

    def action_assistant(self) -> None:
        """`◆ Assistant` or `A`: on screen beside the list when it runs; started first (resuming
        its conversation) when it does not."""
        if not self.cfg.assistant_settings().enabled:
            return self._say(assistant_mod.MSG_OFF, "warning")
        view = self._assistant_view()
        if view is not None and view.running:
            return self._show_session(view.window_index, view.session_id)
        if self._assistant_starting:
            return self._say(assistant_mod.MSG_STARTING)
        self._say("starting the Assistant...")
        self.run_worker(lambda: self._assistant_worker(stage_after=True), thread=True)

    def _assistant_worker(self, stage_after: bool) -> None:
        def on_starting() -> None:
            self.call_from_thread(self._assistant_set_starting, True)

        provider = (getattr(self.sup, "providers", None) or {}).get("claude")
        try:
            result = assistant_mod.ensure(self.cfg, provider=provider, on_starting=on_starting)
        except Exception:
            log.exception("the Assistant did not start")
            result = {"ok": False, "action": "failed",
                      "message": f"the Assistant did not start; details are in {self.cfg.log_file}"}
        self.call_from_thread(self._after_assistant, result, stage_after)

    def _assistant_set_starting(self, value: bool) -> None:
        self._assistant_starting = value
        if self.rail.is_mounted:
            self.rail.redraw()

    def _after_assistant(self, result: dict, stage_after: bool) -> None:
        self._assistant_set_starting(False)
        action = result.get("action")
        if stage_after and result.get("ok") and result.get("window_index") is not None and self._mode != PHONE:
            self._show_session(int(result["window_index"]), str(result.get("session_id") or ""))
        # On a plain deck start a running Assistant says nothing; a start, a fresh conversation
        # or a failure is news.
        if result.get("message") and (stage_after or action not in ("running", "none", "starting")):
            self._say(result["message"], None if result.get("ok") else "warning")
        self.sup.refresh_rows()

    def on_session_sidebar_new_session(self, event) -> None:
        event.stop()
        self.action_new_session()

    def on_session_sidebar_home(self, event) -> None:
        event.stop()
        self.action_unstage()

    def on_session_sidebar_project_chosen(self, event) -> None:
        """A project heading: New Claude session / New Codex job / Resume / Open folder, each
        entering the existing new-session steps with the folder already picked."""
        event.stop()
        name, path = event.name, event.path
        options = [PickOption(*o) for o in PROJECT_MENU]
        self.push_screen(Pick(PROJECT_MENU_TITLE.format(name=name), options),
                         lambda choice: self._project_picked(name, path, choice))

    def _project_picked(self, name: str, path: str, choice: Optional[str]) -> None:
        if not choice:
            return
        if choice == "folder":
            index = tmuxctl.open_or_reuse_shell(self.cfg.tmux_session, path, tmux=self.cfg.tools.tmux)
            return self._say(f"shell window is now in {name}" if index is not None
                             else "could not open the shell window", None if index is not None else "warning")
        self.sup._ns = {"project_dir": path}
        self.sup._ns_got("who", PROJECT_MENU_WHO[choice])

    def on_supervisor_pane_session_started(self, event) -> None:
        """A session the new-session steps just opened goes straight beside the deck (desk
        only: on the phone `n` never moved the viewer, and still does not)."""
        event.stop()
        if event.window_index is not None and self._mode in (DESK, STAGED):
            self._show_session(event.window_index)

    def action_unstage(self) -> None:
        """`b` / `◀ back`: the staged session goes back to its own window, still running, and an
        open editor to its own `EDIT` window with its text."""
        if self._staged is None and not self._editor_beside:
            return self._say("nothing is on screen beside the deck")
        self.run_worker(self._unstage_worker, thread=True)

    def _unstage_worker(self) -> None:
        try:
            result = stage_mod.release_all(self.cfg)
        except Exception:
            log.exception("release failed")
            result = {"ok": False, "message": f"could not put it back; details are in {self.cfg.log_file}"}
        self.call_from_thread(self._after_stage, result)

    # ------------------------------------------------------------------ the editor pane

    def on_session_sidebar_edit_file(self, event) -> None:
        event.stop()
        self._edit(event.folder)

    def action_edit_file(self) -> None:
        """`E` (and `e` / `✎ Edit a file` in the list): a text editor beside the list. The task
        list's highlighted row names its own note file, so from there nothing is asked; anywhere
        else the one question is which file, looked for in the highlighted session's folder."""
        if self.queue.display and self._focus_is_in(self.queue):
            row = self.queue.selected_row()
            path = getattr(row, "path", None) if row is not None else None
            if path and Path(path).is_file():
                return self._edit("", known_file=str(path))
        self._edit(self._edit_folder())

    def _edit_folder(self) -> str:
        if self.rail.display:
            entry = self.rail.selected()
            if entry is not None and entry.cwd:
                return str(entry.cwd).replace("\\", "/")
            if self.rail.context_folder:
                return self.rail.context_folder
        return str(self.cfg.projects_root)

    def _edit(self, folder: str, known_file: str = "") -> None:
        folder = folder or str(self.cfg.projects_root)
        if editor_mod.where(self.cfg) is not None:
            # Already open (beside the list, or parked in its own window): focus it or bring it
            # back -- never a second editor.
            return self._run_edit("", "")
        if known_file:
            return self._run_edit(known_file, str(Path(known_file).parent))

        def typed(text: Optional[str]) -> None:
            path = editor_mod.resolve_path(text or "", folder)
            if not path:
                return self._say(EDIT_NOTHING)
            self._run_edit(path, folder)

        self.push_screen(TextPrompt(EDIT_PROMPT_TITLE,
                                    placeholder=EDIT_PROMPT_PLACEHOLDER.format(folder=folder),
                                    hint=EDIT_PROMPT_HINT), typed)

    def _run_edit(self, path: str, cwd: str) -> None:
        self.run_worker(lambda: self._edit_worker(path, cwd), thread=True)

    def _edit_worker(self, path: str, cwd: str) -> None:
        try:
            result = editor_mod.open_beside_stage(self.cfg, path, cwd=cwd)
        except Exception:
            log.exception("opening the editor failed")
            result = {"ok": False, "action": "none",
                      "message": f"could not open the editor; details are in {self.cfg.log_file}"}
        self.call_from_thread(self._after_stage, result)

    def _render_keys_page(self) -> None:
        """Page 0 is the key list; page 1 is the connector gap list. Read
        fresh each time it opens: a session that just ran `claude mcp add` should not have to
        restart the deck to see it."""
        nav = self.query_one("#deck-keys-nav", Static)
        body = self.query_one("#deck-keys-body", Static)
        if self._keys_page == 0:
            # Escaped brackets (`\[...]`) so Rich renders the literal word `[connectors]` inside
            # the `@click` action span, instead of trying to parse it as another tag. The action
            # name after `app.` is bare (`show_connectors`, not `action_show_connectors`) --
            # Textual's own action dispatch adds that prefix itself.
            nav.update(
                "[@click=app.show_connectors]\\[connectors][/]  "
                "[@click=app.show_tools]\\[tools][/]"
            )
            body.update(keys_mod.KEYS_TEXT)
        elif self._keys_page == 1:
            nav.update("")
            body.update(connectors_mod.report(str(Path.cwd())))
        else:
            # third page: the same read-only text `pantheon --tools` shows, sorted by name
            # (no filter box in the overlay -- that lives in the standalone window).
            nav.update("")
            body.update(tools_report(self.cfg))

    def action_toggle_keys(self) -> None:
        """`?`: open to page 0 (the keys), press again for page 1 (connectors), again for page 2
, a fourth press closes -- the same cycle the standalone
        supervisor's own overlay uses, now three pages deep."""
        panel = self.query_one("#deck-keys", VerticalScroll)
        if not panel.display:
            panel.display = True
            self._keys_page = 0
        elif self._keys_page < 2:
            self._keys_page += 1
        else:
            panel.display = False
            self._keys_page = 0
        self.query_one("#panes", Horizontal).display = not panel.display
        self.strip.display = (not panel.display) and bool(self._wide)
        self._render_keys_page()
        if panel.display:
            panel.focus()
        else:
            self._focus_default()

    def action_new_session(self) -> None:
        self.sup.action_new_session()

    # ------------------------------------------------------------------ the pit

    def action_toggle_pit(self) -> None:
        """`P`: join every agent window's pane into one tiled `PIT` window (or, already in it,
        break them back out) -- real tmux terminals, not a widget.
        Runs in a thread: it is several tmux subprocess calls, same reasoning as `n`'s launch."""
        if self._mode == PHONE and not tmux_pit.is_active(self.cfg):
            # acceptance 5 / S-UX R1: too narrow to tile side by side is too narrow to enter
            # at all -- leaving is always allowed (a window that got narrow after entering must
            # still be able to get back out).
            return self._say("the pit needs desk width (120+ columns) to tile side by side", "warning")
        entering = not tmux_pit.is_active(self.cfg)
        self._say("entering the pit..." if entering else "leaving the pit...")
        self.run_worker(self._toggle_pit_worker, thread=True)

    def _toggle_pit_worker(self) -> None:
        try:
            if not tmux_pit.is_active(self.cfg):
                # a staged session sits in window 0, where the pit never looks -- put it
                # back in its own window first so it is tiled with the rest.
                stage_mod.release(self.cfg)
                self.call_from_thread(self._sync_stage)
                result = tmux_pit.enter_pit(self.cfg, tmux=self.cfg.tools.tmux)
                if result["pit_index"] is not None:
                    tmuxctl.select_window(self.cfg.tmux_session, result["pit_index"], self.cfg.tools.tmux)
                joined = len(result["joined"])
                message = f"pit: {joined} agent{'s' if joined != 1 else ''} tiled together" if joined \
                    else "no agent windows to tile"
            else:
                restored = tmux_pit.leave_pit(self.cfg, tmux=self.cfg.tools.tmux)
                tmuxctl.select_window(self.cfg.tmux_session, 0, self.cfg.tools.tmux)
                message = f"pit: {len(restored)} window{'s' if len(restored) != 1 else ''} restored" if restored \
                    else "left the pit"
        except Exception:
            log.exception("the pit toggle failed")
            message = f"the pit did not toggle cleanly; details are in {self.cfg.log_file}"
        self.call_from_thread(self._say, message)

    def action_show_connectors(self) -> None:
        """The `[connectors]` clickable word at the top of page 0: jumps straight to page 1,
        the same place a second `?` press lands."""
        panel = self.query_one("#deck-keys", VerticalScroll)
        if not panel.display:
            return
        self._keys_page = 1
        self._render_keys_page()

    def action_show_tools(self) -> None:
        """The `[tools]` clickable word at the top of page 0: jumps straight to page 2, the
        read-only skills/hooks/guards inventory."""
        panel = self.query_one("#deck-keys", VerticalScroll)
        if not panel.display:
            return
        self._keys_page = 2
        self._render_keys_page()

    def action_close_keys(self) -> None:
        """Esc: closes the overlay outright from either page (no per-page back step)."""
        panel = self.query_one("#deck-keys", VerticalScroll)
        if not panel.display:
            return
        panel.display = False
        self._keys_page = 0
        self.query_one("#panes", Horizontal).display = True
        self.strip.display = bool(self._wide)
        self._focus_default()

    def check_action(self, action: str, parameters: tuple) -> Optional[bool]:
        """`n` is a priority binding (it has to beat the tables), so without this it also fired
        inside every open confirm: `n` for "No" opened the new-session picker ON TOP of the
        still-open question instead of answering it. While a
        modal is up, `n` belongs to the modal."""
        if action == "new_session" and isinstance(self.screen, ModalScreen):
            return False
        return True

    def action_restart(self) -> None:
        """`R`: restart the deck, queue and budget windows, or everything, from here --
        pantheon/restart.py owns the screens and the tmux call."""
        restart_mod.ask(self, self.cfg, self._say)

    def action_quit_deck(self) -> None:
        panel = self.query_one("#deck-keys", VerticalScroll)
        if panel.display:
            return self.action_close_keys()
        # a staged session would be left alone in window 0 under the deck's name; put it
        # back in its own window first (on this thread: the deck is about to exit anyway).
        try:
            stage_mod.release_all(self.cfg)   # the staged session AND an open editor
        except Exception:
            log.exception("release before quit failed")
        self.exit()


def main() -> int:
    cfg = config_mod.load()
    try:
        config_mod.ensure_state_dirs(cfg)
        logging.basicConfig(filename=str(cfg.log_file), level=logging.INFO,
                            format="%(asctime)s %(levelname)s %(name)s %(message)s")
    except OSError:
        pass
    app = DeckApp(cfg)
    if not Path(cfg.sprints_dir).is_dir():
        app.queue.message = f"vault folder not found at {cfg.sprints_dir}; fix it in pantheon.toml"
    try:
        app.run()
    except Exception:
        log.exception("the deck stopped")
        print(f"the deck stopped; details are in {cfg.log_file}", file=sys.stderr)
        return 1
    # `q` quits at once (no confirm); this line, shown above the launcher's own "press Enter to
    # close", is where it says what it quit.
    print(restart_mod.QUIT_NOTICE)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
