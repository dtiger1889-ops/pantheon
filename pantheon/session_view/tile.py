"""`SessionTile` -- one session in the pit, drawn as a chat card.

The deck lays a grid of these where THE PIT's table and its empty detail box used to sit
.

A COMPACT tile is a readable chat row:

    Resume the diagnosis…                                     project_lanterns
    ● working · running Bash: git status --short
    you › restart the deck
    Pushed. Restarting the deck so the fixed sidebar shows on your desk now…

The EXPANDED tile (Enter) swaps that summary for the full transcript and a send box:

    Fix the launcher                 habit_notes · opus-5 · high · ctx 43%
    ⚠ needs you                                       (only when it does)
    <the conversation>
    > message this session…          (expanded + typable) or a dim read-only line

Compact vs expanded is a CSS class the deck toggles; the tile owns what each looks like.
The message box types straight into the session's tmux window through `tmuxctl.send_text` --
the same call `j` uses to jump there -- so a Desktop-app session or a Codex job, which has no
window to type into, gets a plain read-only line in its place instead of a box that silently
does nothing.

CSS caution (theme.py): only Textual's built-in variables or literal hex from `theme.TOKENS`.
"""
from __future__ import annotations

from typing import Optional

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Input, Static

from .. import theme as theme_mod
from .. import tmuxctl
from . import context as context_mod
from . import models as m
from . import summary as summary_mod
from .conversation import ConversationView

# The context window / percentage rules moved to `context.py`;
# the names stay importable from here for existing callers.
CONTEXT_WINDOW = context_mod.CONTEXT_WINDOW
CONTEXT_WINDOW_1M = context_mod.CONTEXT_WINDOW_1M
CONTEXT_WINDOW_CODEX = context_mod.CONTEXT_WINDOW_CODEX
context_window_for = context_mod.context_window_for

NEEDS_YOU_TEXT = "⚠ needs you"
NO_MESSAGES_TEXT = "no messages yet"
WAITING_TEXT = "running…"
READ_ONLY_DESKTOP = "read-only · runs in the Desktop app"
READ_ONLY_CODEX = "read-only · Codex job"
INPUT_PLACEHOLDER = "message this session…"


def context_window_for(model: Optional[str]) -> int:
    if not model:
        return CONTEXT_WINDOW
    low = model.lower()
    if "[1m]" in low or "fable" in low:
        return CONTEXT_WINDOW_1M
    if "gpt" in low or "codex" in low or "astra" in low:
        return CONTEXT_WINDOW_CODEX
    return CONTEXT_WINDOW


def context_percent(conv: Optional[m.Conversation], statusline: Optional[dict] = None) -> Optional[int]:
    """Whole percent of the model's context the latest turn was carrying, or None when the
    transcript has not told us yet or the number would pass 100 (`context.py`)."""
    if conv is None:
        return None
    return context_mod.context_percent(conv.context_tokens, conv.model, statusline)


def header_caption(entry: m.SessionEntry, conv: Optional[m.Conversation]) -> str:
    """`habit_notes · opus-5 · high · ctx 43%` -- every part dropped when it is unknown, so the
    line never prints a placeholder dash the user has to decode."""
    parts = [entry.project or "?"]
    if conv is not None:
        if conv.model:
            parts.append(conv.model)
        if conv.effort:
            parts.append(conv.effort)
        ctx = context_mod.context_caption(
            conv.context_tokens, conv.model, context_mod.statusline_for_session(entry.session_id))
        if ctx:
            parts.append(ctx)
    return " · ".join(p for p in parts if p)


def read_only_text(entry: m.SessionEntry) -> str:
    if (entry.provider or "claude").lower() == "codex":
        return READ_ONLY_CODEX
    return READ_ONLY_DESKTOP


