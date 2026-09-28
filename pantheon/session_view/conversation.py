"""`ConversationView` -- one session's transcript drawn the way Claude Desktop draws a chat
.

The aim: the condensed view Claude Desktop shows instead of the verbose terminal one, with
thinking one click away. So: the user's own messages in a band, the assistant's prose as Markdown, every
tool call as ONE line (`Read CHECKPOINT.md`) whose output is folded away until it is opened, and
thinking / hook receipts / subagent runs folded by default too.

Consumes `models.py` only -- never the parser -- so a test can build any conversation by hand.

Design rules that bind here:
- R3/R5 three tiers, and colour is never the only signal: a failed tool call prints the WORD
  `error` in its chip line as well as taking the red-orange colour.
- R9 no idle motion: every scroll is `animate=False`; nothing spins, nothing shimmers.
- R10 the pre-attentive budget: inside a tile only "this went wrong" gets colour; chips,
  thinking and system notes are dim.

CSS caution: a widget's DEFAULT_CSS is parsed before any theme
is applied, so only Textual's own built-in variables ($primary, $panel, $text-muted, ...) or a
literal hex from `theme.TOKENS` may appear here. A custom `$var` raises at mount.
"""
from __future__ import annotations

import re

from typing import Optional

from textual.containers import VerticalScroll
from textual.widget import Widget
from textual.widgets import Collapsible, Markdown, Static

from . import models as m

# How many items a tile draws. A compact tile in the grid shows the tail of the conversation --
# enough to see what the agent is doing now; the expanded tile shows a real backlog. Older items
# are not lost: PageUp / Home at the top of the view loads another window of them.
TILE_ITEMS_COMPACT = 12
TILE_ITEMS_EXPANDED = 200

RUNNING_TEXT = "running…"
NO_ITEMS_TEXT = "no messages yet"


def _one_line(text: str, width: int = m.CHIP_CHARS) -> str:
    """Collapse a possibly-multiline string to one line, clipped -- used for every folded title,
    so a hook receipt with ten lines of output cannot push the layout around."""
    flat = " ".join((text or "").split())
    return flat[: width - 1] + "…" if len(flat) > width else flat


def _tool_title(tool: m.ToolCall) -> str:
    """The chip line. An errored call says the word `error` as well as taking the error colour
   ."""
    summary = _one_line(tool.summary or tool.name or "tool")
    if tool.is_error:
        return f"{summary}  — error"
    return summary


