"""The agents pane: one list of every agent that is running, and which of them wants the user.

This is a widget, not an app, so the same list can be a whole tmux window on the phone or one
column of the wide desk layout later. Everything here measures the PANE's own width, never the
terminal's, which is what makes that second life possible.

Design rules that bind here: an agent that is simply working gets
no colour at all; an agent waiting on a human is steady amber with the words beside it, never
red and never a count of "pending approvals"; the colour is never the only signal, so the
state word is always printed; on a narrow screen whole columns are dropped rather than
squeezed, and the state column is never one of them; nothing moves on its own -- no spinners,
no blinking, only the age number ticking up. Keys are in-app and shell out to tmux, because
the tmux prefix key does not survive the phone.
"""
from __future__ import annotations

import functools
import subprocess
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from rich.text import Text
from textual import events as tevents
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.widget import Widget
from textual.widgets import DataTable, Static

from .. import assistant as assistant_mod
from .. import config as config_mod
from .. import connectors as connectors_mod
from .. import dialogs as dialogs_mod
from .. import events as events_mod
from .. import eventcache as eventcache_mod
from .. import glyphs as glyphs_mod
from .. import keys as keys_mod
from .. import model_picker
from .. import session_ctl
from .. import theme as theme_mod
from .. import tmuxctl
from .. import transcripts as transcripts_mod
from ..governor import handoff as handoff_mod
from ..hud import sources as hud_sources
from ..notify import startup_dialogs as dialog_notify
from ..models import AgentState, AgentStatus, Event, TmuxWindow, format_age, utcnow_iso
from ..providers import get_providers
from ..widgets.modal import Confirm, LaunchOptions, Pick, PickOption, ProjectPicker, TextPrompt, numbered
from ..dispatch import projects as projects_mod
from ..dispatch import restart as restart_mod
from ..dispatch import sessions as sessions_mod
from ..widgets.toolbar import Action, RowToolbar
from ..session_view import approval as approval_mod
from . import approvals as approvals_mod
from . import state as state_mod

# Width thresholds, checked against the PANE's own content width. Textual has no breakpoints, so this is
# by hand: drop whole columns, never shrink them, and never drop `state`.
#  names four tiers -- P/N/D/W. P (<80) and N (80-87) keep the
# layouts they always had; D (88-116) and W (117+) are new, sized for a panel, not a monitor --
# `session`/`folder` leave the table for both (the detail panel below shows them instead).
NARROW_AT = 80   # below this it is the phone layout (P); unchanged, the phone tier is never moved
WIDE_AT = 88     # at or above this the desk layout (D)
SUPER_WIDE_AT = 117  # at or above this, model + mode join the desk layout (W; S-UI section 4.1)

# Column layouts: (key, label, content width). The DataTable adds one space of padding either side
# of every column, so a layout costs `sum(widths) + 2 * len(columns)` cells on screen.
#
# `state` is 23 wide at every size because the longest label ("blocked - permission" behind its
# `!!` marker, "waiting - needs input") has to fit -- the words are the signal, not the colour.
# `where` says which tmux window the agent is in, or `no window` when it has none (the Claude
# Desktop app, or a headless Codex job keeps its own word) -- the row toolbar drops Jump/Kill
# entirely for those rows, so this is what explains why.
PHONE_COLUMNS = (          # fits 65 columns: 57 + 6 = 63
    ("state", "state", 23),
    ("project", "project", 22),
    ("where", "where", 12),
)
NARROW_COLUMNS = (         # fits 80 columns: 69 + 10 = 79
    ("state", "state", 23),
    ("project", "project", 14),
    ("where", "where", 12),
    ("action", "last action", 14),
    ("age", "age", 6),
)
WIDE_COLUMNS = (           # D tier, fits 88 columns: 78 + 10 = 88
    ("state", "state", 23),
    ("project", "project", 18),
    ("where", "where", 12),
    ("action", "last action", 20),
    ("age", "age", 5),
)

SUPER_WIDE_COLUMNS = (     # W tier, fits 117 columns: 103 + 14 = 117 -- `last action` widens too
    ("state", "state", 23),
    ("project", "project", 18),
    ("where", "where", 12),
    ("action", "last action", 24),
    ("age", "age", 5),
    ("model", "model", 10),
    ("mode", "mode", 11),
)

# The detail panel's `folder` line is not a table column, so it is not width-locked like one --
# this just keeps one very long path from making that line absurd on a narrow-but-still-D pane.
DETAIL_FOLDER_WIDTH = 70

# Same reasoning for the transcript-derived "last message" line a Desktop-app row shows in place
# of the event log's "last five actions".
DETAIL_TEXT_WIDTH = 70

# What the footer says when the selected agent is not in any tmux at all -- the Claude Desktop app,
# or a plain terminal window. Amber, and it stays put until the next key, because the quiet
# version of this sentence was missed on the phone.
NOT_IN_TMUX_JUMP = "that agent is not in any tmux (it lives on the desktop) - nothing to jump to"
NOT_IN_TMUX_KILL = "that agent is not in any tmux (it lives on the desktop) - nothing to close"
OTHER_TMUX_KILL = "that agent is in another tmux session - close it from there"
NOT_TYPEABLE = "that agent is not a live Claude Code window in this tmux - nothing was typed"

EFFORT_CHOICES = [("d", "default", "keep the session's default effort"), ("l", "low", ""), ("m", "medium", ""),
                  ("h", "high", ""), ("x", "xhigh", ""), ("M", "max", "")]
MODE_CHOICES = [("d", "default", "keep the permission mode"), ("a", "auto", ""), ("x", "acceptEdits", ""),
                ("p", "plan", "")]
# `/effort <level>` -- Desktop's own five names.
EFFORT_LEVELS = ["low", "medium", "high", "xhigh", "max"]
# The Shift+Tab cycle (`session_ctl.MODE_CYCLE`), with a mnemonic letter apiece.
MODE_OPTIONS = [("a", "auto"), ("d", "default"), ("x", "acceptEdits"), ("p", "plan")]
# Codex's switches for a new session.
# `codex exec -m <model> -c model_reasoning_effort=<effort> -s <sandbox>`; the models, and the
# Claude ones, live in `pantheon/model_picker.py` (family first, then version, recent first).
# gpt-6-astra's accepted range, verbatim from the API's own 400 on `minimal`: "Supported values are: 'low', 'medium', 'high', 'xhigh', and 'max'."
CODEX_EFFORTS = ["low", "medium", "high", "xhigh", "max"]
CODEX_SANDBOXES = [("w", "workspace-write"), ("r", "read-only"), ("f", "danger-full-access")]
CODEX_EFFORT_CHOICES = [("d", "default", "the effort in ~/.codex/config.toml")] + [
    ({"xhigh": "x", "max": "M"}.get(e, e[0]), e, "") for e in CODEX_EFFORTS]
CODEX_SANDBOX_CHOICES = [("d", "default", "workspace-write")] + [(k, v, "") for k, v in CODEX_SANDBOXES]

CONFIRM_POLL_SECONDS = 2
CONFIRM_TIMEOUT_SECONDS = 30

# section 3 (`H`, hand off to a fresh session in the same folder): the rows it is offered on,
# and how often the deck looks for the old session's checkpoint. Not on a row blocked on a
# permission question -- typed text would land in that question, not the prompt.
HANDOFF_STATES = {AgentStatus.WORKING, AgentStatus.WAITING_INPUT, AgentStatus.IDLE, AgentStatus.QUIET}
HANDOFF_WRONG_ROW = "hand off here is for a Claude session that is working, waiting or idle"
HANDOFF_POLL_SECONDS = 3.0