class SessionTile(Widget):
    """One session's card. `expanded` is a reactive the deck flips; everything else follows."""

    can_focus = True

    DEFAULT_CSS = """
    SessionTile {
        layout: vertical; height: 1fr; width: 1fr;
        padding: 0 2;
        border: round #1D5B6E;
        border-title-color: #22D3EE;
        background: $surface;
    }
    SessionTile:focus, SessionTile:focus-within { border: heavy $primary; }

    SessionTile #tile-head { height: 1; width: 1fr; }
    /* The caption keeps its full width and the title gives way with an ellipsis: on a narrow
       grid tile the numbers the user scans (ctx %, model) matter more than the last words of a
       title he can read in full once the tile is expanded. */
    SessionTile #tile-title {
        width: 1fr; text-style: bold; color: $text; text-overflow: ellipsis;
    }
    SessionTile #tile-caption { width: auto; color: $text-muted; text-align: right; }
    SessionTile #tile-needs {
        height: 1; width: 1fr; color: #E69F00; text-style: bold; display: none;
    }
    SessionTile.needs-human #tile-needs { display: block; }

    /* The compact card: status + what it's doing, then the latest message, wrapped and readable
. It replaces the transcript scroll in a compact tile. */
    SessionTile #tile-summary { height: 1fr; width: 1fr; }
    SessionTile #tile-activity { height: 1; width: 1fr; }
    SessionTile #tile-you { height: auto; max-height: 2; width: 1fr; color: $text-muted; margin: 1 0 0 0; }
    SessionTile #tile-latest { height: 1fr; width: 1fr; color: $text; }

    SessionTile #tile-conv { height: 1fr; width: 1fr; }
    /* Compact shows the summary and hides the transcript; expanded is the reverse. */
    SessionTile.compact #tile-conv { display: none; }
    SessionTile.expanded #tile-summary { display: none; }
    /* A hairline above the send box so it reads as a place to type, not as one more line of
       the conversation. */
    SessionTile #tile-readonly {
        height: 2; width: 1fr; color: $text-muted; padding: 0 1;
        border-top: solid #1D5B6E;
    }
    SessionTile #tile-input {
        height: 2; padding: 0 1; background: $panel;
        border: none; border-top: solid #1D5B6E;
    }

    /* A compact tile in the grid is a preview: the send box belongs to the expanded one. */
    SessionTile.compact #tile-input { display: none; }
    SessionTile.compact #tile-readonly { display: none; }
    """

    expanded = reactive(False)

    def __init__(
        self,
        entry: m.SessionEntry,
        conversation: Optional[m.Conversation] = None,
        expanded: bool = False,
        id: Optional[str] = None,
        classes: Optional[str] = None,
    ) -> None:
        super().__init__(id=id, classes=classes)
        self.entry = entry
        self.conversation = conversation
        self._view: Optional[ConversationView] = None
        self.set_reactive(SessionTile.expanded, expanded)

    # ---------------------------------------------------------------- composition

    def compose(self) -> ComposeResult:
        with Horizontal(id="tile-head"):
            yield Static(self.entry.title or self.entry.project or self.entry.session_id,
                         markup=False, id="tile-title")
            yield Static(self._caption(), markup=False, id="tile-caption")
        yield Static(NEEDS_YOU_TEXT, markup=False, id="tile-needs")
        # Compact: the readable summary (status + what it's doing + latest message).
        with Vertical(id="tile-summary"):
            yield Static("", markup=False, id="tile-activity")
            yield Static("", markup=False, id="tile-you")
            yield Static("", markup=False, id="tile-latest")
        # Expanded: the full transcript.
        self._view = ConversationView(self.conversation, expanded=bool(self.expanded),
                                      id="tile-conv")
        yield self._view
        if self.entry.can_type:
            yield Input(placeholder=INPUT_PLACEHOLDER, id="tile-input")
        else:
            yield Static(read_only_text(self.entry), markup=False, id="tile-readonly")

    def on_mount(self) -> None:
        self.set_class(bool(self.entry.needs_human), "needs-human")
        self._apply_tier()
        self._render_summary()

    # ---------------------------------------------------------------- state

    def watch_expanded(self, expanded: bool) -> None:
        if not self.is_mounted:
            return
        self._apply_tier()

    def _apply_tier(self) -> None:
        expanded = bool(self.expanded)
        self.set_class(expanded, "expanded")
        self.set_class(not expanded, "compact")
        view = self._conv_view()
        if view is not None:
            # A compact tile shows the summary, not the transcript, so the (hidden) transcript
            # view is fed nothing -- a wall of compact tiles then renders three small Statics
            # each, not N*12 transcript widgets kept diffed on every tick (the "snappy" bar).
            view.set_conversation(self.conversation if expanded else None, expanded=expanded)
        # The caption is compact-vs-expanded, and the compact card only exists at all when compact
        # -- refresh both so a tier flip repaints them.
        self._refresh_header()
        self._render_summary()

    def _conv_view(self) -> Optional[ConversationView]:
        if self._view is not None and self._view.is_mounted:
            return self._view
        try:
            return self.query_one("#tile-conv", ConversationView)
        except Exception:
            return None

    def set_conversation(self, conv: Optional[m.Conversation]) -> None:
        """The deck's refresh tick: hand the tile a newer `Conversation` for the same session."""
        self.conversation = conv
        view = self._conv_view()
        if view is not None:
            view.set_conversation(conv if self.expanded else None, expanded=bool(self.expanded))
        self._refresh_header()
        self._render_summary()

    def set_entry(self, entry: m.SessionEntry) -> None:
        """State changed (it now needs a human, or its title finally arrived)."""
        self.entry = entry
        self.set_class(bool(entry.needs_human), "needs-human")
        self._refresh_header()
        self._render_summary()

    # ---------------------------------------------------------------- header

    def _caption(self) -> str:
        """Compact: just the project, so the title keeps the room. Expanded: the full `project · model · effort · ctx%` line, where
        there is width for it and he has chosen to look closely."""
        if self.expanded:
            return header_caption(self.entry, self.conversation)
        return self.entry.project or ""

    def _refresh_header(self) -> None:
        """The title sits left and bold, the caption right and dim -- two widgets rather than one
        markup string, so the caption stays pinned to the tile's right edge at any width and a
        title with a `[` in it cannot be read as markup."""
        try:
            self.query_one("#tile-title", Static).update(
                self.entry.title or self.entry.project or self.entry.session_id)
            self.query_one("#tile-caption", Static).update(self._caption())
        except Exception:
            pass

    def _render_summary(self) -> None:
        """Fill the compact card -- status + what it's doing, then the latest message, wrapped and
        readable. Mount-safe and cheap; runs on every refresh tick and tier
        flip. The expanded tile hides this whole block (CSS) and reads the transcript instead."""
        try:
            activity = self.query_one("#tile-activity", Static)
            you = self.query_one("#tile-you", Static)
            latest = self.query_one("#tile-latest", Static)
        except Exception:
            return
        colour = theme_mod.TOKENS.get(summary_mod.status_token(self.entry), theme_mod.TOKENS["dim"])
        dim = theme_mod.TOKENS["dim"]
        detail = summary_mod.activity_detail(self.conversation)
        line = Text()
        line.append("● ", style=colour)
        word_shown = False
        # When it needs a human the ⚠ band already says so in full width -- don't repeat "needs
        # you" one line down. Otherwise lead with the status word.
        if not self.entry.needs_human:
            line.append(summary_mod.status_word(self.entry), style=f"bold {colour}")
            word_shown = True
        if detail:
            if word_shown:
                line.append("  ·  ", style=dim)
            line.append(summary_mod.clip(detail, 80), style=dim)
        if word_shown or detail:
            activity.display = True
            activity.update(line)
        else:
            activity.display = False

        you_said, it_said = summary_mod.latest_exchange(self.conversation)
        if you_said:
            you.display = True
            you.update(Text(f"you › {summary_mod.clip(you_said, summary_mod.YOU_CHARS)}",
                            style=theme_mod.TOKENS["dim"]))
        else:
            you.display = False
            you.update("")
        if it_said:
            latest.update(summary_mod.clip(it_said))
        elif you_said:
            latest.update(Text(WAITING_TEXT, style=theme_mod.TOKENS["dim"]))
        else:
            latest.update(Text(NO_MESSAGES_TEXT, style=theme_mod.TOKENS["dim"]))

    # ---------------------------------------------------------------- the message box

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter in the send box types the line into that session's tmux window, literally
        (`send-keys -l`), then Enter -- never through argv, so a prompt with quotes survives."""
        if event.input.id != "tile-input":
            return
        event.stop()
        text = event.value
        target = self.entry.target
        if not text.strip() or not target:
            return
        tmuxctl.send_text(target, text)
        event.input.value = ""


__all__ = [
    "SessionTile",
    "context_percent",
    "context_window_for",
    "header_caption",
    "read_only_text",
    "CONTEXT_WINDOW",
    "CONTEXT_WINDOW_1M",
    "NEEDS_YOU_TEXT",
    "READ_ONLY_DESKTOP",
    "READ_ONLY_CODEX",
    "INPUT_PLACEHOLDER",
]
