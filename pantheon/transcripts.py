"""What a Claude Code Desktop-app session's own transcript can tell the supervisor that its hooks
cannot.

A Desktop-app session runs the same hooks a tmux one does -- THE PIT already gets a row, with a
cwd and tool names, from `events.jsonl` -- but it ships no `bin/statusline_capture`, so
`hud/sources.statusline_for` always returns `{}` for it and the model/mode columns and the detail
panel have nothing to show (`supervisor/pane.py` `_current_model_effort`). Claude Code still writes
the full turn-by-turn transcript for every session, tmux or not, to
`~/.claude/projects/<slug>/<session_id>.jsonl` (`dispatch/sessions.py` already reads the title out
of this file for the New-session "resume" step -- same schema, read here for different fields).
This module is the fallback source for exactly the statusline-less rows.

Schema facts below were verified against real files on this box, not guessed:
- one JSON object per line; `type` is `"user"`, `"assistant"`, `"queue-operation"`, or a few others.
- an assistant record carries `sessionId`, `cwd`, `timestamp`, `effort` ("xhigh" etc), and
  `entrypoint` ("claude-desktop" for a Desktop-app turn) at the TOP level, and `message.model`
  (e.g. `"claude-opus-5-5"`), `message.usage` (`input_tokens`, `output_tokens`,
  `cache_creation_input_tokens`, `cache_read_input_tokens`), and `message.content`, a list of parts
  typed `"thinking"` / `"tool_use"` / `"text"`, all at the same level.
- a `tool_use` part is `{"type": "tool_use", "name": "Read", "input": {"file_path": "..."}, ...}`;
  the file-bearing key varies by tool (`file_path` for Read/Edit/Write, `notebook_path` for
  NotebookEdit, `path` for a couple of others) -- a tool with none (Bash, Grep's own pattern) has
  none, and this reports just the tool name.
- `"tokens this session"` here means the LATEST assistant turn's own usage total (input + output +
  both cache buckets), the same "current context size" idea the statusline's `context_window.
  used_percentage` already reports for a tmux row (`hud/sources.py` `statusline_line`) -- not a sum
  across every turn, which would need scanning the whole file instead of just its tail.

Read tail-efficiently: a long-running Desktop session's transcript can be many MB, and every
supervisor tick asks. `read_transcript` reads only the last chunk of the file (widening once if
that chunk holds no assistant record at all, e.g. one huge tool result at the very end), and a
module-level cache keyed by the file's (mtime, size) skips the re-read entirely once nothing has
changed since the last tick.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Optional

from .dispatch.sessions import slug_for, title_of

TAIL_BYTES = 200_000       # plenty for a handful of JSON lines even with one large tool result
MAX_TAIL_BYTES = 8_000_000  # give up widening past this and report whatever the tail held
ASK_TAIL_BYTES = 2_000_000  # widen this far looking for the latest typed ask (a long turn buries it)
MAX_ACTIONS = 5

# The Remote Control dispatch conversation's fingerprint: a session opened via `claude --rc` writes a `custom-title` record of "Claude RC"
# and its FIRST user message is the literal string "claude rc" -- verified against one real
# root-cwd transcript that carries that custom title. `title_of` already falls back to the
# first user message when there is no `aiTitle` record (it does not read `custom-title`), so this
# session's `title` already comes out as "claude rc" with no change to that function -- the only
# thing needed here is the exact string this fingerprint matches, case/whitespace-insensitively.
# A root-cwd session that is NOT this (e.g. the "work the plate" orchestrator, whose first message
# names the sprints it is delegating) has its own descriptive title and never matches.
DISPATCH_TITLE = "claude rc"

# The tool-input keys that name a file, tried in this order. Most tools carry none of these (Bash,
# Grep's own `pattern`/`glob`), and then the action is just the tool name.
_FILE_INPUT_KEYS = ("file_path", "notebook_path", "path")

_USAGE_KEYS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")


@dataclass(frozen=True)
class ToolAction:
    name: str
    file: Optional[str] = None
    detail: Optional[str] = None   # what the call was FOR (a Bash call's own description, a search)

    @property
    def text(self) -> str:
        #. A bare tool name is the last resort, never the first.
        if self.file:
            return f"{self.name} {self.file}"
        if self.detail:
            return self.detail if self.name in _SELF_DESCRIBING else f"{self.name}: {self.detail}"
        return self.name


# Tools whose `description` input is already a plain sentence written for a person.
_SELF_DESCRIBING = {"Bash", "PowerShell", "Agent", "Task"}
# Per tool, the input key that says what the call was about, tried in order.
_DETAIL_INPUT_KEYS = ("description", "query", "pattern", "url", "skill", "command", "prompt")
DETAIL_WIDTH = 90


def _tool_detail(input_: dict) -> Optional[str]:
    for key in _DETAIL_INPUT_KEYS:
        value = input_.get(key)
        if isinstance(value, str) and value.strip():
            one = " ".join(value.split())
            return one if len(one) <= DETAIL_WIDTH else one[:DETAIL_WIDTH - 1] + "…"
    return None


@dataclass(frozen=True)
class TranscriptInfo:
    model: Optional[str] = None
    effort: Optional[str] = None
    tokens: int = 0
    title: Optional[str] = None
    actions: tuple = ()
    last_text: Optional[str] = None
    last_ask: Optional[str] = None   # the newest thing the person typed (what it is working on)

    @property
    def known(self) -> bool:
        """False for a session whose transcript could not be read at all."""
        return self.model is not None or self.title is not None


_EMPTY = TranscriptInfo()

# path (str) -> (mtime, size, parsed). Module-level: every supervisor tick shares it, so a session
# whose file has not grown since the last tick costs a `stat()`, not a re-read.
_cache: dict = {}


def transcript_path(session_id: str, cwd: str, claude_home: Optional[Path] = None) -> Path:
    home = Path(claude_home) if claude_home is not None else Path.home() / ".claude"
    return home / "projects" / slug_for(cwd) / f"{session_id}.jsonl"


def _typed_text(rec: dict) -> Optional[str]:
    """A user record's text when a person typed it: not a tool result, not a hook/meta record,
    not a slash command's wrapper (`<command-name>...`). A message typed while Claude was mid-turn
    is not a user record at all: it lands as a `queued_command` attachment."""
    attachment = rec.get("attachment") if rec.get("type") == "attachment" else None
    if isinstance(attachment, dict):
        if attachment.get("type") != "queued_command" or (attachment.get("origin") or {}).get("kind") != "human":
            return None
        prompt = " ".join(str(attachment.get("prompt") or "").split())
        return prompt or None
    if rec.get("type") != "user" or rec.get("isMeta"):
        return None
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, list):
        if any(isinstance(p, dict) and p.get("type") == "tool_result" for p in content):
            return None
        content = " ".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    if not isinstance(content, str):
        return None
    content = " ".join(content.split())
    if not content or content.startswith("<"):
        return None
    return content


def _tool_file(input_: dict) -> Optional[str]:
    for key in _FILE_INPUT_KEYS:
        value = input_.get(key)
        if isinstance(value, str) and value:
            return Path(value.replace("\\", "/")).name
    return None


def _tail_lines(path: Path, chunk: int, size: int) -> list:
    read_size = min(chunk, size)
    with open(path, "rb") as fh:
        fh.seek(size - read_size)
        data = fh.read(read_size)
    lines = data.decode("utf-8", errors="replace").splitlines()
    if read_size < size and lines:
        lines = lines[1:]   # the first line may have been cut mid-record; drop it
    return lines


def _scan(path: Path) -> TranscriptInfo:
    size = path.stat().st_size
    if size == 0:
        return _EMPTY
    chunk = TAIL_BYTES
    while True:
        lines = _tail_lines(path, chunk, size)
        model = effort = last_text = last_ask = None
        tokens = 0
        actions: list = []
        got_latest = False
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict) and last_ask is None:
                last_ask = _typed_text(rec)
            if not isinstance(rec, dict) or rec.get("type") != "assistant":
                continue
            message = rec.get("message") or {}
            if not got_latest:
                model = message.get("model")
                effort = rec.get("effort")
                usage = message.get("usage") or {}
                tokens = int(sum((usage.get(k) or 0) for k in _USAGE_KEYS))
                got_latest = True
            for part in reversed(message.get("content") or []):
                if not isinstance(part, dict):
                    continue
                kind = part.get("type")
                if kind == "tool_use" and len(actions) < MAX_ACTIONS:
                    input_ = part.get("input") or {}
                    actions.append(ToolAction(part.get("name") or "?", _tool_file(input_), _tool_detail(input_)))
                elif kind == "text" and last_text is None:
                    text = part.get("text")
                    if text:
                        last_text = text
            if got_latest and len(actions) >= MAX_ACTIONS and last_text is not None and last_ask is not None:
                break
        ask_done = last_ask is not None or chunk >= min(size, ASK_TAIL_BYTES)
        if (got_latest and ask_done) or chunk >= min(size, MAX_TAIL_BYTES):
            return TranscriptInfo(
                model=model, effort=effort, tokens=tokens,
                actions=tuple(actions[:MAX_ACTIONS]), last_text=last_text, last_ask=last_ask,
            )
        chunk = min(chunk * 4, size, MAX_TAIL_BYTES)


def is_dispatch_title(title: Optional[str]) -> bool:
    """True when a transcript's title is the Remote Control dispatch fingerprint (`DISPATCH_TITLE`
    above), case/whitespace-insensitive. The caller supplies the title -- this function does not
    read a file -- so it works equally from `read_transcript(...).title` (a live row) or
    `dispatch.sessions.title_of(...)` (a recent/closed one)."""
    return bool(title) and title.strip().lower() == DISPATCH_TITLE


def read_transcript(session_id: str, cwd: str, claude_home: Optional[Path] = None) -> TranscriptInfo:
    """The transcript-derived facts for one session, or `TranscriptInfo` (all fields empty) when
    there is no session id/cwd, no transcript file, or it cannot be read -- never raises."""
    if not session_id or not cwd:
        return _EMPTY
    path = transcript_path(session_id, cwd, claude_home)
    try:
        stat = path.stat()
    except OSError:
        return _EMPTY
    key = str(path)
    cached = _cache.get(key)
    if cached is not None and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
        return cached[2]
    try:
        info = _scan(path)
        info = replace(info, title=title_of(path))
    except OSError:
        return _EMPTY
    _cache[key] = (stat.st_mtime, stat.st_size, info)
    return info
