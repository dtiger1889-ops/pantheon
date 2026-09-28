"""Small helpers shared by `transcript.py` (Claude) and `codex_transcript.py` (Codex) -- the two
parsers stay independent of each other, so
this is the one file both may import instead of copy-pasting.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from .models import CHIP_CHARS, INPUT_CHARS, RESULT_CHARS, TEXT_CHARS

# The tool-input keys that name a file, tried in this order (same idea as `transcripts.py`'s
# `_FILE_INPUT_KEYS`, kept local so this module has no import on that one).
_FILE_INPUT_KEYS = ("file_path", "notebook_path", "path")


def read_new_lines(path: Path, start_offset: int) -> tuple[list[str], int]:
    """The complete lines appended to `path` since byte `start_offset`, and the new offset.

    Works on raw bytes so the offset is always exact. A trailing line with no `\\n` yet (the
    writer is mid-record) is left unconsumed -- the returned offset stops right before it, so
    the next call picks it up whole."""
    with open(path, "rb") as fh:
        fh.seek(start_offset)
        data = fh.read()
    if not data:
        return [], start_offset
    chunks = data.split(b"\n")
    if data.endswith(b"\n"):
        chunks.pop()  # trailing empty string from the split
        consumed = start_offset + len(data)
    else:
        incomplete = chunks.pop()
        consumed = start_offset + len(data) - len(incomplete)
    lines = [chunk.decode("utf-8", errors="replace") for chunk in chunks]
    return lines, consumed


def truncate(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def cap_chip(text: str) -> str:
    return truncate(" ".join(str(text).split()), CHIP_CHARS)


def cap_input(text: str) -> str:
    return truncate(text, INPUT_CHARS)


def cap_result(text: str) -> str:
    return truncate(text, RESULT_CHARS)


def cap_text(text: str) -> str:
    return truncate(text, TEXT_CHARS)


def tool_file(input_: dict) -> Optional[str]:
    """Basename of the file a tool's input names, when it names one (Read/Edit/Write/NotebookEdit
    -- Bash, Grep's own pattern/glob, and most MCP tools carry none)."""
    if not isinstance(input_, dict):
        return None
    for key in _FILE_INPUT_KEYS:
        value = input_.get(key)
        if isinstance(value, str) and value:
            return Path(value.replace("\\", "/")).name
    return None


def first_string_value(input_: dict) -> str:
    """The first non-empty string value in a tool's input dict, in insertion order -- the
    generic fallback chip content for a tool `chip_summary` has no dedicated rule for."""
    if not isinstance(input_, dict):
        return ""
    for value in input_.values():
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def pretty_input(input_: dict) -> str:
    if not input_:
        return ""
    try:
        return json.dumps(input_, indent=2, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(input_)


def result_text_of(content) -> str:
    """A tool_result's `content`: a plain string, or a list of `{"type": "text", "text": ...}`
    parts joined with newlines. Anything else -> ""."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
            elif isinstance(part, str):
                parts.append(part)
        return "\n".join(parts)
    return ""
