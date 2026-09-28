"""The one sentence the deck's status line shows when a notification just went out while the
deck was open.

Rare by construction -- open + present is normally suppressed, so this only shows up when a
Warning-tier alert fires regardless of presence (a Codex job failing) or when the user is at the
desk but past `present_seconds` with no recent key/mouse activity. This module is a formatter
only, mirroring `hud/line.py`'s shape; `deck/app.py`
is what will call `sent_line` and put the result next to the HUD's other attention lines, and
`?` there gets one more entry glossing `notify`.
"""
from __future__ import annotations

MAX_CHARS = 70


def sent_line(title: str, body: str, channel: str, detail: str = "", max_chars: int = MAX_CHARS) -> str:
    """`sent: <title> - <body> -> <channel>`, clipped to `max_chars`. `detail` is the deck-only context a
    pushed notification never carries (a log path, a session id -- see `rules.Notification`'s
    docstring, the OpenClaw lock-screen-text rule); the DECK is allowed to show it, so it is
    appended here in parentheses when present."""
    text = f"sent: {title} - {body}" + (f" ({detail})" if detail else "") + f" -> {channel}"
    return text[:max_chars]
