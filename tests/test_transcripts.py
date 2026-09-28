"""`pantheon/transcripts.py` reads a Claude Code session's own JSONL transcript for
what a Desktop-app row has no statusline to report -- model, tokens, title, recent tool calls with
file names, and the last reply. Every fixture line below mirrors a real record shape verified
against actual files under `C:/Home/x/.claude/projects/` on this box, never
guessed: assistant records carry `sessionId`/`cwd`/`effort` at the top level and `message.model`/
`message.usage`/`message.content` nested; a `tool_use` content part is `{"type": "tool_use",
"name": ..., "input": {...}}` with the file-bearing key varying by tool.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from pantheon import transcripts as transcripts_mod
from pantheon.dispatch.sessions import slug_for

SESSION_ID = "c0ffee00-0000-4000-8000-000000000005"
CWD = r"C:\Home\x\Documents\Projects\project_lanterns"


def _assistant(model="claude-opus-5-5", effort="xhigh", content=None, usage=None):
    return {
        "type": "assistant",
        "sessionId": SESSION_ID,
        "cwd": CWD,
        "timestamp": "2026-09-05T12:00:00.000Z",
        "effort": effort,
        "entrypoint": "claude-desktop",
        "message": {
            "model": model,
            "role": "assistant",
            "content": content or [],
            "usage": usage or {
                "input_tokens": 2,
                "output_tokens": 100,
                "cache_creation_input_tokens": 1000,
                "cache_read_input_tokens": 2000,
            },
        },
    }


def _user_text(text):
    return {"type": "user", "sessionId": SESSION_ID, "message": {"role": "user", "content": text}}


def _write(tmp_path: Path, lines: list) -> Path:
    home = tmp_path / ".claude"
    folder = home / "projects" / slug_for(CWD)
    folder.mkdir(parents=True)
    path = folder / f"{SESSION_ID}.jsonl"
    with open(path, "w", encoding="utf-8") as fh:
        for line in lines:
            fh.write(json.dumps(line) + "\n")
    return home


@pytest.fixture(autouse=True)
def _clear_cache():
    transcripts_mod._cache.clear()
    yield
    transcripts_mod._cache.clear()


def test_missing_transcript_returns_empty_info(tmp_path):
    home = tmp_path / ".claude"
    info = transcripts_mod.read_transcript(SESSION_ID, CWD, claude_home=home)
    assert info == transcripts_mod.TranscriptInfo()
    assert not info.known


def test_no_session_id_or_cwd_is_empty_not_a_crash(tmp_path):
    home = tmp_path / ".claude"
    assert transcripts_mod.read_transcript("", CWD, claude_home=home) == transcripts_mod.TranscriptInfo()
    assert transcripts_mod.read_transcript(SESSION_ID, "", claude_home=home) == transcripts_mod.TranscriptInfo()


def test_reads_model_effort_and_tokens_off_the_latest_assistant_turn(tmp_path):
    lines = [
        _user_text("hello"),
        _assistant(model="claude-sonnet-5", effort="high",
                  usage={"input_tokens": 1, "output_tokens": 10,
                         "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}),
        _user_text("do more"),
        _assistant(model="claude-opus-5-5", effort="xhigh",
                  usage={"input_tokens": 2, "output_tokens": 100,
                         "cache_creation_input_tokens": 1000, "cache_read_input_tokens": 2000}),
    ]
    home = _write(tmp_path, lines)
    info = transcripts_mod.read_transcript(SESSION_ID, CWD, claude_home=home)
    assert info.model == "claude-opus-5-5"        # the newest turn's model, not the first
    assert info.effort == "xhigh"
    assert info.tokens == 2 + 100 + 1000 + 2000
    assert info.known


def test_last_five_tool_actions_carry_file_names_newest_first(tmp_path):
    lines = [
        _assistant(content=[{"type": "tool_use", "name": "Grep", "input": {"pattern": "foo"}}]),
        _assistant(content=[
            {"type": "tool_use", "name": "Read", "input": {"file_path": "C:\\a\\b\\CHECKPOINT.md"}},
            {"type": "tool_use", "name": "Edit", "input": {"file_path": "C:\\a\\b\\ROADMAP.md"}},
        ]),
        _assistant(content=[{"type": "text", "text": "done for now"}]),
    ]
    home = _write(tmp_path, lines)
    info = transcripts_mod.read_transcript(SESSION_ID, CWD, claude_home=home)
    names = [a.name for a in info.actions]
    files = [a.file for a in info.actions]
    assert names == ["Edit", "Read", "Grep"]        # newest first, across turns and within one
    assert files == ["ROADMAP.md", "CHECKPOINT.md", None]
    assert info.actions[0].text == "Edit ROADMAP.md"
    assert info.last_text == "done for now"


def test_an_action_says_what_the_call_was_for_not_just_its_tool_name(tmp_path):
    lines = [
        _assistant(content=[{"type": "tool_use", "name": "Grep", "input": {"pattern": "effort"}}]),
        _assistant(content=[{"type": "tool_use", "name": "WebSearch", "input": {"query": "vortex patch renumber"}}]),
        _assistant(content=[{"type": "tool_use", "name": "Bash", "input": {
            "command": "ls -la /x", "description": "Check the crash dump times"}}]),
        _assistant(content=[{"type": "tool_use", "name": "Bash", "input": {"command": "tasklist   |  grep hd2"}}]),
    ]
    home = _write(tmp_path, lines)
    info = transcripts_mod.read_transcript(SESSION_ID, CWD, claude_home=home)
    assert [a.text for a in info.actions] == [
        "tasklist | grep hd2",                       # no description: the command itself
        "Check the crash dump times",                # Bash's own description, as written
        "WebSearch: vortex patch renumber",
        "Grep: effort",
    ]


def test_latest_ask_is_the_newest_typed_message_not_a_tool_result_or_command(tmp_path):
    def user(content, **extra):
        return {"type": "user", "sessionId": SESSION_ID, "message": {"role": "user", "content": content}, **extra}
    lines = [
        user("fix the crash when adding a stratagem"),
        _assistant(content=[{"type": "tool_use", "name": "Read", "input": {"file_path": "a.lua"}}]),
        user([{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]),
        user("<command-name>/model</command-name>"),
        user("hook text", isMeta=True),
    ]
    home = _write(tmp_path, lines)
    info = transcripts_mod.read_transcript(SESSION_ID, CWD, claude_home=home)
    assert info.last_ask == "fix the crash when adding a stratagem"
    # A message typed mid-turn is a `queued_command` attachment, not a user record.
    lines.append({"type": "attachment", "sessionId": SESSION_ID, "attachment": {
        "type": "queued_command", "prompt": "the game is down", "origin": {"kind": "human"}}})
    home = _write(tmp_path / "again", lines)
    assert transcripts_mod.read_transcript(SESSION_ID, CWD, claude_home=home).last_ask == "the game is down"


def test_caps_at_five_actions(tmp_path):
    lines = [
        _assistant(content=[{"type": "tool_use", "name": f"Tool{i}", "input": {}}])
        for i in range(8)
    ]
    home = _write(tmp_path, lines)
    info = transcripts_mod.read_transcript(SESSION_ID, CWD, claude_home=home)
    assert len(info.actions) == 5
    assert info.actions[0].name == "Tool7"           # newest


def test_title_is_the_last_ai_title_same_as_dispatch_sessions(tmp_path):
    lines = [
        _user_text("what is going on here"),
        {"aiTitle": "First title"},
        _assistant(),
        {"aiTitle": "Second, truer title"},
    ]
    home = _write(tmp_path, lines)
    info = transcripts_mod.read_transcript(SESSION_ID, CWD, claude_home=home)
    assert info.title == "Second, truer title"


def test_result_is_cached_until_the_file_changes(tmp_path, monkeypatch):
    home = _write(tmp_path, [_assistant(model="claude-sonnet-5")])
    first = transcripts_mod.read_transcript(SESSION_ID, CWD, claude_home=home)
    assert first.model == "claude-sonnet-5"

    calls = []
    real_scan = transcripts_mod._scan

    def spy(path):
        calls.append(path)
        return real_scan(path)

    monkeypatch.setattr(transcripts_mod, "_scan", spy)
    again = transcripts_mod.read_transcript(SESSION_ID, CWD, claude_home=home)
    assert again.model == "claude-sonnet-5"
    assert calls == []                                # unchanged file -> no re-scan

    path = transcripts_mod.transcript_path(SESSION_ID, CWD, claude_home=home)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(_assistant(model="claude-opus-5-5")) + "\n")
    changed = transcripts_mod.read_transcript(SESSION_ID, CWD, claude_home=home)
    assert changed.model == "claude-opus-5-5"
    assert calls == [path]                            # grew -> exactly one re-scan


def test_a_tail_chunk_with_no_assistant_record_widens_until_it_finds_one(tmp_path):
    # A giant tool_result (a `type: "user"` record) sits at the very end, past `TAIL_BYTES`, with
    # the real assistant turn further back -- the tail read must widen rather than report nothing.
    padding = "x" * (transcripts_mod.TAIL_BYTES * 2)
    lines = [
        _assistant(model="claude-opus-5-5", content=[{"type": "text", "text": "ok"}]),
        {"type": "user", "sessionId": SESSION_ID,
         "message": {"role": "user", "content": [{"type": "tool_result", "content": padding}]}},
    ]
    home = _write(tmp_path, lines)
    info = transcripts_mod.read_transcript(SESSION_ID, CWD, claude_home=home)
    assert info.model == "claude-opus-5-5"


def test_malformed_lines_are_skipped_not_raised(tmp_path):
    home = tmp_path / ".claude"
    folder = home / "projects" / slug_for(CWD)
    folder.mkdir(parents=True)
    path = folder / f"{SESSION_ID}.jsonl"
    path.write_text("not json at all\n" + json.dumps(_assistant()) + "\n", encoding="utf-8")
    info = transcripts_mod.read_transcript(SESSION_ID, CWD, claude_home=home)
    assert info.model == "claude-opus-5-5"


# ---------------------------------------------------------- dispatch fingerprint


@pytest.mark.parametrize("title", ["claude rc", "Claude RC", "  claude rc  ", "CLAUDE RC\n"])
def test_is_dispatch_title_matches_case_and_whitespace_insensitively(title):
    assert transcripts_mod.is_dispatch_title(title)


@pytest.mark.parametrize("title", [
    None, "", "work the plate: pick 5 rows",   # the orchestrator's own root-cwd title
    "claude rce", "not claude rc",
])
def test_is_dispatch_title_rejects_anything_else(title):
    assert not transcripts_mod.is_dispatch_title(title)
