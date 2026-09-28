"""Codex rollout parser for the condensed session view.
Turns `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` into a `Conversation` (`models.py`) with
`provider="codex"`, using the same `load`/`update` shape as `transcript.py` (the Claude parser)
-- but no import between the two beyond `models.py`.

Record shapes below follow the spec: every line is
`{"timestamp": ..., "type": ..., "payload": {...}}`.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from . import _common
from .models import ASSISTANT, Conversation, Item, SYSTEM, THINKING, TOOL, ToolCall, USER

_RANK_NONE = 0
_RANK_USER = 1

_NOT_SHARED = "(reasoning not shared by Codex)"


def _is_system_text(text: str) -> bool:
    text = (text or "").strip()
    return text.startswith("<app-context") or "AGENTS.md" in text


def _text_parts(content) -> list:
    if not isinstance(content, list):
        return []
    out = []
    for part in content:
        if isinstance(part, dict) and isinstance(part.get("text"), str):
            out.append(part["text"])
    return out


def _codex_chip(name: str, input_) -> str:
    """The chip line for a Codex `custom_tool_call`. `input` may arrive as a dict or as a
    JSON-encoded string (Codex's own tool-call convention) -- both are accepted."""
    parsed = input_
    if isinstance(input_, str):
        try:
            parsed = json.loads(input_)
        except (json.JSONDecodeError, TypeError):
            parsed = {}
    if not isinstance(parsed, dict):
        parsed = {}
    if name == "exec":
        cmd = parsed.get("cmd") or parsed.get("command") or ""
        if isinstance(cmd, list):
            cmd = " ".join(str(part) for part in cmd)
        return _common.cap_chip(f"exec {cmd[:60]}".rstrip())
    first_val = _common.first_string_value(parsed)
    return _common.cap_chip(f"{name} {first_val}".rstrip())


def _pretty(input_) -> str:
    if isinstance(input_, str):
        return _common.cap_input(input_)
    return _common.cap_input(_common.pretty_input(input_ or {}))


class _State:
    def __init__(self, conv: Conversation):
        self.items: list = list(conv.items)
        self.pending_idx: dict = {}
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
        self.parse_errors = conv.parse_errors

    def maybe_first_user_title(self, text: str) -> None:
        if self.title_rank == _RANK_NONE:
            self.title = " ".join(text.split())
            self.title_rank = _RANK_USER

    def append(self, item: Item) -> None:
        self.items.append(item)

    def resolve_tool(self, call_id: str, result_text: str, is_error: bool) -> None:
        idx = self.pending_idx.pop(call_id, None)
        if idx is None:
            return
        old = self.items[idx]
        new_tool = ToolCall(
            id=old.tool.id, name=old.tool.name, summary=old.tool.summary,
            input_text=old.tool.input_text, result_text=_common.cap_result(result_text),
            is_error=is_error, file=old.tool.file,
        )
        self.items[idx] = Item(kind=TOOL, text=old.text, ts=old.ts, uuid=old.uuid, tool=new_tool)

    def freeze(self, session_id: str, path: str, offset: int) -> Conversation:
        return Conversation(
            session_id=session_id, path=path, provider="codex",
            items=tuple(self.items), title=self.title, model=self.model, effort=self.effort,
            context_tokens=self.context_tokens, cwd=self.cwd, git_branch=self.git_branch,
            entrypoint=self.entrypoint, offset=offset, pending=tuple(self.pending_idx.keys()),
            parse_errors=self.parse_errors, title_rank=self.title_rank,
        )


def _handle_response_item(payload: dict, rec: dict, state: _State) -> None:
    kind = payload.get("type")
    ts = rec.get("timestamp")

    if kind == "message":
        role = payload.get("role")
        texts = _text_parts(payload.get("content"))
        if role == "developer":
            joined = "\n".join(texts)
            if joined:
                state.append(Item(kind=SYSTEM, text="developer note", detail=_common.cap_text(joined), ts=ts))
            return
        if role == "user":
            for text in texts:
                if _is_system_text(text):
                    state.append(Item(kind=SYSTEM, text="note", detail=_common.cap_text(text), ts=ts))
                else:
                    state.append(Item(kind=USER, text=_common.cap_text(text), ts=ts))
                    state.maybe_first_user_title(text)
            return
        if role == "assistant":
            joined = "\n".join(texts)
            if joined:
                state.append(Item(kind=ASSISTANT, text=_common.cap_text(joined), ts=ts))
            return
        return

    if kind == "custom_tool_call":
        call_id = payload.get("call_id") or ""
        name = payload.get("name") or ""
        input_ = payload.get("input")
        tool = ToolCall(
            id=call_id, name=name, summary=_codex_chip(name, input_),
            input_text=_pretty(input_),
            file=_common.tool_file(input_) if isinstance(input_, dict) else None,
        )
        state.append(Item(kind=TOOL, ts=ts, tool=tool))
        state.pending_idx[call_id] = len(state.items) - 1
        return

    if kind == "custom_tool_call_output":
        call_id = payload.get("call_id") or ""
        output = payload.get("output")
        if isinstance(output, list):
            result_text = "\n".join(_text_parts(output))
        elif isinstance(output, str):
            result_text = output
        else:
            result_text = ""
        state.resolve_tool(call_id, result_text, bool(payload.get("is_error")))
        return

    if kind == "reasoning":
        summary = payload.get("summary")
        texts = _text_parts(summary) if isinstance(summary, list) else []
        text = "\n".join(texts) if texts else _NOT_SHARED
        state.append(Item(kind=THINKING, text=_common.cap_text(text), ts=ts))
        return

    # unrecognised response_item payload type -- skipped, not a parse error.


def _handle_record(rec: dict, state: _State) -> None:
    rtype = rec.get("type")
    payload = rec.get("payload") or {}

    if rtype == "session_meta":
        cwd = payload.get("cwd")
        if cwd:
            state.cwd = cwd
        originator = payload.get("originator")
        if originator:
            state.entrypoint = originator
        return

    if rtype == "turn_context":
        cwd = payload.get("cwd")
        if cwd:
            state.cwd = cwd
        return

    if rtype == "response_item":
        _handle_response_item(payload, rec, state)
        return

    if rtype == "token_usage_record":
        usage = payload.get("usage") or {}
        total = usage.get("total_tokens")
        if total is not None:
            state.context_tokens = int(total)
        return

    if rtype == "event_msg":
        if payload.get("type") == "task_complete":
            state.append(Item(kind=SYSTEM, text="turn done", ts=rec.get("timestamp")))
        return

    if rtype == "world_state":
        model = (
            (payload.get("state") or {})
            .get("collaboration_mode", {})
            .get("model")
        )
        if model:
            state.model = model
        return

    # any other recognised-but-unhandled top-level type -- skipped, not a parse error.


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
    path = Path(path)
    session_id = path.stem
    conv = Conversation(session_id=session_id, path=str(path), provider="codex")
    state = _State(conv)
    try:
        size = path.stat().st_size
    except OSError:
        return conv
    lines, offset = _common.read_new_lines(path, 0) if size else ([], 0)
    _process(lines, state)
    return state.freeze(session_id, str(path), offset)


def update(conv: Conversation) -> Conversation:
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


# ---------------------------------------------------------------------------------- find_rollout

def _peek_session_meta(path: Path, max_lines: int = 5):
    """(`timestamp`, `cwd`) of a rollout's `session_meta` record, read from just the first few
    lines -- cheap enough to call once per candidate file in `find_rollout`."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for _ in range(max_lines):
                line = fh.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict) and rec.get("type") == "session_meta":
                    payload = rec.get("payload") or {}
                    return rec.get("timestamp"), payload.get("cwd")
    except OSError:
        pass
    return None, None


def find_rollout(cwd: str, not_before_iso: str, codex_home: Optional[Path] = None) -> Optional[Path]:
    """The newest rollout file under `codex_home/sessions` (default `~/.codex/sessions`) whose
    `session_meta.cwd` equals `cwd` and whose timestamp is not older than `not_before_iso`
    (ISO-8601 strings compare correctly as plain strings when both use the same zero-padded
    UTC format, which Codex's rollouts do)."""
    home = Path(codex_home) if codex_home is not None else Path.home() / ".codex"
    sessions_dir = home / "sessions"
    if not sessions_dir.is_dir():
        return None
    best_ts: Optional[str] = None
    best_path: Optional[Path] = None
    for path in sessions_dir.glob("**/rollout-*.jsonl"):
        ts, meta_cwd = _peek_session_meta(path)
        if meta_cwd != cwd or ts is None or ts < not_before_iso:
            continue
        if best_ts is None or ts > best_ts:
            best_ts, best_path = ts, path
    return best_path