def _checkpoint_mtime(cwd: Optional[str]) -> Optional[datetime]:
    """When the folder's `CHECKPOINT.md` was last written (UTC), or None if it has none."""
    if not cwd:
        return None
    try:
        return datetime.fromtimestamp((Path(cwd) / "CHECKPOINT.md").stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None

# The toolbar action catalogue (S-UI section 4.4): key, label, the `action_<name>` this dispatches
# to, and its button variant. `hand_off` exists because S-UI puts it on the toolbar, but (the
# governor) is a separate, parallel build -- until it exists this is always disabled, never
# actually dispatched. `resume_now` doesn't: resume ladder runs on its own
# 60-second loop with no "resume this one row now" call the deck could invoke, so a button here
# could never do anything but explain that -- copy review 2026-09-02 "interaction issue 2": a
# control that can never be pressed teaches nothing, so it is hidden rather than shown disabled
# until the governor actually grows that single-row resume.
ACTION_DEFS: dict[str, Action] = {
    #: answer a permission prompt from the deck. `a` types the dialog's
    # own plain "Yes" (never "and don't ask again") after a fresh read of the window; a cut line
    # opens the full view first. `d` types its "No". `v` shows the whole request word for word.
    "allow": Action("a", "Allow once", "allow", "warning"),
    "deny": Action("d", "Deny", "deny"),
    "view": Action("v", "View", "view_request"),   # the whole request, word for word
    "answer": Action("j", "Answer", "jump", "primary"),
    "jump": Action("j", "Jump", "jump", "primary"),
    "kill": Action("k", "Kill", "kill", "error"),
    # ask this session to /checkpoint, then open the New-session steps for the same
    # folder. Capital H: lower-case `h` is the cross-provider hand-off below, `R` restarts the deck.
    "handoff": Action("H", "Hand off…", "handoff", "warning"),
    "model": Action("m", "Model…", "model"),
    "effort": Action("e", "Effort…", "effort"),
    "mode": Action("p", "Mode…", "mode"),
    "folder": Action("o", "Folder", "open_folder"),
    "copy_id": Action("c", "Copy id", "copy_id"),
    "hand_codex": Action("h", "Hand to Codex…", "hand_off", "error"),
    "hand_claude": Action("h", "Hand to Claude…", "hand_off", "error"),
    "log": Action("l", "Log", "log"),
    # Not a row action: the standing "New session" button, first on the bar
    # whether or not a row is selected.
    "new": Action("n", "New session…", "new_session", "primary"),
}
NEW_SESSION_ACTION = ACTION_DEFS["new"]

# Who a fresh session goes to (the queue's `x` picker, minus the local model): `c` Claude in
# this tmux, `i` Codex in a window on the PC, `x` a headless Codex job (needs a first message).
NEW_SESSION_WHO = [
    PickOption("c", "claude", "Claude in tmux · the usual"),
    PickOption("r", "claude-resume", "Claude in tmux · resume a session in that folder"),
    PickOption("i", "codex-pc", "Codex in a PC window · not on the phone"),
    PickOption("x", "codex-headless", "Codex headless job · needs a first message"),
]
# The phone modal is 50 columns (~44 usable); the two long labels above clip into unreadable
# fragments there. Below DESK_AT the who step swaps in these short forms -- same keys, same values.
NEW_SESSION_WHO_PHONE_LABELS = {
    "claude": "Claude · the usual",
    "claude-resume": "Claude · resume in this folder",
    "codex-pc": "Codex PC window · desk only",
    "codex-headless": "Codex headless · type a task",
}
NEW_SESSION_VALUES: dict[str, tuple[str, Optional[bool]]] = {
    "claude": ("claude", None), "claude-resume": ("claude", None),
    "codex-pc": ("codex", True), "codex-headless": ("codex", False),
}

# Which of the catalogue above shows up for each row state (S-UI section 4.2's "Toolbar buttons
# offered" column). Codex rows (running/queued/failed/done) never get model/effort/mode -- D9.
ROW_ACTIONS: dict[AgentStatus, list[str]] = {
    #: a blocked row answers its prompt (a/d/v) or jumps to it. No
    # model/effort/mode here: those TYPE into the window, and with a permission dialog up a typed
    # digit is an answer to it.
    AgentStatus.BLOCKED_PERMISSION: ["allow", "deny", "view", "answer", "kill", "folder", "copy_id"],
    AgentStatus.WAITING_INPUT: ["answer", "kill", "model", "effort", "mode", "folder", "copy_id", "handoff"],
    AgentStatus.FAILED: ["hand_claude", "log", "copy_id"],
    AgentStatus.WORKING: ["jump", "kill", "model", "effort", "mode", "folder", "copy_id", "handoff"],
    AgentStatus.RUNNING: ["jump", "log", "copy_id"],
    # A quiet row was working a moment ago and could still be sitting right where it left off --
    # same buttons as `working`.
    AgentStatus.QUIET: ["jump", "kill", "model", "effort", "mode", "folder", "copy_id", "handoff"],
    AgentStatus.WINDING_DOWN: ["jump", "kill", "hand_codex"],
    AgentStatus.QUEUED: ["jump", "log"],
    AgentStatus.IDLE: ["jump", "kill", "model", "effort", "mode", "folder", "copy_id", "handoff"],
    AgentStatus.UNKNOWN: ["jump", "kill", "folder"],
    AgentStatus.PARKED: ["hand_codex", "copy_id"],
    AgentStatus.RESUME_FAILED: ["hand_codex", "folder", "copy_id"],
    AgentStatus.DONE: ["log", "hand_claude"],
    AgentStatus.GONE: ["copy_id", "hand_codex"],
}

# Every key the toolbar can ever show, regardless of row -- the pane's BINDINGS must offer exactly
# this set (S-UI acceptance 3, `tests/test_ui_controls.py`).
ALL_TOOLBAR_KEYS = {a.key for a in ACTION_DEFS.values()}


def _confirmed(data: dict, field: str, wanted: str) -> bool:
    """Did the newest statusline capture pick up the change typed?"""
    if field == "effort":
        return str((data.get("effort") or {}).get("level") or "").lower() == wanted.lower()
    if field == "model":
        model = data.get("model") or {}
        blob = f"{model.get('id') or ''} {model.get('display_name') or ''}".lower()
        return wanted.lower() in blob
    return False

_GLYPH_FOR = {
    AgentStatus.WORKING: "working",
    AgentStatus.RUNNING: "working",
    AgentStatus.WINDING_DOWN: "attention",
    AgentStatus.WAITING_INPUT: "attention",
    AgentStatus.BLOCKED_PERMISSION: "attention",
    AgentStatus.RESUME_FAILED: "attention",
    AgentStatus.IDLE: "idle",
    AgentStatus.QUEUED: "idle",
    AgentStatus.PARKED: "idle",
    AgentStatus.UNKNOWN: "idle",
    AgentStatus.QUIET: "idle",
    AgentStatus.DONE: "ok",
    AgentStatus.FAILED: "fail",
    AgentStatus.GONE: "dot",
}

_MUTED_STATES = {AgentStatus.GONE, AgentStatus.IDLE, AgentStatus.DONE, AgentStatus.PARKED, AgentStatus.QUIET}


# The phone tier (under NARROW_AT) draws each session on TWO lines in one full-width column,
# like the desk sidebar's running rows: line 1 the
# glyph and the session's title, line 2 dim `status · project · where · age · last action`.
# Each line is clipped, never wrapped. `PHONE_COLUMNS` above is kept for importers; the table no
# longer draws it.
PHONE_ROW_COLUMN = "session"
PHONE_ROW_HEIGHT = 2


def phone_columns(width: int) -> tuple:
    # 1 cell of padding either side, plus room for the vertical scrollbar.
    return ((PHONE_ROW_COLUMN, "session", max(20, width - 4)),)


def columns_for(width: int) -> tuple:
    """Which layout a pane this wide gets. Fewer columns, never narrower ones."""
    if width >= SUPER_WIDE_AT:
        return SUPER_WIDE_COLUMNS
    if width >= WIDE_AT:
        return WIDE_COLUMNS
    if width >= NARROW_AT:
        return NARROW_COLUMNS
    return phone_columns(width)


# The long labels, shortened for the phone's second line (the `!!` glyph on line 1 still marks a
# permission prompt); every other status keeps its own label.
PHONE_WORDS = {AgentStatus.WAITING_INPUT: "needs you", AgentStatus.BLOCKED_PERMISSION: "needs approval",
               AgentStatus.QUIET: "quiet"}


def phone_status_word(status: AgentStatus) -> str:
    return PHONE_WORDS.get(status, status.label)


def clip_line(text: str, width: int) -> str:
    """One line cut to `width` with an ellipsis -- never wrapped onto a second line."""
    text = " ".join(str(text or "").split())
    if width <= 0:
        return ""
    return text if len(text) <= width else text[: max(0, width - 1)] + "…"


def state_cell(row: AgentState, glyphs: dict[str, str]) -> str:
    """`!! blocked - permission`, `● working`, `▲ waiting - needs input`. Words always present."""
    if row.status in (AgentStatus.BLOCKED_PERMISSION, AgentStatus.RESUME_FAILED):
        return f"!! {row.status.label}"   # both need the user now (S-UI section 4.2, top of the sort)
    return f"{glyphs.get(_GLYPH_FOR.get(row.status, 'idle'), '')} {row.status.label}".strip()


def row_style(row: AgentState) -> Optional[str]:
    """Which colour role a row gets, or None for the plain rows -- most of them."""
    if row.status is AgentStatus.FAILED:
        return "error"
    if row.needs_human:
        return "warning"
    if row.status in _MUTED_STATES:
        return "muted"
    return None


def _describe_event(e: Event) -> str:
    """One line of the detail panel's "last five actions": a short plain sentence, never a code. Built from the same fields the hook actually
    writes (`hooks/pantheon_event.ps1`) -- no file path is available for a tool call today, so
    `Edit`/`Bash`/`Grep` print as just the tool name, not the fuller `Edit src/x.py` the spec's
    own example shows."""
    source = (e.source or "claude").lower()
    name = e.event or ""
    if source == "codex":
        name_l = name.lower()
        detail = (e.detail or "").strip()
        if name_l == "queued":
            return "codex exec queued"
        if name_l == "running":
            return "codex exec running"
        if name_l in ("done", "failed"):
            return f"codex exec {detail}".strip() if detail else f"codex exec {name_l}"
        return f"codex {name_l}".strip() or "codex event"
    if source == "pantheon":
        name_l = name.lower()
        if name_l == "kill":
            return "closed from the deck"
        if name_l == "wind_down":
            return "asked to save and stop"
        if name_l == "park":
            return "parked until the limit resets"
        if name_l == "dispatch":
            row_id = e.extra.get("row_id")
            return f"started from QUEUE row '{row_id}'" if row_id else "started from the deck"
        if name_l == "resume":
            return "resume attempted"
        return (e.message or "").strip() or name_l or "pantheon event"
    # claude
    if e.agent_id:
        return f"subagent: {e.tool_name or name or 'event'}"
    if name == "SessionStart":
        return "session started"
    if name in ("PostToolUse", "PreToolUse"):
        return e.tool_name or "ran a tool"
    if name == "Stop":
        return "turn done"
    if name == "SessionEnd":
        return "session ended"
    if name == "PermissionRequest":
        return f"asked permission to use {e.tool_name or 'a tool'}"
    if name == "Notification":
        nt = (e.notification_type or "").lower()
        msg = (e.message or "").strip()
        return msg or nt.replace("_", " ").strip() or "notice"
    return name or "event"


def toolbar_actions_for(row: AgentState, available_providers: Optional[set] = None) -> list[Action]:
    """The row toolbar's buttons for one row, in the fixed order of `ACTION_DEFS` (S-UI 4.4:
    "the order above is fixed so the eye finds them in the same place"). A button whose action
    would only print a refusal is still shown, dimmed -- the disabled state is the explanation,
    never a hidden button (S-UI 4.4), EXCEPT `Resume now` and `Answer`/`Jump`/`Kill` on a row with no tmux window at all. `available_providers` are the ones switched on
    in `pantheon.toml`."""
    keys = ROW_ACTIONS.get(row.status, [])
    typeable = row.in_pantheon and row.window_index is not None
    available = available_providers or set()
    out: list[Action] = []
    for name in keys:
        # A row with no tmux window (`desktop`) can never be jumped to or killed -- same reasoning
        # as `Resume now`'s removal: disabled taught nothing, so the button is dropped entirely
        #.
        if name in ("jump", "answer", "kill") and row.window_index is None:
            continue
        base = ACTION_DEFS[name]
        enabled, tooltip = True, None
        if name in ("allow", "deny"):
            enabled = row.window_index is not None
            if not enabled:
                tooltip = "answer it in the Desktop app"
        elif name == "kill":
            enabled = row.in_pantheon
        elif name in ("model", "effort", "mode"):
            enabled = typeable
        elif name == "handoff":
            enabled = typeable and row.provider != "codex" and "claude" in available
            if typeable and not enabled:
                tooltip = "'claude' is not switched on in pantheon.toml"
        elif name == "folder":
            enabled = bool(row.cwd)
        elif name in ("hand_codex", "hand_claude"):
            to_provider = "codex" if name == "hand_codex" else "claude"
            enabled = to_provider in available
            if not enabled:
                tooltip = f"'{to_provider}' is not switched on in pantheon.toml"
        out.append(Action(base.key, base.label, base.action_name, base.variant, enabled, tooltip))
    return out


def _tail(text: Optional[str], width: int) -> str:
    """The end of a long path, which is the informative end.

    A cut that lands mid-folder-name reads as gibberish ("cuments/Projects/Plumb"), so when the first
    surviving name is only half there, drop it and start at the slash instead.
    """
    t = (text or "").replace("\\", "/")
    if len(t) <= width:
        return t
    cut = t[-width:]
    if t[-width - 1] != "/" and "/" in cut:
        return cut[cut.index("/"):]
    return cut


def _clip_one_line(text: str, width: int) -> str:
    """One flat line for the detail panel's transcript-derived "last reply" -- collapses
    any newlines a multi-paragraph answer had, same shape as `state.py`'s `_clip` for a
    notification message."""
    flat = " ".join(text.split())
    return flat[: width - 1] + "…" if len(flat) > width else flat


class SupervisorPane(Widget):
    """The agents list, header line, key overlay and footer sentence, as one reusable widget."""

    class SessionStarted(Message):
        """The new-session steps opened a session in a tmux window."""

        def __init__(self, window_index: Optional[int]) -> None:
            self.window_index = window_index
            super().__init__()

    DEFAULT_CSS = """
    SupervisorPane { layout: vertical; height: 1fr; width: 1fr; }
    SupervisorPane #header { height: 1; padding: 0 1; }
    SupervisorPane #agents { height: 1fr; }
    SupervisorPane DataTable > .datatable--header { background: $panel; color: $primary; text-style: bold; }
    SupervisorPane DataTable > .datatable--cursor { background: $primary-muted; text-style: bold; }
    SupervisorPane DataTable > .datatable--odd-row { background: $background; }
    SupervisorPane DataTable > .datatable--even-row { background: $surface; }
    SupervisorPane DataTable > .datatable--hover { background: $surface-lighten-1; }
    SupervisorPane #detail {
        display: none; height: 1fr; padding: 0 1;
        border: round $secondary; border-title-color: $primary; border-title-style: bold;
        color: $foreground;
    }
    SupervisorPane #toolbar { height: auto; }
    SupervisorPane #status { height: 1; padding: 0 1; color: $text-muted; }
    SupervisorPane #keys { display: none; height: 1fr; padding: 1 2; background: $panel; }
    """

    # These fire while the DataTable inside has focus: Textual walks the focus chain outwards, and
    # the table claims none of these letters. `m`/`e`/`p`/`enter` are row-toolbar actions;
    # `h` exists only so the (always-disabled) Hand-off button has a key, per S-UI acceptance 3
    # ("every button has a key and every key a button"). No `R`/resume-now binding: that button
    # was removed entirely, not just disabled.
    BINDINGS = [
        ("n", "new_session", "new session"),
        ("j", "jump", "jump"),
        ("k", "kill", "kill"),
        ("o", "open_folder", "open"),
        ("c", "copy_id", "copy id"),
        ("r", "refresh_now", "refresh"),
        ("m", "model", "model"),
        ("e", "effort", "effort"),
        ("p", "mode", "mode"),
        ("l", "log", "log"),
        Binding("a", "allow", "allow once", show=False),
        Binding("d", "deny", "deny", show=False),
        Binding("v", "view_request", "see all of it", show=False),
        Binding("h", "hand_off", "hand off", show=False),
        Binding("H", "handoff", "hand off here", show=False),
        # `priority=True`: `DataTable` binds its OWN `enter` (to `select_cursor`) on itself, the
        # focused widget, which would otherwise win over this pane-level binding every time.
        Binding("enter", "open_actions", "actions", priority=True),
    ]

    def __init__(
        self,
        cfg: Optional[config_mod.Config] = None,
        window_source: Optional[Callable[[], list[TmuxWindow]]] = None,
        id: Optional[str] = "supervisor",
    ) -> None:
        super().__init__(id=id)
        self.cfg = cfg or config_mod.load()
        # Injectable so tests never touch a real tmux server, and never the user's own session.
        # The default goes through the short-lived cache (perf sweep item 2): the supervisor and
        # queue panes, both polling "every window in every session" on their own ~5s timers a
        # fraction of a second apart, now usually share one subprocess spawn instead of two.
        self._window_source = window_source or (lambda: tmuxctl.list_windows_cached(None))
        # `appearance_settings.glyphs`: `[appearance] glyphs` wins when set, else the old
        # top-level `glyphs` key still works. `icon_table` layers Nerd Font glyphs from
        # `[appearance] icons` on top -- `state_cell` still prints the word beside
        # whichever glyph comes back, so `icons = false` (the default) renders exactly as before.
        appearance = self.cfg.appearance_settings()
        self.glyphs = glyphs_mod.icon_table(appearance.glyphs, appearance.icons)
        self.rows: list[AgentState] = []
        self.table_rows: list[AgentState] = []   # what the table draws (see `hide_assistant`)
        # The pinned Assistant has its own line in the deck's sidebar, so the table leaves it
        # out only while that sidebar is on screen; the deck sets this. The phone layout and the
        # stand-alone agents window have no sidebar, so there it stays a row.
        self.hide_assistant = False
        self._assistant_ids: frozenset = frozenset()
        self._assistant_title = ""
        self.windows: list[TmuxWindow] = []
        self.parse_errors = 0
        self.live_tmux = False
        self._row_keys: list[str] = []
        self._message = ""
        self._message_role: Optional[str] = None
        self._columns: tuple = ()
        self._age_index: Optional[int] = None   # perf sweep item 3: which cell is `age`, if any
        self._last_cells: Optional[list[list[str]]] = None    # last tick's painted cell text
        self._last_styles: Optional[list[Optional[str]]] = None
        self._selected_key: Optional[str] = None
        self.providers: dict = {}   # populated on mount; the Hand-off buttons need to know what
                                     # is switched on in pantheon.toml
        self._keys_page = 0         # which page the `?` overlay shows: 0 keys, 1 connectors
        self._events: list[Event] = []   # this tick's parsed event log (perf sweep item 1's
                                          # shared cache), kept so the detail panel can filter it
                                          # by session_id instead of a second file read
        self._detail_visible = False     # D/W widths only
        # Startup questions (folder trust, a new MCP server) read off agent screens each tick;
        # `_row_dialogs` is session_id -> (tmux target, Dialog) for the rows showing one now.
        self._dialog_scanner = dialogs_mod.Scanner()
        self._row_dialogs: dict = {}
        # Permission prompts: session_id -> what the row is asking, read
        # from the event log and the window's own screen each tick (`supervisor/approvals.py`).
        self.approvals = approvals_mod.Approvals()
        self._ascii = (appearance.glyphs or "").lower() == "ascii"
        # session_id -> (monotonic deadline, UTC time `/checkpoint` was typed) for
        # each `H` hand-off still waiting on its old session; a second `H` never types it twice.
        self._handoff_waits: dict[str, tuple[float, datetime]] = {}

    # ------------------------------------------------------------------ layout

    def compose(self) -> ComposeResult:
        yield Static("PANTHEON", id="header")
        with Vertical():
            yield DataTable(id="agents", zebra_stripes=True, cursor_type="row")
            yield Static("", id="detail")
            yield RowToolbar(self._toolbar_dispatch, id="toolbar")
            yield Static(keys_mod.section("deck"), id="keys")
        yield Static("", id="status")

    def on_mount(self) -> None:
        self._apply_columns(self.size.width or self.app.size.width)
        try:
            self.providers = get_providers(self.cfg)
        except Exception as exc:
            self._log(exc)
            self.providers = {}
        self.refresh_rows()
        self.focus_table()
        self.set_interval(max(1, int(self.cfg.refresh_seconds or 5)), self.refresh_rows)

    def on_resize(self, event: tevents.Resize) -> None:
        # The PANE's width, not the screen's -- this widget will share a row with others later.
        if self._apply_columns(event.size.width or self.size.width):
            self.redraw()

    def focus_table(self) -> None:
        """Put the cursor in the list, so j/k/o/c/r reach this pane from the first keypress."""
        self.query_one("#agents", DataTable).focus()

    def _apply_columns(self, width: int) -> bool:
        # The detail panel shows/hides at the same D/W boundary the columns do, but
        # every call re-checks it (not only on a column change): a bare `_detail_visible` flip
        # with no matching column-tuple change should never happen given today's thresholds, but
        # this stays correct even if that ever stops being true.
        self._set_detail_visible(width >= WIDE_AT)
        wanted = columns_for(width)
        if wanted == self._columns:
            return False
        self._columns = wanted
        self._age_index = next((i for i, (key, _l, _w) in enumerate(wanted) if key == "age"), None)
        if self._phone_rows():
            # The one cell holds the age too: a changed cell is patched in place, never a rebuild.
            self._age_index = 0
        table = self.query_one("#agents", DataTable)
        table.clear(columns=True)
        table.show_header = not self._phone_rows()
        for key, label, w in wanted:
            table.add_column(label, width=w, key=key)
        self._row_keys = []
        self._last_cells = None
        self._last_styles = None
        return True

    def _set_detail_visible(self, visible: bool) -> None:
        if visible == self._detail_visible:
            return
        self._detail_visible = visible
        try:
            detail = self.query_one("#detail", Static)
            table = self.query_one("#agents", DataTable)
        except Exception:  # pragma: no cover - not mounted yet
            return
        detail.display = visible
        if not visible:
            table.styles.clear_rule("height")

    # ------------------------------------------------------------------ data

    def refresh_rows(self) -> None:
        try:
            events_mod.rotate_if_large(self.cfg.events_file)
            evs, errors = eventcache_mod.read_events_cached(self.cfg.events_file)
            self._events = evs   # kept for the detail panel's "last five actions":
                                  # the same parsed copy `refresh_rows` already reads, not a
                                  # second file read
            self.windows = list(self._window_source() or [])
            self.parse_errors = errors
            self.live_tmux = bool(self.windows)
            self.rows = state_mod.fold(
                evs,
                self.windows,
                datetime.now(timezone.utc),
                self.cfg.tmux_session,
                self.cfg.projects_root,
                statusline_mtimes=hud_sources.statusline_mtimes(self.cfg.statusline_dir),
                reserved_panes=assistant_mod.reserved_panes(self.cfg),
            )
            self.rows = self._mark_dialogs(self.rows)
            self.rows = self._mark_approvals(self.rows)
            # The pinned Assistant has its own line in the sidebar; every other surface
            # still reads `self.rows` (the sidebar's Assistant status, the queue, the strip).
            self._pick_table_rows()
        except Exception as exc:  # never let a bad read kill the deck
            self._log(exc)
            self._message = "could not read the agent log - details in state/pantheon.log"
            self._message_role = "error"
        self.redraw()

    def _mark_dialogs(self, rows: list[AgentState]) -> list[AgentState]:
        """Read the screens of agent windows with no fresh hook event; a startup question there
        turns the row amber with the reason and toasts once per appearance (`pantheon/dialogs.py`,
        `notify/startup_dialogs.py`). A failure here must never cost the rest of the tick."""
        try:
            now = datetime.now(timezone.utc)
            found = self._dialog_scanner.scan(state_mod.dialog_candidates(rows, now),
                                              tmux=self.cfg.tools.tmux)
            self._row_dialogs = found
            by_id = {r.session_id: r for r in rows}
            keys = []
            for sid, (target, dialog) in found.items():
                row = by_id[sid]
                key = f"{dialogs_mod.Scanner.key(row)}|{dialog.kind}"
                keys.append(key)
                dialog_notify.announce(self.cfg, key, f"Window {row.window_index} ({row.project or '-'}) "
                                       f"is {dialog.reason}. Select it on the deck and press Enter.")
            dialog_notify.forget_except(self.cfg, keys)
            return state_mod.apply_dialogs(rows, {sid: d.reason for sid, (_t, d) in found.items()})
        except Exception as exc:
            self._log(exc)
            return rows

    def _pick_table_rows(self) -> None:
        others = assistant_mod.without_assistant(self.cfg, self.rows)
        kept = {r.session_id for r in others}
        self._assistant_ids = frozenset(r.session_id for r in self.rows if r.session_id not in kept)
        self._assistant_title = ""
        if self._assistant_ids:
            try:
                self._assistant_title = assistant_mod._name(self.cfg, assistant_mod.read_state(self.cfg))
            except Exception:
                self._assistant_title = ""
        self.table_rows = others if self.hide_assistant else list(self.rows)

    def set_hide_assistant(self, hide: bool) -> None:
        """The deck: True while its SESSIONS sidebar (with the `◆ Assistant` line) is on screen."""
        if hide == self.hide_assistant:
            return
        self.hide_assistant = hide
        self._pick_table_rows()
        try:
            self.redraw()
        except Exception:   # pragma: no cover - not mounted yet
            pass

    def link_lost(self, pane_id: str) -> bool:
        """What the startup-question scan's last read of this pane said about its Remote Control
        link (`dialogs.link_lost`); the scan already reads idle windows, so this costs nothing."""
        return self._dialog_scanner.link_lost(pane_id)

    def _mark_approvals(self, rows: list[AgentState]) -> list[AgentState]:
        """What each blocked row is asking, word for word where the hook recorded it; a read of
        the window keeps it honest (a prompt answered there clears here). Never costs the tick."""
        try:
            return self.approvals.update(rows, self._events, self.windows, self._row_dialogs,
                                         self.cfg.tmux_session, tmux=self.cfg.tools.tmux,
                                         ascii_only=self._ascii)
        except Exception as exc:
            self._log(exc)
            return rows

    def _action_width(self) -> int:
        return next((w for key, _l, w in self._columns if key == "action"), 0)

    def _phone_rows(self) -> bool:
        return bool(self._columns) and self._columns[0][0] == PHONE_ROW_COLUMN

    def _row_title(self, row: AgentState) -> str:
        """The same title the desk sidebar shows: a rename first, then Claude's own title, then
        the first thing typed; `untitled <project>` when there is nothing. The Assistant's row is
        `Assistant`, with its conversation's own name after it when it has one."""
        if row.session_id in self._assistant_ids:
            name = (self._assistant_title or "").strip()
            default = (self.cfg.assistant_settings().name or "").strip().lower()
            return f"Assistant · {name}" if name and name.lower() != default else "Assistant"
        title = None
        if (row.provider or "claude") == "claude" and row.cwd and row.session_id:
            try:
                path = transcripts_mod.transcript_path(row.session_id, row.cwd, Path(self.cfg.claude_home))
                title = sessions_mod.title_or_none(path) if path.exists() else None
            except Exception:
                title = None
        if not title:
            from ..session_view import titles as titles_mod
            title = titles_mod.untitled(row.project, None)
        return title

    def _phone_where(self, row: AgentState) -> str:
        if row.window_index is not None:
            if (row.tmux_session or self.cfg.tmux_session) == self.cfg.tmux_session:
                return f"window {row.window_index}"
            return row.where
        return "desktop" if row.where == "desktop" else "no window"

    def _phone_cell(self, row: AgentState, now: datetime) -> str:
        """Two lines, each clipped to the column: glyph + title, then the dim details."""
        width = self._columns[0][2]
        if row.status in (AgentStatus.BLOCKED_PERMISSION, AgentStatus.RESUME_FAILED):
            glyph = "!!"
        else:
            glyph = self.glyphs.get(_GLYPH_FOR.get(row.status, "idle"), "")
        first = clip_line(f"{glyph} {self._row_title(row)}".strip(), width)
        word = phone_status_word(row.status)
        # The Assistant's title already says what it is; its project (`workspace`) would only push
        # the window number off a 46-column line.
        project = "" if row.session_id in self._assistant_ids else (row.project or "-")
        parts = [word, project, self._phone_where(row), format_age(row.age_seconds(now))]
        base = " · ".join(p for p in parts if p)
        room = width - 2 - len(base) - len(" · ")
        action = ""
        shown = self.approvals.shown.get(row.session_id)
        if shown is not None and room >= 8:
            action = shown.line(room, self._ascii)[0]
        elif row.last_action and row.last_action != "-" and room >= 8:
            action = row.last_action
        second = clip_line(base + (f" · {action}" if action else ""), width - 2)
        return first + "\n  " + second

    def _cell_values(self, row: AgentState, now: datetime) -> list[str]:
        """One row's text, in the order of whichever layout is on screen. `session`/`folder` left
        the table for the detail panel, so they are no longer built here."""
        if self._phone_rows():
            return [self._phone_cell(row, now)]
        values = {
            "state": state_cell(row, self.glyphs),
            "project": row.project or "-",
            "where": "no window" if row.where == "desktop" else row.where,
            "action": row.last_action or "-",
            "age": format_age(row.age_seconds(now)),
        }
        shown = self.approvals.shown.get(row.session_id)
        if shown is not None and self._action_width():
            # The request itself, cut only at the end with `… +N` (never a silent cut).
            values["action"] = shown.line(self._action_width(), self._ascii)[0]
        if any(key == "model" for key, _, _ in self._columns):
            # W width only (S-UI section 4.1): the newest statusline capture, e.g. `opus[1m]`, or
            # the transcript's latest turn when this row has no statusline at all (a
            # Claude Desktop-app session -- `mode` never needed that fallback, it comes from the
            # same hooks a tmux row uses, statusline or not).
            model, _effort = self._current_model_effort(row.session_id, row.cwd)
            values["model"] = model or "-"
            values["mode"] = row.mode or "-"
        return [values.get(key, "") for key, _, _ in self._columns]

    def redraw(self) -> None:
        """Perf sweep: an idle tick used to `table.clear` and re-`add_row` every row whether or not
        anything on screen had changed. Now: compute what would be painted, and if it is identical
        to last tick's paint except for the `age` column, patch only the `age` cells
        (`table.update_cell`) or do nothing at all -- the age number still ticks up on its own
, everything else stays untouched."""
        table = self.query_one("#agents", DataTable)
        now = datetime.now(timezone.utc)
        new_keys = [row.session_id for row in self.table_rows]
        new_styles = [row_style(row) for row in self.table_rows]
        new_cells = [self._cell_values(row, now) for row in self.table_rows]

        if self._rebuild_can_be_skipped(new_keys, new_styles, new_cells):
            self._patch_age_cells(table, new_keys, new_cells)
            self._last_cells = new_cells
            self._update_header()
            self._update_status()
            self._refresh_toolbar()
            self._size_table_for_detail(table)
            self._update_detail()
            return

        keep = None
        if 0 <= table.cursor_row < len(self._row_keys):
            keep = self._row_keys[table.cursor_row]
        previous_index = table.cursor_row

        table.clear()
        self._row_keys = []
        for row, style, cells in zip(self.table_rows, new_styles, new_cells):
            table.add_row(*(self._paint(c, style) for c in cells), key=row.session_id,
                          height=PHONE_ROW_HEIGHT if self._phone_rows() else 1)
            self._row_keys.append(row.session_id)

        if keep is not None and keep in self._row_keys:
            table.move_cursor(row=self._row_keys.index(keep))
        elif self._row_keys:
            table.move_cursor(row=min(max(previous_index, 0), len(self._row_keys) - 1))

        self._last_styles = new_styles
        self._last_cells = new_cells
        self._update_header()
        self._update_status()
        self._refresh_toolbar()
        self._size_table_for_detail(table)
        self._update_detail()

    def _size_table_for_detail(self, table: DataTable) -> None:
        """At D/W the table gives up the bottom of the pane to the detail box: it takes what its
        rows need, up to half the pane, and the detail box fills the rest. `height:
        auto` is not used -- `DataTable` does not implement content-height measurement, so this
        sets an explicit row count instead, recomputed every redraw since the row count changes."""
        if not self._detail_visible:
            return
        pane_height = self.size.height or (self.app.size.height if self.app else 24)
        half = max(4, pane_height // 2)
        wanted = min(len(self.table_rows) + 1, half)   # `+ 1`: the header row
        table.styles.height = max(3, wanted)

    def _cells_equal_ignoring_age(self, a: list[str], b: list[str]) -> bool:
        if self._age_index is None:
            return a == b
        i = self._age_index
        return a[:i] == b[:i] and a[i + 1 :] == b[i + 1 :]

    def _rebuild_can_be_skipped(self, keys: list[str], styles: list[Optional[str]],
                           cells: list[list[str]]) -> bool:
        """True when a full rebuild can be skipped: same rows, same order, same colours, and
        every cell identical except possibly `age`. False (rebuild) the first time this pane
        draws at all (`_last_cells` is still `None`)."""
        if self._last_cells is None or self._last_styles is None:
            return False
        if keys != self._row_keys or styles != self._last_styles:
            return False
        if len(cells) != len(self._last_cells):
            return False
        return all(self._cells_equal_ignoring_age(a, b) for a, b in zip(cells, self._last_cells))

    def _patch_age_cells(self, table: DataTable, keys: list[str], cells: list[list[str]]) -> None:
        if self._age_index is None or self._last_cells is None or self._last_styles is None:
            return
        age_key = self._columns[self._age_index][0]
        for row_key, new_row, old_row, style in zip(keys, cells, self._last_cells, self._last_styles):
            new_age = new_row[self._age_index]
            if new_age == old_row[self._age_index]:
                continue
            try:
                table.update_cell(row_key, age_key, self._paint(new_age, style))
            except Exception:  # pragma: no cover - defensive: never let a cell patch break a tick
                pass

    def _paint(self, text: str, style: Optional[str]) -> Text:
        first, sep, rest = text.partition("\n")
        if not sep:
            if style is None:
                return Text(text)
            colour = theme_mod.TOKENS.get(style, "")
            return Text(text, style=f"bold {colour}" if style == "error" else colour)
        # A phone row: the first line takes the row's colour, the second is always dim.
        colour = theme_mod.TOKENS.get(style, "") if style else ""
        out = Text(first, style=(f"bold {colour}" if style == "error" else colour) or "",
                   no_wrap=True, overflow="ellipsis")
        out.append("\n" + rest, style=theme_mod.TOKENS.get("muted", "") or "dim")
        return out

    def _update_header(self) -> None:
        working, needing = state_mod.counts(self.rows)
        dot = self.glyphs.get("dot", ".")
        self.query_one("#header", Static).update(
            f"PANTHEON {dot} agents  {working} working {dot} {needing} need you"
        )

    def _update_status(self) -> None:
        standing = state_mod.footer_status(self.parse_errors, self.live_tmux)
        line = Text()
        if self._message:
            colour = theme_mod.TOKENS.get(self._message_role or "", "")
            line.append(self._message, style=colour or None)
            line.append("   ")
        elif not self._detail_visible:
            # Narrow (phone) widths have no detail box: the selected row's prompt goes here.
            shown = self._selected_shown()
            if shown is not None:
                text, _cut = shown.line(self._footer_width(), self._ascii)
                line.append(text, style=theme_mod.TOKENS["error" if shown.summary.warning else "warning"])
                line.append("   ")
        line.append(standing)
        self.query_one("#status", Static).update(line)

    def say(self, message: str, role: Optional[str] = None) -> None:
        """One sentence in the footer. `role` is a theme token name. Nothing here ever self-dismisses on a timer."""
        self._message = message
        self._message_role = role
        self._update_status()

    def _log(self, exc: BaseException) -> None:
        try:
            p = Path(self.cfg.log_file)
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "a", encoding="utf-8") as fh:
                fh.write(f"{utcnow_iso()} supervisor: {exc!r}\n{traceback.format_exc()}\n")
        except OSError:
            pass

    # ------------------------------------------------------------------ selection

    def selected(self) -> Optional[AgentState]:
        table = self.query_one("#agents", DataTable)
        if 0 <= table.cursor_row < len(self.table_rows):
            return self.table_rows[table.cursor_row]
        return None

    def _window_for(self, row: AgentState) -> Optional[TmuxWindow]:
        return state_mod.match_window(row.cwd, row.tmux_pane, self.windows, self.cfg.tmux_session,
                                      provider=row.provider)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        # Arrow-key/mouse movement inside the table changes the selection without a full redraw.
        self._refresh_toolbar()
        self._update_detail()

    # ------------------------------------------------------------------ row toolbar

    def _refresh_toolbar(self) -> None:
        row = self.selected()
        self._selected_key = row.session_id if row is not None else None
        try:
            bar = self.query_one("#toolbar", RowToolbar)
        except Exception:  # pragma: no cover - not mounted yet
            return
        actions = toolbar_actions_for(row, set(self.providers.keys())) if row is not None else []
        # The standing New session button leads the bar even with no row selected.
        actions = [NEW_SESSION_ACTION] + actions
        # `self.size.width` can still be 0 on the very first paint, before this widget's first
        # layout pass -- same fallback `on_mount` already uses for `_apply_columns`. The bar now
        # decides its own form (full/compact/bare/collapsed) from the width alone:
        # panes stop computing `collapsed=` themselves (desk-first review findings 3 and 14).
        width = self.size.width or self.app.size.width
        bar.set_actions(actions, width=width)

    # ------------------------------------------------------------------ detail panel

    def subtitle(self) -> str:
        """`8 running · sorted by who needs you` -- the deck sets `border_subtitle` from this on
        its 5-second header refresh."""
        return f"{state_mod.running_count(self.table_rows)} running · sorted by who needs you"

    def _update_detail(self) -> None:
        if not self._detail_visible:
            return
        try:
            detail = self.query_one("#detail", Static)
        except Exception:  # pragma: no cover - not mounted yet
            return
        row = self.selected()
        if row is None:
            detail.border_title = "no agent selected"
            detail.update("")
            return
        detail.border_title = self._detail_title(row)
        detail.update(self._detail_body(row))

    def _detail_title(self, row: AgentState) -> str:
        name = row.project or (row.session_id[:8] if row.session_id else "agent")
        if row.window_index is not None:
            where = f"window {row.window_index}"
        elif row.provider == "codex" and row.job_id:
            where = "headless"
        else:
            where = "desktop"
        return f"{name} · {where}"

    def _session_name(self, row: AgentState) -> str:
        data = hud_sources.statusline_for(self.cfg.statusline_dir, row.session_id)
        name = data.get("session_name")
        if name:
            return str(name)
        if not data and row.cwd:
            # no statusline at all -- the transcript's own title (the same `aiTitle`/first
            # message `dispatch/sessions.py` reads for the New-session "resume" list) beats a
            # bare id fragment.
            title = transcripts_mod.read_transcript(row.session_id, row.cwd).title
            if title:
                return title
        return row.session_id[:8] if row.session_id else "-"

    def _recent_events(self, session_id: Optional[str], now: datetime) -> list[tuple[Event, str]]:
        """The last five events of THIS session, newest first, from the same parsed copy of
        `events.jsonl` `refresh_rows` already reads."""
        if not session_id:
            return []
        matches = [e for e in self._events if e.session_id == session_id]
        matches.sort(key=lambda e: e.when or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        out = []
        for e in matches[:5]:
            when = e.when
            age = max(0.0, (now - when).total_seconds()) if when is not None else None
            out.append((e, format_age(age)))
        return out

    def _detail_body(self, row: AgentState) -> Text:
        now = datetime.now(timezone.utc)
        dim = theme_mod.TOKENS["dim"]
        chrome = theme_mod.TOKENS["chrome"]
        text = Text()
        shown = self.approvals.shown.get(row.session_id)
        if shown is not None:
            self._append_request(text, row, shown)
        text.append(row.status.label, style="bold")
        text.append("   ")
        text.append(format_age(row.age_seconds(now)), style=dim)
        text.append("\n")
        text.append("session ", style=dim)
        text.append(self._session_name(row), style="bold")
        text.append("\n")
        statusline_data = hud_sources.statusline_for(self.cfg.statusline_dir, row.session_id)
        model, effort = self._current_model_effort(row.session_id, row.cwd)
        text.append("model ", style=dim)
        text.append(model or "-", style="bold")
        text.append("  ·  effort ", style=dim)
        text.append(effort or "-", style="bold")
        text.append("  ·  mode ", style=dim)
        text.append(row.mode or "-", style="bold")
        transcript = None
        if row.cwd:
            # Every row, tmux or Desktop: the event log only carries bare tool names; the transcript has
            # what each call was for, the last reply and the latest ask. Tokens stay a Desktop-only
            # line: a tmux row already has the statusline's context figure.
            transcript = transcripts_mod.read_transcript(row.session_id, row.cwd)
            if transcript.tokens and not statusline_data:
                text.append("  ·  tokens ", style=dim)
                text.append(f"{transcript.tokens / 1000:.1f}k", style="bold")
        text.append("\n")
        text.append("where ", style=dim)
        text.append(row.where, style="bold")
        text.append("  ·  folder ", style=dim)
        text.append(_tail(row.cwd, DETAIL_FOLDER_WIDTH) or "-", style="bold")
        text.append("\n")
        if row.tracker_id:
            text.append("tracker ", style=dim)
            text.append(row.tracker_id, style="bold")
            text.append("\n")
        text.append("\n")
        if transcript is not None and transcript.last_ask:
            text.append("latest ask ", style=dim)
            text.append(_clip_one_line(transcript.last_ask, DETAIL_TEXT_WIDTH), style="bold")
            text.append("\n\n")
        text.append("last five actions\n", style=f"bold {chrome}")
        if transcript is not None and transcript.known:
            # no statusline means no event-derived "last action" either has file names
            # (`_describe_event`) -- the transcript's own tool calls do. But a transcript that
            # could not be read at all (no file on disk -- true in every test, and for a brand
            # new Desktop-app session before its first turn lands) has nothing more to say than
            # the event log already does, so fall through to the event-derived list below rather
            # than a permanent "(no recent events)" that would hide a real Notification message.
            if not transcript.actions:
                text.append("  (no recent events)", style=dim)
            else:
                for action in transcript.actions:
                    text.append(f"  {action.text}\n")
            if transcript.last_text:
                text.append("\n")
                text.append("last reply ", style=dim)
                text.append(_clip_one_line(transcript.last_text, DETAIL_TEXT_WIDTH), style="bold")
                text.append("\n")
        else:
            events = self._recent_events(row.session_id, now)
            if not events:
                text.append("  (no recent events)", style=dim)
            else:
                for e, age in events:
                    text.append(f"  {age:<5} ", style=dim)
                    text.append(_describe_event(e))
                    text.append("\n")
        return text

    def _toolbar_dispatch(self, action_name: str) -> None:
        """A toolbar button was pressed. Route it through the exact same `action_<name>` method a
        key press would call."""
        if action_name == "__actions__":
            return self.action_open_actions()
        method = getattr(self, f"action_{action_name}", None)
        if method is not None:
            method()

    def _target_for(self, row: AgentState) -> Optional[str]:
        if row.window_index is None or not row.in_pantheon:
            return None
        return f"{self.cfg.tmux_session}:{row.window_index}"

    def action_open_actions(self) -> None:
        """`Enter`, or the collapsed toolbar's `Actions… (Enter)` button (S-UI section 4.4, "P/N
        widths"): a `Pick` naming every action offered for the selected row, whatever the width."""
        row = self.selected()
        if row is None:
            return self.say("no agent selected")
        found = self._row_dialogs.get(row.session_id)
        if found and found[1].is_trust:
            return self._ask_trust(row, *found)
        actions = toolbar_actions_for(row, set(self.providers.keys()))
        if not actions:
            return self.say("no actions for this row")
        options = [
            PickOption(a.key, a.action_name, a.label.rstrip("…"), disabled=not a.enabled)
            for a in actions
        ]
        self.app.push_screen(Pick("actions for this row", options), self._toolbar_dispatch_or_none)

    def _ask_trust(self, row: AgentState, target: str, dialog) -> None:
        """Enter on a row stuck on the folder-trust question: show it, answer with one key."""
        where = dialog.folder or row.cwd or row.project or "this folder"
        body = (f"{where}\nClaude Code will not start in window {row.window_index} until this is "
                f"answered. Yes = \"Yes, I trust this folder\". No {dialog.no_effect}.")
        self.app.push_screen(
            Confirm(f"Trust {row.project or 'this folder'}?", yes_label="Yes, trust it (y)",
                    no_label="No, close Claude (n)", body=body),
            functools.partial(self._trust_answered, row.session_id, target, dialog))

    def _trust_answered(self, session_id: str, target: str, dialog, yes: Optional[bool]) -> None:
        if yes is None:
            return self.say("left the question open")
        ok, why = dialogs_mod.answer(target, dialog, bool(yes), tmux=self.cfg.tools.tmux, settle=0.3)
        self.say(f"{target}: {why}", None if ok else "warning")
        self.refresh_rows()

    # ------------------------------------------------------------------ permission prompts

    def _append_request(self, text: Text, row: AgentState, shown) -> None:
        """The detail box's top block: the one-line request (80 columns at most), where it is,
        and the keys -- the spec's card, drawn in the box under the table."""
        dim = theme_mod.TOKENS["dim"]
        colour = theme_mod.TOKENS["error" if shown.summary.warning else "warning"]
        line, _cut = shown.line(approval_mod.WIDTH, self._ascii)
        text.append(line, style=f"bold {colour}" if shown.summary.warning else colour)
        text.append("\n")
        if shown.kind == approvals_mod.TRUST:
            text.append(f"   {approvals_mod.TRUST_NOTE}\n", style=dim)
        else:
            bits = [f"in {row.project or '-'}", shown.where, f"mode {shown.request.mode or row.mode or '?'}"]
            if not shown.request.known and shown.kind == approvals_mod.PERMISSION:
                bits.append("full request not known")
            text.append("   " + " · ".join(bits) + "\n", style=dim)
        text.append(shown.hint(approval_mod.WIDTH, self._ascii) + "\n\n", style=dim)

    def _selected_shown(self):
        row = self.selected()
        return self.approvals.shown.get(row.session_id) if row is not None else None

    def action_allow(self) -> None:
        """`a` on the selected row: allow once (or, when its line is cut, see all of it first)."""
        row = self.selected()
        self.approval_key(row.session_id if row else "", "a", self._shown_width())

    def action_deny(self) -> None:
        row = self.selected()
        self.approval_key(row.session_id if row else "", "d", self._shown_width())

    def action_view_request(self) -> None:
        row = self.selected()
        self.approval_key(row.session_id if row else "", "v", self._shown_width())

    def _footer_width(self) -> int:
        """Columns the footer gives the selected row's request (narrow widths, no detail box)."""
        standing = state_mod.footer_status(self.parse_errors, self.live_tmux)
        return max(20, (self.size.width or 80) - 4 - len(standing) - 3)

    def _shown_width(self) -> int:
        """How much of the line the user is looking at: the detail box shows the whole 80-column
        line; without it, the footer shows the selected row's line as wide as it can."""
        if self._detail_visible:
            return approval_mod.WIDTH
        return self._footer_width()

    def approval_key(self, session_id: str, key: str, width: int = approval_mod.WIDTH) -> None:
        """`a` / `d` / `v` for one session, from the table or the SESSIONS list. `width` is how
        much of the summary that surface showed: a cut line never approves on the first press."""
        shown = self.approvals.shown.get(session_id) if session_id else None
        if shown is None:
            return self.say(approvals_mod.NOTHING_WAITING)
        if key == "v" or (key == "a" and shown.answerable and shown.needs_full_view(width, self._ascii)):
            return self._open_request(shown)
        if key in ("a", "d"):
            refusal = shown.refusal()
            if refusal:
                return self.say(refusal, "warning")
            self._answer_request(session_id, key == "a")

    def _open_request(self, shown) -> None:
        now = datetime.now(timezone.utc)
        kind = "FIRST START" if shown.kind == approvals_mod.TRUST else "PERMISSION"
        title = f"{kind} · {shown.project or '-'} · {shown.where}"
        self.app.push_screen(
            approvals_mod.RequestView(title, self.approvals.full_text(shown.session_id, now),
                                      shown.answerable, trust=shown.kind == approvals_mod.TRUST,
                                      note=shown.refusal() or ""),
            functools.partial(self._request_chosen, shown.session_id))

    def _request_chosen(self, session_id: str, choice: Optional[str]) -> None:
        if choice is None:
            return
        if choice == "jump":
            return self._jump_to_session(session_id)
        self._answer_request(session_id, choice == "allow")

    def _answer_request(self, session_id: str, allow: bool) -> None:
        ok, why = self.approvals.answer(session_id, allow, tmux=self.cfg.tools.tmux)
        self.say(why, None if ok else "warning")
        if ok:
            self.refresh_rows()

    def _jump_to_session(self, session_id: str) -> None:
        for i, row in enumerate(self.table_rows):
            if row.session_id == session_id:
                try:
                    self.query_one("#agents", DataTable).move_cursor(row=i)
                except Exception:
                    pass
                return self.action_jump()
        self.say("that session is not on the list any more")

    def _toolbar_dispatch_or_none(self, action_name: Optional[str]) -> None:
        if action_name is not None:
            self._toolbar_dispatch(action_name)

    # ------------------------------------------------------------------ new session

    def action_new_session(self) -> None:
        """`n` or the New session button: pick a project, then who works it, then an optional
        first message; the session opens the same way a dispatched one does, minus the row."""
        projects = projects_mod.list_projects(self.cfg, self._events)
        if not projects:
            return self.say(f"no project folders found under {self.cfg.projects_root}", "warning")
        self._ns: dict = {}
        self._ns_open("project")

    # The flow is a chain of steps over one dict; every modal's Escape returns None and that means
    # "back one step", never "start over". Resume skips the model/effort/mode
    # steps: a resumed session keeps what it had.
    _NS_STEPS = ("project", "who", "resume", "model", "effort", "mode", "message")

    def _ns_skipped(self) -> set:
        """Steps this path does not need: the resume list only on the resume path; model, effort
        and mode never on it (a resumed session keeps what it had)."""
        if self._ns.get("resume"):
            return {"model", "effort", "mode"}
        return {"resume"}

    def _ns_next(self, step: str) -> None:
        order = list(self._NS_STEPS)
        i = order.index(step) + 1
        skip = self._ns_skipped()
        while i < len(order) and order[i] in skip:
            i += 1
        if i >= len(order):
            return self._ns_launch()
        self._ns_open(order[i])

    def _ns_back(self, step: str) -> None:
        order = list(self._NS_STEPS)
        i = order.index(step) - 1
        skip = self._ns_skipped()
        while i >= 0 and order[i] in skip:
            i -= 1
        if i < 0:
            return self.say("no new session")
        self._ns_open(order[i])

    def _ns_open(self, step: str) -> None:
        ns = self._ns
        name = Path(ns["project_dir"]).name if ns.get("project_dir") else ""
        codex = ns.get("provider") == "codex"
        if step == "project":
            projects = projects_mod.list_projects(self.cfg, self._events)
            workspace = projects_mod.workspace_project(self.cfg, self._events)
            self.app.push_screen(ProjectPicker(projects, cfg=self.cfg, root=str(self.cfg.projects_root),
                                               workspace=workspace),
                                 functools.partial(self._ns_got, "project"))
        elif step == "who":
            phone = self.app.size.width < 80
            options = [PickOption(o.key, o.value,
                                  NEW_SESSION_WHO_PHONE_LABELS[o.value] if phone else o.label,
                                  disabled=(NEW_SESSION_VALUES[o.value][0] not in self.providers))
                       for o in NEW_SESSION_WHO]
            self.app.push_screen(Pick(f"who works in {name}  (Esc = back)", options, current=ns.get("who", "claude")),
                                 functools.partial(self._ns_got, "who"))
        elif step == "resume":
            found = sessions_mod.list_sessions(ns["project_dir"])
            if not found:
                options = [PickOption("", "", "no sessions found for this folder", "Esc = back", disabled=True)]
            else:
                options = [PickOption(str(i + 1), s.session_id, s.title, s.when_text)
                           for i, s in enumerate(found)]
            self.app.push_screen(Pick(f"resume which session in {name}  (Esc = back)", options),
                                 functools.partial(self._ns_got, "resume"))
        elif step == "model":
            # Family first, then version, recent first (`pantheon/model_picker.py`, the same two
            # screens the row action `m` opens). Escape on the family screen = back one step.
            default_model = "default" if codex else self.cfg.new_session_settings().model
            model_picker.open_picker(self.app, "codex" if codex else "claude", f"model for {name}",
                                     ns.get("model", default_model),
                                     functools.partial(self._ns_got, "model"), cfg=self.cfg)
        elif step == "effort":
            choices = CODEX_EFFORT_CHOICES if codex else EFFORT_CHOICES
            options = [PickOption(k, v, v, d) for k, v, d in choices]
            default_effort = "default" if codex else self.cfg.new_session_settings().effort
            self.app.push_screen(Pick(f"effort for {name}  (Esc = back)", options, current=ns.get("effort", default_effort)),
                                 functools.partial(self._ns_got, "effort"))
        elif step == "mode":
            choices = CODEX_SANDBOX_CHOICES if codex else MODE_CHOICES
            title = "sandbox" if codex else "permission mode"
            options = [PickOption(k, v, v, d) for k, v, d in choices]
            self.app.push_screen(Pick(f"{title} for {name}  (Esc = back)", options, current=ns.get("mode", "default")),
                                 functools.partial(self._ns_got, "mode"))
        elif step == "message":
            if self._carry_offer_pending():   # only after an `H` hand-off
                return self._ask_carry()
            headless = ns.get("interactive") is False
            self.app.push_screen(
                TextPrompt("first message" + (" (required for a headless job)" if headless else " (optional)"),
                           placeholder="what should it start on? Enter with nothing = just open the session",
                           hint="Enter starts the session · Esc = back", text=ns.get("message_seed", "")),
                functools.partial(self._ns_got, "message"))

    def _ns_got(self, step: str, value) -> None:
        if value is None:
            if step == "message" and self._ns.pop("carry", None) is not None:
                self._ns.pop("message_seed", None)   # back to the digest question, not past it
                return self._ns_open("message")
            return self._ns_back(step)
        ns = self._ns
        if step == "project":
            ns["project_dir"] = value
        elif step == "who":
            ns["who"] = value
            provider_name, interactive = NEW_SESSION_VALUES[value]
            ns["provider"], ns["interactive"] = provider_name, interactive
            ns["resume"] = value == "claude-resume"
        elif step == "resume":
            if not value:
                return self._ns_back(step)
            ns["session_id"] = value
        elif step in ("model", "effort", "mode"):
            ns[step] = value
        elif step == "message":
            if ns.get("interactive") is False and not (value or "").strip():
                # Spec path D: a headless job with no message stays on the box, with the reason.
                self.say("a headless Codex job needs a first message to work on", "warning")
                return self._ns_open("message")
            ns["message"] = value
        self._ns_next(step)

    def _ns_launch(self) -> None:
        ns = self._ns
        provider = self.providers.get(ns.get("provider", ""))
        if provider is None:
            return self.say(f"'{ns.get('provider')}' is not switched on in pantheon.toml", "warning")
        options = {k: ns.get(k) for k in ("model", "effort", "mode") if ns.get(k) and ns.get(k) != "default"}
        if ns.get("resume"):
            options["resume"] = ns.get("session_id") or True   # an id -> `--resume <id>`; True -> `--continue`
        name = Path(ns["project_dir"]).name
        self.say(f"starting {ns['provider']} in {name}...")
        # In a thread: the Claude launch waits on tmux for many seconds (same as the queue's `w`).
        self.run_worker(
            functools.partial(self._new_session_launch, ns["project_dir"], provider, ns.get("interactive"),
                              ns.get("message", ""), options),
            thread=True,
        )

    def _new_session_launch(self, project_dir: str, provider, interactive: Optional[bool], message: str,
                            options: dict) -> None:
        try:
            # Model/effort/mode go on Claude's command line inside `start_session`.
            result = projects_mod.start_session(project_dir, self.cfg, provider, interactive, message, options)
        except Exception:
            self._log(traceback.format_exc())
            self.app.call_from_thread(
                self.say, f"could not start that session; what went wrong is in {self.cfg.log_file}", "error")
            return
        self.app.call_from_thread(self._new_session_done, result)

    def _new_session_done(self, result, applied_notes: Optional[list[str]] = None) -> None:
        message = result.message or ("started" if result.ok else "that session did not start")
        if applied_notes:
            message = f"{message}; " + "; ".join(applied_notes)
        self.say(message, None if result.ok else "warning")
        self.refresh_rows()
        if getattr(result, "window_index", None) is not None:
            self.post_message(self.SessionStarted(result.window_index))

    # ------------------------------------------------------------------ model / effort / mode

    def _current_model_effort(
        self, session_id: Optional[str], cwd: Optional[str] = None
    ) -> tuple[Optional[str], Optional[str]]:
        """The model/effort a row is on right now: the newest statusline capture when one exists,
        else the transcript's latest turn -- the only source a Desktop-app session (no
        `bin/statusline_capture`) ever has. `cwd` is optional so `action_model`/`action_effort`
        (only reachable for a typeable row, which always has a live window and so always has a
        statusline) can keep calling this with just the session id."""
        data = hud_sources.statusline_for(self.cfg.statusline_dir, session_id)
        if not data and cwd:
            info = transcripts_mod.read_transcript(session_id, cwd)
            if info.model:
                return info.model, info.effort
        model = (data.get("model") or {}).get("display_name") or (data.get("model") or {}).get("id")
        effort = (data.get("effort") or {}).get("level")
        return model, effort

    def action_model(self) -> None:
        row = self.selected()
        if row is None:
            return self.say("no agent selected")
        target = self._target_for(row)
        if target is None:
            return self.say(NOT_TYPEABLE, role="warning")
        current, _effort = self._current_model_effort(row.session_id)
        model_picker.open_picker(self.app, "claude", "pick a model", current,
                                 functools.partial(self._model_picked, row.session_id, row.window_index, target),
                                 cfg=self.cfg)

    def _model_picked(self, session_id: str, window_index: int, target: str, alias: Optional[str]) -> None:
        if alias is None:
            return
        refusal = session_ctl.set_model(target, alias, tmux=self.cfg.tools.tmux)
        if refusal:
            return self.say(refusal, role="warning")
        self._record_control(session_id, f"/model {alias}")
        self.say(f"asked window {window_index} to switch to {alias}; "
                f"it confirms within about {CONFIRM_TIMEOUT_SECONDS} seconds")
        self._poll_confirm(session_id, window_index, "model", alias)

    def action_effort(self) -> None:
        row = self.selected()
        if row is None:
            return self.say("no agent selected")
        target = self._target_for(row)
        if target is None:
            return self.say(NOT_TYPEABLE, role="warning")
        _model, current = self._current_model_effort(row.session_id)
        options = numbered([(lvl, lvl) for lvl in EFFORT_LEVELS])
        self.app.push_screen(
            Pick("pick an effort level", options, current=current),
            functools.partial(self._effort_picked, row.session_id, row.window_index, target),
        )

    def _effort_picked(self, session_id: str, window_index: int, target: str, level: Optional[str]) -> None:
        if level is None:
            return
        refusal = session_ctl.set_effort(target, level, tmux=self.cfg.tools.tmux)
        if refusal:
            return self.say(refusal, role="warning")
        self._record_control(session_id, f"/effort {level}")
        self.say(f"asked window {window_index} to switch to {level}; "
                f"it confirms within about {CONFIRM_TIMEOUT_SECONDS} seconds")
        self._poll_confirm(session_id, window_index, "effort", level)

    def action_mode(self) -> None:
        row = self.selected()
        if row is None:
            return self.say("no agent selected")
        target = self._target_for(row)
        if target is None:
            return self.say(NOT_TYPEABLE, role="warning")
        options = [PickOption(k, v, v) for k, v in MODE_OPTIONS]
        self.app.push_screen(
            Pick("pick a permission mode", options, current=row.mode),
            functools.partial(self._mode_picked, row.session_id, row.window_index, target, row.mode),
        )

    def _mode_picked(
        self, session_id: str, window_index: int, target: str, current: Optional[str], wanted: Optional[str]
    ) -> None:
        if wanted is None:
            return
        refusal = session_ctl.set_mode(target, current, wanted, tmux=self.cfg.tools.tmux)
        if refusal:
            return self.say(refusal, role="warning")
        self._record_control(session_id, f"Shift+Tab toward {wanted}")
        # Confirmation for a mode change is the next hook event's `permission_mode` --
        # the periodic `refresh_rows` already picks that up, so there is nothing to poll here.
        self.say(f"asked window {window_index} to switch to {wanted} mode; "
                f"it confirms on that window's next step")

    def _record_control(self, session_id: str, message: str) -> None:
        """Every typed command is logged so the supervisor and a later session can
        see it -- this never changes what is drawn; `refresh_rows` on its own timer does that."""
        try:
            events_mod.append_event(
                self.cfg.events_file,
                Event(ts=utcnow_iso(), event="control", source="pantheon",
                      session_id=session_id, message=message),
            )
        except OSError as exc:
            self._log(exc)

    def _poll_confirm(self, session_id: str, window_index: int, field: str, wanted: str,
                      _deadline: Optional[float] = None) -> None:
        """Re-reads the statusline capture every `CONFIRM_POLL_SECONDS` for up to
        `CONFIRM_TIMEOUT_SECONDS`. A timer, not a worker: the read is one small local
        JSON file (T13 in the Textual research)."""
        import time as _time

        deadline = _deadline if _deadline is not None else _time.monotonic() + CONFIRM_TIMEOUT_SECONDS
        data = hud_sources.statusline_for(self.cfg.statusline_dir, session_id)
        if _confirmed(data, field, wanted):
            return self.say(f"window {window_index} is now on {wanted}")
        if _time.monotonic() >= deadline:
            return self.say(
                f"window {window_index} did not confirm the change; press j and look at it",
                role="warning",
            )
        self.set_timer(CONFIRM_POLL_SECONDS,
                       functools.partial(self._poll_confirm, session_id, window_index, field, wanted, deadline))

    # ------------------------------------------------------------------ hand-off / resume

    def action_hand_off(self) -> None:
        """The trade-off button: hand a dead/parked/failed
        row's work to the other provider. Fetches the digest in a thread (it shells out to
        PowerShell for a Claude session -- `governor.handoff.digest_for`), then confirms with the
        first 12 lines shown, per S-UI section 6.2."""
        row = self.selected()
        if row is None:
            return self.say("no agent selected")
        to_provider = "codex" if row.provider != "codex" else "claude"
        if to_provider not in self.providers:
            return self.say(f"'{to_provider}' is not switched on in pantheon.toml", role="warning")
        self.say(f"reading what {row.project or 'that agent'} was doing...")
        self.run_worker(functools.partial(self._fetch_digest, row, to_provider), thread=True)

    def _fetch_digest(self, row: AgentState, to_provider: str) -> None:
        digest, source_sentence = handoff_mod.digest_for(row, self.cfg)
        self.app.call_from_thread(
            self._show_handoff_confirm, row.session_id, to_provider, digest, source_sentence
        )

    def _show_handoff_confirm(
        self, session_id: str, to_provider: str, digest: str, source_sentence: str
    ) -> None:
        row = next((r for r in self.rows if r.session_id == session_id), None)
        if row is None:
            return self.say("that agent is gone now")
        label = row.tracker_id or row.project or row.session_id[:8]
        body = digest if digest else source_sentence
        preview = "\n".join(body.splitlines()[:12])
        question = (f'hand "{label}" to {to_provider.capitalize()}? '
                    "It gets the task briefing plus what the last agent did.")
        self.app.push_screen(
            Confirm(question, yes_label="Yes, hand it over (y)", no_label="No (n)", body=preview,
                   danger=True),
            functools.partial(self._handoff_confirmed, session_id, to_provider, digest, source_sentence),
        )

    def _handoff_confirmed(
        self, session_id: str, to_provider: str, digest: str, source_sentence: str,
        confirmed: Optional[bool],
    ) -> None:
        if not confirmed:
            return self.say("left it as is")
        row = next((r for r in self.rows if r.session_id == session_id), None)
        if row is None:
            return self.say("that agent is gone now")
        self.say(f"handing {row.project or 'that work'} to {to_provider}...")
        # In a thread, like the queue's dispatch: a Claude launch waits on tmux for up to a
        # minute (window, prompt, briefing), and the deck must not freeze while it does.
        self.run_worker(
            functools.partial(self._run_handoff, row, to_provider, digest, source_sentence),
            thread=True,
        )

    def _run_handoff(self, row: AgentState, to_provider: str, digest: str, source_sentence: str) -> None:
        try:
            result = handoff_mod.handoff(
                row, to_provider, self.cfg, self.providers, list(self.rows),
                digest_fn=lambda _row, _cfg: (digest, source_sentence),
            )
        except Exception as exc:
            self._log(exc)
            self.app.call_from_thread(
                self.say, f"the hand-off did not start; what went wrong is in {self.cfg.log_file}", "warning"
            )
            return
        self.app.call_from_thread(self._handoff_done, result)

    def _handoff_done(self, result) -> None:
        self.say(result.message or ("handed off" if result.ok else "hand-off did not start"),
                role=None if result.ok else "warning")
        self.refresh_rows()

    # ------------------------------------------------------------------ hand off here

    def action_handoff(self) -> None:
        """`H`: the user decided a healthy Claude session went off the rails. Confirm, type
        `/checkpoint` into it, wait for that to land, then open the New-session steps for the
        same folder with the last launch's model/effort/mode highlighted. Never closes the old
        window -- `k` is still the only thing that does."""
        row = self.selected()
        if row is None:
            return self.say("no agent selected")
        if row.status not in HANDOFF_STATES or row.provider == "codex":
            return self.say(HANDOFF_WRONG_ROW, role="warning")
        if self._target_for(row) is None:
            return self.say(NOT_TYPEABLE, role="warning")
        if "claude" not in self.providers:
            return self.say("'claude' is not switched on in pantheon.toml", role="warning")
        label = row.project or "this session"
        if row.session_id in self._handoff_waits:
            return self.say(f"already waiting for {label} to finish its checkpoint")
        question = (f'restart "{label}"? It gets asked to /checkpoint; its window stays open. '
                    "You pick the new session's model and effort next.")
        self.app.push_screen(
            Confirm(question, yes_label="Yes, checkpoint it (y)", no_label="No (n)"),
            functools.partial(self._handoff_here_confirmed, row.session_id),
        )

    def _handoff_here_confirmed(self, session_id: str, confirmed: Optional[bool]) -> None:
        if not confirmed:
            return self.say("left it as is")
        row = next((r for r in self.rows if r.session_id == session_id), None)
        target = self._target_for(row) if row is not None else None
        if row is None or target is None:
            return self.say("that agent is gone now")
        refusal = session_ctl.request_checkpoint(target, tmux=self.cfg.tools.tmux)
        if refusal:
            return self.say(refusal, role="warning")
        self._record_control(session_id, "/checkpoint (hand-off to a fresh session)")
        import time as _time

        grace = max(1, int(self.cfg.governor_settings().grace_seconds))
        self._handoff_waits[session_id] = (_time.monotonic() + grace, datetime.now(timezone.utc))
        minutes = max(1, round(grace / 60))
        self.say(f"asked {row.project or 'it'} to /checkpoint; the new-session steps open when it "
                 f"has saved (waiting up to {minutes} min)")
        self._poll_handoff(row)

    def _poll_handoff(self, row: AgentState) -> None:
        """Every `HANDOFF_POLL_SECONDS` until the old session has checkpointed (or ended) or the
        governor's `grace_seconds` runs out -- the one dial for "how long a checkpoint gets"."""
        import time as _time

        wait = self._handoff_waits.get(row.session_id)
        if wait is None:
            return
        deadline, since = wait
        try:
            events, _errors = eventcache_mod.read_events_cached(self.cfg.events_file)
        except OSError as exc:
            self._log(exc)
            events = []
        settled = restart_mod.checkpoint_settled(row.session_id, since, events,
                                                 _checkpoint_mtime(row.cwd))
        label = row.project or "that session"
        if settled:
            self._handoff_waits.pop(row.session_id, None)
            self.say(f"{label} saved its checkpoint; pick the new session"
                     if settled == "saved" else f"{label} has ended; pick the new session")
            return self._open_restart_chain(row, events)
        if _time.monotonic() >= deadline:
            self._handoff_waits.pop(row.session_id, None)
            where = f"window {row.window_index}" if row.window_index is not None else "its window"
            return self.say(f"no checkpoint seen from {label} in time; {where} is still open and "
                            "nothing was closed", role="warning")
        self.set_timer(HANDOFF_POLL_SECONDS, functools.partial(self._poll_handoff, row))

    def _open_restart_chain(self, row: AgentState, events: list[Event]) -> None:
        """the ordinary New-session steps, already past "which folder" and
        "who" (this row's folder, Claude), opening on the model screen with the folder's last
        launch choices highlighted. Escape walks back exactly as says -- to "who works here",
        then the folder list."""
        _provider, interactive = NEW_SESSION_VALUES["claude"]
        self._ns = {"project_dir": row.cwd, "who": "claude", "provider": "claude",
                    "interactive": interactive, "resume": False, "carry_from": row}
        self._ns.update(restart_mod.last_options_for(row.cwd, events))
        self._ns_open("model")

    def _carry_offer_pending(self) -> bool:
        """The digest question comes up once, right before the first-message box, and only while
        the flow is still on the folder the old session was in."""
        ns = self._ns
        row = ns.get("carry_from")
        return (row is not None and "carry" not in ns
                and events_mod.norm_path(ns.get("project_dir")) == events_mod.norm_path(row.cwd))

    def _ask_carry(self) -> None:
        name = Path(self._ns["project_dir"]).name
        options = [PickOption("d", "digest", "carry a digest of what the last session did"),
                   PickOption("b", "blank", "start with an empty message box")]
        self.app.push_screen(Pick(f"first message for {name}  (Esc = back)", options, current="blank"),
                             self._carry_got)

    def _carry_got(self, value: Optional[str]) -> None:
        if value is None:
            return self._ns_back("message")
        self._ns["carry"] = value
        if value != "digest":
            return self._ns_open("message")
        self.say("reading what the last session did...")
        # A thread: `digest_for` shells out to PowerShell for a Claude transcript (up to 30 s).
        self.run_worker(functools.partial(self._fetch_carry_digest, self._ns["carry_from"]), thread=True)

    def _fetch_carry_digest(self, row: AgentState) -> None:
        try:
            digest, source_sentence = handoff_mod.digest_for(row, self.cfg)
        except Exception as exc:
            self._log(exc)
            digest, source_sentence = "", handoff_mod.NOT_FOUND_SENTENCE
        self.app.call_from_thread(self._carry_ready, digest, source_sentence)

    def _carry_ready(self, digest: str, source_sentence: str) -> None:
        if digest:
            self._ns["message_seed"] = ("Continuing from the previous session in this folder. "
                                        f"Digest of what it did:\n{digest}")
            self.say(f"the message box holds a {source_sentence}; edit it or press Enter")
        else:
            self.say(source_sentence, role="warning")
        self._ns_open("message")

    def action_log(self) -> None:
        row = self.selected()
        if row is None:
            return self.say("no agent selected")
        log_path = Path(self.cfg.dispatch_dir) / f"{row.job_id}.log" if row.job_id else None
        if log_path is None or not log_path.exists():
            return self.say("no log file recorded for that job", role="warning")
        index = tmuxctl.open_or_reuse_shell(self.cfg.tmux_session, str(log_path.parent),
                                            tmux=self.cfg.tools.tmux)
        if index is None:
            return self.say("could not open the shell window", role="warning")
        tmuxctl.send_text(f"{self.cfg.tmux_session}:{index}", f'less "{log_path}"',
                          tmux=self.cfg.tools.tmux)
        self.say(f"opened the log in the shell window")

    # ------------------------------------------------------------------ keys

    @property
    def keys_open(self) -> bool:
        return bool(self.query_one("#keys", Static).display)

    def _keys_page_text(self, page: int) -> str:
        """Page 0 is the key list; page 1 is the connector gap list. Read fresh each time it opens -- cheap local file reads, and a
        session that just ran `claude mcp add` should not have to restart the deck to see it."""
        if page == 0:
            return keys_mod.section("deck")
        return connectors_mod.report(str(Path.cwd()))

    def toggle_keys(self) -> None:
        """`?`: open to page 0, press again for page 1 (connectors), a third press closes."""
        panel = self.query_one("#keys", Static)
        if not panel.display:
            panel.display = True
            self._keys_page = 0
        elif self._keys_page == 0:
            self._keys_page = 1
        else:
            panel.display = False
            self._keys_page = 0
        panel.update(self._keys_page_text(self._keys_page))
        self.query_one("#agents", DataTable).display = not panel.display

    def action_jump(self) -> None:
        row = self.selected()
        if row is None:
            return self.say("no agent selected")
        if row.in_pantheon and row.window_index is not None:
            if tmuxctl.select_window(self.cfg.tmux_session, row.window_index, self.cfg.tools.tmux):
                self._hint_back_key(row.window_index)
                return self.say(f"jumped to window {row.window_index}")
            return self.say("tmux would not switch window", role="warning")
        w = self._window_for(row)
        if w is not None and w.session and tmuxctl.switch_client(
            w.session, self.cfg.tools.tmux, from_session=self.cfg.tmux_session
        ):
            return self.say(f"jumped to session {w.session}")
        if row.window_index is None:
            return self.say(NOT_IN_TMUX_JUMP, role="warning")
        self.say("that agent is in another tmux - could not switch to it", role="warning")

    def _hint_back_key(self, window_index: int) -> None:
        """the first thing the user used to see after `j` landed him in a stock agent
        window was tmux's plain status line -- no signpost at all back to the deck. One `display-message` on the window he just landed in,
        naming the configured back key (`[keys] back_to_deck`, F12 default), fixes that without
        needing the `?` overlay opened from inside an agent session."""
        key = self.cfg.keys_settings().back_to_deck
        target = f"{self.cfg.tmux_session}:{window_index}"
        tmuxctl.display_message(f"press {key} to go back to the deck", target=target, tmux=self.cfg.tools.tmux)

    def action_kill(self) -> None:
        """`k`, or the toolbar's `Kill` button: a `Confirm` modal, `y`/`n` still work as its
        accelerators."""
        row = self.selected()
        if row is None:
            return self.say("no agent selected")
        if row.window_index is None:
            return self.say(NOT_IN_TMUX_KILL, role="warning")
        if not row.in_pantheon:
            return self.say(OTHER_TMUX_KILL, role="warning")
        question = f"close window {row.window_index} ({row.project or 'this agent'})?"
        self.app.push_screen(
            Confirm(question, yes_label="Yes, close it (y)", no_label="No (n)", danger=True),
            functools.partial(self._kill_confirmed, row.session_id),
        )

    def _kill_confirmed(self, session_id: str, confirmed: Optional[bool]) -> None:
        if not confirmed:
            return self.say("left it running")
        self._do_kill(session_id)

    def _do_kill(self, session_id: str) -> None:
        row = next((r for r in self.rows if r.session_id == session_id), None)
        if row is None or row.window_index is None:
            return self.say("that agent is already gone")
        ok = tmuxctl.kill_window(self.cfg.tmux_session, row.window_index, self.cfg.tools.tmux)
        if not ok:
            return self.say("tmux would not close that window", role="warning")
        try:
            events_mod.append_event(
                self.cfg.events_file,
                Event(
                    ts=utcnow_iso(),
                    event="kill",
                    source="pantheon",
                    session_id=row.session_id,
                    cwd=row.cwd,
                    project=row.project,
                    message="closed from the deck",
                ),
            )
        except OSError as exc:
            self._log(exc)
        self.say(f"closed window {row.window_index}")
        self.refresh_rows()

    def action_open_folder(self) -> None:
        row = self.selected()
        if row is None:
            return self.say("no agent selected")
        if not row.cwd:
            return self.say("no folder recorded for that agent")
        index = tmuxctl.open_or_reuse_shell(self.cfg.tmux_session, row.cwd, tmux=self.cfg.tools.tmux)
        if index is None:
            return self.say("could not open the shell window", role="warning")
        self.say(f"shell window is now in {row.project or 'that folder'}")

    def action_copy_id(self) -> None:
        row = self.selected()
        if row is None:
            return self.say("no agent selected")
        try:
            subprocess.run([self.cfg.tools.clip], input=row.session_id, text=True, check=True)
        except (OSError, subprocess.SubprocessError) as exc:
            self._log(exc)
            return self.say("could not reach the clipboard program", role="warning")
        self.say(f"copied {row.session_id[:8]} to the clipboard")

    def action_refresh_now(self) -> None:
        self._message = ""
        self._message_role = None
        self.refresh_rows()
