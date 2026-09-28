"""Parses a project `CHECKPOINT.md`'s `## Open threads` section into rows.

Pure text in, structured data out -- no file I/O here, so `checkpoint_source.py` owns reading the
file and this module stays unit-testable on plain strings. This mirrors `basefilter.py`'s split
between the grammar and the module that reads a file off disk.

The convention this parses: a bulleted list under
`## Open threads`, each bullet leading with a bolded title that carries bracket tags --
`[the user]`/`[agent]` for who moves it, `[low]`/`[high]` for priority, and an optional
`[gates: ...]` naming what it is blocked on. Not every project (or every bullet) uses the bracket
vocabulary; a bare `- **Title.**` or even a plain `- Title.` bullet is still a valid thread, just
with `owner="unknown"` and `priority="normal"`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

_SECTION_RE = re.compile(r"^##\s*open threads\s*$", re.IGNORECASE)
_ANY_HEADING_RE = re.compile(r"^##\s+\S")
_BULLET_RE = re.compile(r"^- \s*(.*)$")          # top-level bullet: "- " at column 0, no leading space
_BOLD_RE = re.compile(r"^\*\*(.+?)\*\*\s*(.*)$", re.DOTALL)
_BRACKET_RE = re.compile(r"\[([^\[\]]+)\]")
_GATES_RE = re.compile(r"^gates\s*:\s*(.+)$", re.IGNORECASE)


@dataclass
class Thread:
    """One `## Open threads` bullet, mapped onto plain fields a queue row can use directly."""

    title: str
    owner: str = "unknown"                 # "the user" | "agent" | "unknown"
    priority: str = "normal"               # "high" | "low" | "normal"
    gates: Optional[str] = None            # the text after "gates:", or None
    detail: str = ""                       # everything after the bold title, continuations included


def _find_section(text: str) -> Optional[str]:
    """The lines between `## Open threads` (case-insensitive) and the next `## ` heading, minus a
    leading `Tags:` legend line like some CHECKPOINTs carry. `None` when there is no
    such section at all -- not every project's CHECKPOINT has one, and that is not an error."""
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if _SECTION_RE.match(line.strip()):
            start = i + 1
            break
    if start is None:
        return None
    end = len(lines)
    for i in range(start, len(lines)):
        if _ANY_HEADING_RE.match(lines[i]):
            end = i
            break
    body = lines[start:end]
    while body and body[0].strip() == "":
        body.pop(0)
    if body and body[0].lstrip().lower().startswith("tags:"):
        body.pop(0)
    return "\n".join(body)


def _split_bullets(section: str) -> list[str]:
    """Top-level bullets only (a `- ` at column 0); an indented line -- a sub-bullet or a wrapped
    continuation paragraph -- is folded into the CURRENT bullet's text, never starts a new one.
    A line that appears BEFORE any bullet at all (stray prose under the heading, e.g. "Nothing to
    do right now.") is not a continuation of anything and is dropped, not turned into its own
    thread -- `current` stays `None` until the first bullet is seen."""
    bullets: list[str] = []
    current: Optional[list[str]] = None
    for raw_line in section.splitlines():
        m = _BULLET_RE.match(raw_line)
        if m is not None:
            if current:
                bullets.append(" ".join(current).strip())
            current = [m.group(1)]
        elif raw_line.strip() == "":
            continue     # a blank line never starts a new thread and adds no text of its own
        elif current is not None:
            current.append(raw_line.strip())
        # else: prose before the first bullet -- not a continuation of anything, dropped
    if current:
        bullets.append(" ".join(current).strip())
    return [b for b in bullets if b]


def _parse_bullet(bullet: str) -> Thread:
    m = _BOLD_RE.match(bullet)
    if m is not None:
        bold, rest = m.group(1).strip(), m.group(2).strip()
    else:
        # No bold lead at all: the first sentence is the title, the rest is detail (a bare bullet
        # with no tags is still a valid row -- owner unknown, priority normal, per the spec).
        sentence_end = bullet.find(". ")
        if sentence_end == -1:
            bold, rest = (bullet[:-1], "") if bullet.endswith(".") else (bullet, "")
        else:
            bold, rest = bullet[: sentence_end + 1].rstrip("."), bullet[sentence_end + 2:].strip()

    owner = "unknown"
    priority = "normal"
    gates: Optional[str] = None
    for tag in _BRACKET_RE.findall(bold):
        low = tag.strip().lower()
        gate_m = _GATES_RE.match(tag.strip())
        if gate_m is not None:
            gates = gate_m.group(1).strip()
        elif low == "the user":
            owner = "the user"
        elif low == "agent":
            owner = "agent"
        elif low == "low":
            priority = "low"
        elif low == "high":
            priority = "high"
        # any other bracket tag is left alone -- the parser is tolerant of a project's own vocabulary

    title = _BRACKET_RE.sub("", bold).strip()
    title = re.sub(r"\s+", " ", title).strip().rstrip(".").strip()
    return Thread(title=title, owner=owner, priority=priority, gates=gates, detail=rest)


def parse_open_threads(text: str) -> list[Thread]:
    """Every top-level bullet under `## Open threads`, in file order. `[]` when the file has no
    such section (never an error -- see `_find_section`)."""
    section = _find_section(text)
    if section is None:
        return []
    return [_parse_bullet(b) for b in _split_bullets(section)]