class ConversationView(VerticalScroll):
    """A scrolling column of one child widget per `Item`.

    `set_conversation(conv, expanded)` is the only entry point the deck uses. It diffs by
    `Item.uuid` (falling back to the item's index in the transcript) so the 5-second refresh
    tick mounts ONE widget for ONE new item instead of rebuilding the tree -- that is the
    difference between a deck that stays responsive with ten busy sessions and the "slow or
    laggy" rejection criterion.
    """

    DEFAULT_CSS = """
    /* `align-vertical: bottom`: a short conversation sits ON the message box instead of leaving
       a field of empty space above it. A long one scrolls exactly as before. */
    ConversationView {
        height: 1fr; width: 1fr; scrollbar-size-vertical: 1; align-vertical: bottom;
    }

    ConversationView .cv-user {
        background: $primary-muted; color: $text; padding: 0 1; margin: 1 0 0 0; width: 1fr;
    }
    ConversationView .cv-assistant { margin: 0 0 1 0; padding: 0 1; width: 1fr; }
    ConversationView Markdown { margin: 0; padding: 0; }
    ConversationView MarkdownBlock { margin: 0; padding: 0; }

    ConversationView Collapsible { border: none; padding: 0; margin: 0; background: $surface; }
    ConversationView Collapsible CollapsibleTitle { color: $text-muted; padding: 0 1; }
    ConversationView Collapsible Contents { padding: 0 0 0 3; }
    ConversationView .cv-body { color: $text-muted; width: 1fr; }

    ConversationView .cv-tool CollapsibleTitle { color: $text; }
    ConversationView .cv-tool-error CollapsibleTitle { color: #D55E00; text-style: bold; }
    ConversationView .cv-tool-error .cv-body { color: #D55E00; }
    ConversationView .cv-thinking CollapsibleTitle { color: #5C7480; }
    ConversationView .cv-system CollapsibleTitle { color: #5C7480; }
    ConversationView .cv-subagent CollapsibleTitle { color: $text-muted; }

    ConversationView .cv-empty { color: $text-muted; padding: 1 1; }
    """

    def __init__(self, conversation: Optional[m.Conversation] = None, expanded: bool = False,
                 id: Optional[str] = None, classes: Optional[str] = None) -> None:
        super().__init__(id=id, classes=classes)
        self._conv: Optional[m.Conversation] = conversation
        self._expanded = expanded
        # Extra items the reader asked for with PageUp / Home at the top of the view.
        self._extra = 0
        # The window currently on screen, parallel lists: diff key -> Item -> child widget.
        self._keys: list[str] = []
        self._items: list[m.Item] = []
        self._empty: Optional[Static] = None

    # ---------------------------------------------------------------- lifecycle

    def on_mount(self) -> None:
        self._redraw()

    # ---------------------------------------------------------------- public API

    def set_conversation(self, conv: Optional[m.Conversation], expanded: bool = False) -> None:
        """Draw `conv`. Cheap to call on a timer: an append mounts only the appended items."""
        if expanded != self._expanded:
            # Changing tier changes the window size, so the old diff no longer applies.
            self._expanded = expanded
            self._extra = 0
            self._keys = []
            self._items = []
            if self.is_mounted:
                self.remove_children()
        self._conv = conv
        if self.is_mounted:
            self._redraw()

    @property
    def item_limit(self) -> int:
        base = TILE_ITEMS_EXPANDED if self._expanded else TILE_ITEMS_COMPACT
        return base + self._extra

    # ---------------------------------------------------------------- earlier items

    def on_key(self, event) -> None:
        """PageUp / Home while already at the top loads an earlier window instead of doing
        nothing. Anywhere else they scroll as usual (the event is left alone)."""
        if event.key not in ("pageup", "home"):
            return
        if self.scroll_offset.y > 0:
            return
        if self._grow():
            event.stop()
            event.prevent_default()

    def _grow(self) -> bool:
        """Widen the window by one more page of items. False when everything is already shown."""
        conv = self._conv
        if conv is None or len(conv.items) <= self.item_limit:
            return False
        self._extra += TILE_ITEMS_EXPANDED if self._expanded else TILE_ITEMS_COMPACT
        # The window's left edge moved, so the diff keys no longer line up: redraw the column.
        self._keys = []
        self._items = []
        self.remove_children()
        self._redraw(follow=False)
        return True

    # ---------------------------------------------------------------- drawing

    def _window(self) -> tuple[list[str], list[m.Item]]:
        conv = self._conv
        items = list(conv.items) if conv is not None else []
        start = max(0, len(items) - self.item_limit)
        window = items[start:]
        keys = [(it.uuid or f"#{start + n}") for n, it in enumerate(window)]
        return keys, window

    def _redraw(self, follow: Optional[bool] = None) -> None:
        keys, window = self._window()
        at_end = self.is_vertical_scroll_end if follow is None else follow

        if not window:
            if self._keys:
                self.remove_children()
                self._keys, self._items = [], []
            if self._empty is None or not self._empty.is_mounted:
                self._empty = Static(NO_ITEMS_TEXT, markup=False, classes="cv-empty")
                self.mount(self._empty)
            return
        if self._empty is not None and self._empty.is_mounted:
            self._empty.remove()
            self._empty = None

        appended_only = bool(self._keys) and keys[: len(self._keys)] == self._keys
        if appended_only:
            # In-place changes first (a TOOL item whose result finally arrived), then the tail.
            for idx, item in enumerate(window[: len(self._items)]):
                if item != self._items[idx]:
                    self._refresh_child(idx, item)
                    self._items[idx] = item
            fresh = window[len(self._keys):]
            if fresh:
                self.mount_all([self._make(it) for it in fresh])
                self._keys = keys
                self._items = window
        else:
            self.remove_children()
            self.mount_all([self._make(it) for it in window])
            self._keys = keys
            self._items = window

        if at_end:
            # R9: never animated.
            self.call_after_refresh(self.scroll_end, animate=False)

    def _child_at(self, idx: int) -> Optional[Widget]:
        kids = [c for c in self.children if c is not self._empty]
        return kids[idx] if 0 <= idx < len(kids) else None

    def _refresh_child(self, idx: int, item: m.Item) -> None:
        """Update a widget in place. The common case by far is a TOOL whose result arrived:
        its Collapsible keeps its identity and only its title
        and body change."""
        child = self._child_at(idx)
        if child is None:
            return
        if item.kind == m.TOOL and isinstance(child, Collapsible) and item.tool is not None:
            child.title = _tool_title(item.tool)
            child.set_class(item.tool.is_error, "cv-tool-error")
            try:
                body = child.query_one("#tool-result", Static)
            except Exception:
                return
            body.update(item.tool.result_text if item.tool.result_text is not None else RUNNING_TEXT)
            return
        # Anything else: swap the widget for a freshly built one at the same position.
        new = self._make(item)
        self.mount(new, after=child)
        child.remove()

    # ---------------------------------------------------------------- one widget per kind

    def _make(self, item: m.Item) -> Widget:
        kind = item.kind
        if kind == m.USER:
            return Static(f"you › {_one_line(item.text, 4_000)}", markup=False, classes="cv-user")
        if kind == m.ASSISTANT:
            return Markdown(item.text or "", classes="cv-assistant")
        if kind == m.TOOL:
            return self._tool_widget(item)
        if kind == m.THINKING:
            return Collapsible(
                Static(item.text or "", markup=False, classes="cv-body"),
                title="Thinking…", collapsed=True, classes="cv-thinking",
            )
        if kind == m.SUBAGENT:
            return Collapsible(
                Static(item.detail or item.text or "", markup=False, classes="cv-body"),
                title=_one_line("subagent: " + re.sub(r"^\s*subagent:\s*", "", item.text or "")), collapsed=True,
                classes="cv-subagent",
            )
        # SYSTEM, and anything a future parser adds: a dim folded one-liner with its body inside.
        return Collapsible(
            Static(item.detail or item.text or "", markup=False, classes="cv-body"),
            title=_one_line(item.text or "note"), collapsed=True, classes="cv-system",
        )

    def _tool_widget(self, item: m.Item) -> Collapsible:
        tool = item.tool
        if tool is None:  # a malformed item: draw it as a plain folded note rather than crash.
            return Collapsible(
                Static(item.text or "", markup=False, classes="cv-body"),
                title=_one_line(item.text or "tool"), collapsed=True, classes="cv-tool",
            )
        classes = "cv-tool cv-tool-error" if tool.is_error else "cv-tool"
        result = tool.result_text if tool.result_text is not None else RUNNING_TEXT
        return Collapsible(
            Static(tool.input_text or "", markup=False, id="tool-input", classes="cv-body"),
            Static(result, markup=False, id="tool-result", classes="cv-body"),
            title=_tool_title(tool), collapsed=True, classes=classes,
        )


__all__ = [
    "ConversationView",
    "TILE_ITEMS_COMPACT",
    "TILE_ITEMS_EXPANDED",
    "RUNNING_TEXT",
    "NO_ITEMS_TEXT",
]
