"""When a session last committed and last pushed -- a good stop point at a glance.

Read from the transcript itself, never from git: every Bash/PowerShell tool call is in the file.
A commit or push counts only once its result came back without an error (a `git commit` with
nothing to commit exits 1 and is recorded as a tool error, so it is not a commit). The mark's
time is the tool call's own timestamp.

Cheap on purpose (the sidebar rebuilds every 5 s): `marks_for(path)` keeps a per-file offset and
reads only bytes appended since the last call, and a line is parsed only when it mentions `git`
with `commit`/`push`, or names a tool call still waiting for its result. Record shapes, verified
on this box 2026-09-26:
- Claude: an assistant `tool_use` part (`name` Bash/PowerShell, `input.command`), then a user
  `tool_result` part with the same `tool_use_id` and `is_error`.
- Codex: an `event_msg` whose payload is `item_completed` with `item.type == "CommandExecution"`,
  `item.command` (argv list) and `item.exit_code` ("0" on success) -- one record, no join.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import titles as titles_mod

COMMIT = "commit"
PUSH = "push"

_SHELL_TOOLS = {"Bash", "PowerShell"}

# `git` + any global options (`-C dir`, `-c k=v`, `--no-pager`) + the subcommand. Prose that
# merely mentions a commit ("git ... and commit it") does not match: the word right after git's
# own options has to be the subcommand.
_GIT_OP = re.compile(
    r"(?<![\w-])git(?:\.exe)?"
    r"(?:\s+(?:-C|-c)\s+(?:'[^']*'|\"[^\"]*\"|\S+)|\s+--[\w-]+(?:=\S+)?)*"
    r"\s+(commit|push)\b"
)


def git_ops(command: Optional[str]) -> set:
    """{"commit", "push"} subset the shell command runs."""
    if not command:
        return set()
    return {m.group(1) for m in _GIT_OP.finditer(str(command))}


@dataclass(frozen=True)
class GitMarks:
    last_commit_at: Optional[str] = None   # ISO timestamp of the latest successful commit
    last_push_at: Optional[str] = None     # ISO timestamp of the latest successful push


class _FileState:
    __slots__ = ("offset", "pending", "commit", "push")

    def __init__(self):
        self.offset = 0
        self.pending: dict = {}     # tool_use_id -> (ops, ts) awaiting a result
        self.commit: Optional[str] = None
        self.push: Optional[str] = None


_states: dict = {}   # path -> _FileState


def _later(a: Optional[str], b: Optional[str]) -> Optional[str]:
    if not a:
        return b
    if not b:
        return a
    return max(a, b)


def _apply(state: _FileState, ops: set, ts: Optional[str]) -> None:
    if COMMIT in ops:
        state.commit = _later(state.commit, ts)
    if PUSH in ops:
        state.push = _later(state.push, ts)


def _handle(rec: dict, state: _FileState) -> None:
    rtype = rec.get("type")
    if rtype == "assistant":
        for part in (rec.get("message") or {}).get("content") or []:
            if not isinstance(part, dict) or part.get("type") != "tool_use":
                continue
            if part.get("name") not in _SHELL_TOOLS:
                continue
            ops = git_ops((part.get("input") or {}).get("command"))
            if ops and part.get("id"):
                state.pending[part["id"]] = (ops, rec.get("timestamp"))
        return
    if rtype == "user":
        content = (rec.get("message") or {}).get("content")
        if not isinstance(content, list):
            return
        for part in content:
            if not isinstance(part, dict) or part.get("type") != "tool_result":
                continue
            waiting = state.pending.pop(part.get("tool_use_id"), None)
            if waiting is not None and not part.get("is_error"):
                _apply(state, waiting[0], waiting[1])
        return
    if rtype == "event_msg":   # Codex
        payload = rec.get("payload") or {}
        item = payload.get("item") or {}
        if payload.get("type") != "item_completed" or item.get("type") != "CommandExecution":
            return
        command = item.get("command")
        if isinstance(command, list):
            command = " ".join(str(c) for c in command)
        ops = git_ops(command)
        if ops and str(item.get("exit_code")) == "0":
            _apply(state, ops, rec.get("timestamp"))


def _interesting(raw: bytes, state: _FileState) -> bool:
    if b"git" in raw and (b"commit" in raw or b"push" in raw):
        return True
    if state.pending and b"tool_result" in raw:
        return any(tid.encode() in raw for tid in state.pending)
    return False


def marks_for(path) -> GitMarks:
    """The latest successful commit / push in one transcript (Claude or Codex). Never raises."""
    key = str(path)
    state = _states.get(key)
    try:
        size = Path(path).stat().st_size
    except OSError:
        return GitMarks()
    if state is None or size < state.offset:
        state = _FileState()
        _states[key] = state
    if size > state.offset:
        try:
            with open(path, "rb") as fh:
                fh.seek(state.offset)
                data = fh.read(size - state.offset)
        except OSError:
            return GitMarks(state.commit, state.push)
        end = data.rfind(b"\n")
        if end >= 0:   # only whole lines; a half-written last line waits for the next call
            for raw in data[:end].split(b"\n"):
                if not _interesting(raw, state):
                    continue
                try:
                    rec = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if isinstance(rec, dict):
                    _handle(rec, state)
            state.offset += end + 1
    return GitMarks(state.commit, state.push)


def scan_records(records) -> GitMarks:
    """Same rules over already-parsed records (the transcript parser's own pass, and tests)."""
    state = _FileState()
    for rec in records:
        if isinstance(rec, dict):
            _handle(rec, state)
    return GitMarks(state.commit, state.push)


def _parse_ts(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None


def stop_point_text(entry, now: Optional[datetime] = None) -> str:
    """`pushed 4m` / `committed 12m` / `never pushed` for anything carrying `last_commit_at` /
    `last_push_at` (a `SessionEntry`, a `Conversation`, a `GitMarks`). A commit newer than the
    last push reads `committed ...` -- that work is saved locally but not yet pushed."""
    now = now or datetime.now(timezone.utc)
    commit = _parse_ts(getattr(entry, "last_commit_at", None))
    push = _parse_ts(getattr(entry, "last_push_at", None))

    def ago(when: datetime) -> str:
        return titles_mod.age_text((now - when).total_seconds())

    if push is not None and (commit is None or push >= commit):
        return f"pushed {ago(push)}"
    if commit is not None:
        return f"committed {ago(commit)}"
    return "never pushed"
