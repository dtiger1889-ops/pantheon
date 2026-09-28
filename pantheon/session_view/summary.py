"""What a session shows at a glance in a COMPACT pit tile.

Pure functions over `models.py` so the tile widget stays dumb and every rule is unit-tested
without a terminal. The tile renders these; the EXPANDED tile still draws the full transcript
through `conversation.ConversationView`.
"""
from __future__ import annotations

from typing import Optional

from . import models as m

# A readable preview of the agent's latest message: it WRAPS in the tile (never one clipped line),
# and is cut only so one enormous message cannot crowd every other tile off the screen.
LATEST_CHARS = 600
# The user's own line above it is context, not the point -- one short line is enough.
YOU_CHARS = 160


def status_word(entry: m.SessionEntry) -> str:
    """Plain status: 'needs you', else the folded status label the pit already computed, else
    'idle'. Always a word -- colour is only ever the second signal."""
    if entry.needs_human:
        return "needs you"
    return (entry.status_text or "").strip() or "idle"


def status_token(entry: m.SessionEntry) -> str:
    """`theme.TOKENS` key for the status colour."""
    if entry.needs_human:
        return "warning"
    if entry.style == "error":
        return "error"
    if entry.style == "muted":
        return "muted"
    return "accent"


def activity_detail(conv: Optional[m.Conversation]) -> str:
    """What it's doing right now, read off the tail of the transcript: a running tool, thinking,
    or the most recent action. Empty when the newest item is a finished message -- that message
    is shown on its own below, so repeating it here would be noise."""
    if conv is None or not conv.items:
        return ""
    last = conv.items[-1]
    if last.kind == m.TOOL and last.tool is not None:
        verb = "" if last.tool.done else "running "
        return verb + (last.tool.summary or last.tool.name or "a tool")
    if last.kind == m.THINKING:
        return "thinking"
    return ""


def _last_index(items, kind) -> Optional[int]:
    found = None
    for i, it in enumerate(items):
        if it.kind == kind and (it.text or "").strip():
            found = i
    return found


def latest_exchange(conv: Optional[m.Conversation]) -> tuple[Optional[str], Optional[str]]:
    """`(you_said, it_said)` -- the most recent user line and the assistant's latest prose, so a
    compact tile reads like one row of a chat list. When a user line is the newest speech (no
    reply has come back yet) `it_said` is None and only the ask shows."""
    if conv is None or not conv.items:
        return None, None
    items = conv.items
    a = _last_index(items, m.ASSISTANT)
    u = _last_index(items, m.USER)
    if u is not None and (a is None or u > a):
        return items[u].text, None
    it_said = items[a].text if a is not None else None
    you_said = None
    if a is not None:
        for j in range(a - 1, -1, -1):
            if items[j].kind == m.USER and (items[j].text or "").strip():
                you_said = items[j].text
                break
    return you_said, it_said


def clip(text: Optional[str], limit: int = LATEST_CHARS) -> str:
    """Trim to `limit`, on a whole word where possible, with a trailing ellipsis -- the preview
    still WRAPS in the tile; this only bounds a runaway message."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[: limit - 1].rstrip()
    space = cut.rfind(" ")
    if space >= limit // 2:
        cut = cut[:space]
    return cut.rstrip() + "…"
