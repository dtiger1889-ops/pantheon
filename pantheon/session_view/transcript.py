"""Claude Code transcript parser for the condensed session view. Turns `~/.claude/projects/<slug>/<session_id>.jsonl` into a `Conversation`
(`models.py`), incrementally -- the file is append-only while a session runs, so `update`
only reads the bytes past `conv.offset`.

Record shapes below were verified on this box -- not guessed.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from . import _common
from . import gitmarks as gitmarks_mod
from . import titles as titles_mod
from .models import (
    ASSISTANT,
    Conversation,
    Item,
    SUBAGENT,
    SYSTEM,
    THINKING,
    TOOL,
    ToolCall,
    USER,
)

_USAGE_KEYS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")

# title_rank values (also documented on `Conversation.title_rank`)
_RANK_NONE = 0
_RANK_USER = 1
_RANK_AI = 2
_RANK_CUSTOM = 3


# ---------------------------------------------------------------------------------- chip_summary

def chip_summary(name: str, input_: Optional[dict]) -> str:
    """The one-line chip for a tool call, capped to `CHIP_CHARS` (models.py)."""
    input_ = input_ or {}
    name = name or ""

    if name in ("Read", "Edit", "Write"):
        base = _common.tool_file(input_) or ""
        return _common.cap_chip(f"{name} {base}".strip())

    if name == "Bash":
        command = input_.get("command") or ""
        first_line = command.splitlines()[0] if command else ""
        return _common.cap_chip(f"Bash {first_line[:60]}".rstrip())

    if name == "Grep":
        return _common.cap_chip(f"Grep {input_.get('pattern', '')}".rstrip())

    if name == "Glob":
        return _common.cap_chip(f"Glob {input_.get('pattern', '')}".rstrip())

    if name == "Agent":
        return _common.cap_chip(f"Agent {input_.get('description', '')}".rstrip())

    if name == "Skill":
        return _common.cap_chip(f"Skill {input_.get('skill', '')}".rstrip())

    if name == "WebFetch":
        url = input_.get("url") or ""
        host = urlparse(url).netloc or url
        return _common.cap_chip(f"WebFetch {host}".rstrip())

    if name.startswith("mcp__"):
        parts = name.split("__")
        if len(parts) >= 3:
            server = parts[1]
            tool = "__".join(parts[2:])
            first_val = _common.first_string_value(input_)
            return _common.cap_chip(f"{server}: {tool} {first_val}".rstrip())

    first_val = _common.first_string_value(input_)
    return _common.cap_chip(f"{name} {first_val}".rstrip())


# ---------------------------------------------------------------------------------- title helper

def _clean_title(text: str) -> str:
    return " ".join(str(text).split())


def _first_user_text(content) -> Optional[str]:
    """A `user.message.content` value reduced to plain text, or None for a tool_result-only
    message or a system-reminder/command-caveat wrapper (starts with `<`)."""
    if isinstance(content, list):
        text_parts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]
        if not text_parts:
            return None
        content = " ".join(text_parts)
    if not isinstance(content, str):
        return None
    content = content.strip()
    if not content or content.startswith("<"):
        return None
    return content


# ---------------------------------------------------------------------------------- parse state

class _State:
    """Mutable working copy of the bits of `Conversation` a parse pass updates. Built from the
    prior `Conversation` (or empty, for `load()`), mutated in place while walking new lines, then
    frozen back into a `Conversation` at the end -- this is what makes `update()` after N small
    batches produce the same result as one `load()` over the whole file."""

    def __init__(self, conv: Conversation):
        self.items: list = list(conv.items)
        self.pending_idx: dict = {}   # tool_use_id -> index into self.items, only unresolved ones
        for idx, item in enumerate(self.items):
            if item.kind == TOOL and item.tool is not None and not item.tool.done:
                self.pending_idx[item.tool.id] = idx
        self.title = conv.title
        self.title_rank = conv.title_rank
        self.model = conv.model
        self.effort = conv.effort
        self.context_tokens = conv.context_tokens
        self.cwd = conv.cwd
        self.git_branch = conv.git_branch
        self.entrypoint = conv.entrypoint
        self.sidechain_pending: list = list(conv.sidechain_pending)
        self.parse_errors = conv.parse_errors
        self.last_commit_at = conv.last_commit_at
        self.last_push_at = conv.last_push_at

    # -- title -------------------------------------------------------------------------------
    def maybe_title(self, rec: dict) -> None:
        custom = rec.get("customTitle")
        if custom and not titles_mod.is_uuid(custom):
            self.title = _clean_title(titles_mod.readable(custom))
            self.title_rank = _RANK_CUSTOM
            return
        ai = rec.get("aiTitle")
        if ai and self.title_rank < _RANK_CUSTOM:
            self.title = _clean_title(titles_mod.readable(ai))
            self.title_rank = _RANK_AI

    def maybe_first_user_title(self, rec: dict) -> None:
        """The first user turn that is really the user's words (`titles.user_record_candidate`:
        skill bodies, command wrappers, hook output, injections skipped)."""
        if self.title_rank == _RANK_NONE:
            cand = titles_mod.user_record_candidate(rec)
            if cand is not None and cand[1] >= titles_mod.STRONG:
                self.title = cand[0]
                self.title_rank = _RANK_USER

    # -- sidechain folding ---------------------------------------------------------------------
    def flush_sidechain(self) -> None:
        if not self.sidechain_pending:
            return
        detail = "\n".join(self.sidechain_pending)
        summary = self.sidechain_pending[0] if self.sidechain_pending else ""
        self.items.append(Item(
            kind=SUBAGENT,
            text=_common.cap_chip(f"subagent: {summary}"),
            detail=_common.cap_text(detail),
            sidechain=True,
        ))
        self.sidechain_pending = []

    def add_sidechain_text(self, text: str) -> None:
        if text:
            self.sidechain_pending.append(text)

    # -- items -----------------------------------------------------------------------------
    def append(self, item: Item) -> None:
        self.items.append(item)

    def resolve_tool(self, tool_use_id: str, result_text: str, is_error: bool) -> None:
        idx = self.pending_idx.pop(tool_use_id, None)
        if idx is None:
            return
        old = self.items[idx]
        new_tool = ToolCall(
            id=old.tool.id, name=old.tool.name, summary=old.tool.summary,
            input_text=old.tool.input_text, result_text=_common.cap_result(result_text),
            is_error=is_error, file=old.tool.file,
        )
        if not is_error and old.tool.name in ("Bash", "PowerShell"):
            ops = gitmarks_mod.git_ops(old.tool.input_text)
            if gitmarks_mod.COMMIT in ops:
                self.last_commit_at = max(filter(None, (self.last_commit_at, old.ts)), default=None)
            if gitmarks_mod.PUSH in ops:
                self.last_push_at = max(filter(None, (self.last_push_at, old.ts)), default=None)
        self.items[idx] = Item(kind=TOOL, text=old.text, ts=old.ts, uuid=old.uuid, tool=new_tool)

    def freeze(self, session_id: str, path: str, offset: int) -> Conversation:
        return Conversation(
            session_id=session_id,
            path=path,
            provider="claude",
            items=tuple(self.items),
            title=self.title,
            model=self.model,
            effort=self.effort,
            context_tokens=self.context_tokens,
            cwd=self.cwd,
            git_branch=self.git_branch,
            entrypoint=self.entrypoint,
            offset=offset,
            pending=tuple(self.pending_idx.keys()),
            parse_errors=self.parse_errors,
            title_rank=self.title_rank,
            sidechain_pending=tuple(self.sidechain_pending),
            last_commit_at=self.last_commit_at,
            last_push_at=self.last_push_at,
        )


# ---------------------------------------------------------------------------------- record handling

def _handle_record(rec: dict, state: _State) -> None:
    rtype = rec.get("type")
    is_sidechain = bool(rec.get("isSidechain"))

    for key in ("cwd", "gitBranch", "entrypoint"):
        value = rec.get(key)
        if value:
            setattr(state, {"cwd": "cwd", "gitBranch": "git_branch", "entrypoint": "entrypoint"}[key], value)

    state.maybe_title(rec)

    if rtype == "user":
        message = rec.get("message") or {}
        content = message.get("content")
        if is_sidechain:
            text = _first_user_text(content)
            if text:
                state.add_sidechain_text(f"you › {text}")
            _handle_tool_results(content, state)
            return
        state.flush_sidechain()
        state.maybe_first_user_title(rec)   # its own skip rules; a `/command args` wrapper counts
        text = _first_user_text(content)
        if text is not None:
            state.append(Item(kind=USER, text=_common.cap_text(text), ts=rec.get("timestamp"), uuid=rec.get("uuid")))
        elif isinstance(content, str) and content.strip().startswith("<"):
            state.append(Item(
                kind=SYSTEM, text="note", detail=_common.cap_text(content.strip()),
                ts=rec.get("timestamp"), uuid=rec.get("uuid"),
            ))
        _handle_tool_results(content, state)
        return

    if rtype == "assistant":
        message = rec.get("message") or {}
        parts = message.get("content") or []
        if is_sidechain:
            # a subagent's own turn -- folded into the SUBAGENT item, never the top-level
            # model/effort/context_tokens (those describe the main conversation's latest turn).
            for part in parts:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text" and part.get("text"):
                    state.add_sidechain_text(part["text"])
            return
        state.model = message.get("model") or state.model
        state.effort = rec.get("effort") or state.effort
        usage = message.get("usage") or {}
        if usage:
            state.context_tokens = int(sum((usage.get(k) or 0) for k in _USAGE_KEYS))
        state.flush_sidechain()
        for part in parts:
            if not isinstance(part, dict):
                continue
            kind = part.get("type")
            if kind == "thinking":
                text = part.get("thinking") or part.get("text") or ""
                state.append(Item(kind=THINKING, text=_common.cap_text(text), ts=rec.get("timestamp"), uuid=rec.get("uuid")))
            elif kind == "text":
                text = part.get("text") or ""
                state.append(Item(kind=ASSISTANT, text=_common.cap_text(text), ts=rec.get("timestamp"), uuid=rec.get("uuid")))
            elif kind == "tool_use":
                tool_input = part.get("input") or {}
                tool_name = part.get("name") or ""
                tool = ToolCall(
                    id=part.get("id") or "",
                    name=tool_name,
                    summary=chip_summary(tool_name, tool_input),
                    input_text=_common.cap_input(_common.pretty_input(tool_input)),
                    file=_common.tool_file(tool_input),
                )
                item = Item(kind=TOOL, ts=rec.get("timestamp"), uuid=rec.get("uuid"), tool=tool)
                state.append(item)
                state.pending_idx[tool.id] = len(state.items) - 1
        return

    if rtype == "attachment":
        attachment = rec.get("attachment") or {}
        hook_name = attachment.get("hookName")
        if hook_name:
            state.flush_sidechain()
            state.append(Item(
                kind=SYSTEM, text=_common.cap_chip(f"hook: {hook_name}"),
                detail=_common.cap_text(str(rec.get("content") or "")),
                ts=rec.get("timestamp"), uuid=rec.get("uuid"),
            ))
        return

    if rtype == "system":
        subtype = rec.get("subtype")
        if subtype:
            state.flush_sidechain()
            text = "remote control active" if subtype == "bridge_status" else f"system: {subtype}"
            state.append(Item(kind=SYSTEM, text=text, ts=rec.get("timestamp"), uuid=rec.get("uuid")))
        return

    # queue-operation / last-prompt / atis-latch / custom-title / ai-title / bridge-session /
    # mode / permission-mode / file-history-snapshot / file-history-delta: recognised types that
    # carry no drawable item on their own (title/cwd/branch handling above already read what
    # they offer) -- skipped, not a parse error.


def _handle_tool_results(content, state: _State) -> None:
    if not isinstance(content, list):
        return
    for part in content:
        if not isinstance(part, dict) or part.get("type") != "tool_result":
            continue
        tool_use_id = part.get("tool_use_id")
        if not tool_use_id:
            continue
        result_text = _common.result_text_of(part.get("content"))
        state.resolve_tool(tool_use_id, result_text, bool(part.get("is_error")))


# ---------------------------------------------------------------------------------- public API

def _process(lines: list, state: _State) -> None:
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            state.parse_errors += 1
            continue
        if not isinstance(rec, dict):
            state.parse_errors += 1
            continue
        _handle_record(rec, state)


def load(path) -> Conversation:
    """Parse a Claude Code transcript from the start."""
    path = Path(path)
    session_id = path.stem
    conv = Conversation(session_id=session_id, path=str(path))
    state = _State(conv)
    try:
        size = path.stat().st_size
    except OSError:
        return conv
    lines, offset = _common.read_new_lines(path, 0) if size else ([], 0)
    _process(lines, state)
    return state.freeze(session_id, str(path), offset)


def update(conv: Conversation) -> Conversation:
    """Read only the bytes appended since `conv.offset`. A shrunk file (rotated/truncated) is
    re-`load`ed from scratch."""
    path = Path(conv.path)
    try:
        size = path.stat().st_size
    except OSError:
        return conv
    if size < conv.offset:
        return load(path)
    if size == conv.offset:
        return conv
    lines, offset = _common.read_new_lines(path, conv.offset)
    if not lines:
        return conv
    state = _State(conv)
    _process(lines, state)
    return state.freeze(conv.session_id, conv.path, offset)
