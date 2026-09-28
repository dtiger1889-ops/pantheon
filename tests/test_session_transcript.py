"""Tests for `pantheon/session_view/transcript.py`.

`tests/fixtures/session_view/claude_session.jsonl` is a hand-built synthetic transcript covering
every record shape the spec names -- fake session id, fake `C:/x/proj` paths, never a copy of a
real transcript.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from pantheon.session_view import transcript
from pantheon.session_view.models import (
    ASSISTANT,
    SUBAGENT,
    SYSTEM,
    THINKING,
    TOOL,
    USER,
)

FIXTURE = Path(__file__).parent / "fixtures" / "session_view" / "claude_session.jsonl"


def test_load_parses_every_kind():
    conv = transcript.load(FIXTURE)
    kinds = {item.kind for item in conv.items}
    assert kinds == {USER, ASSISTANT, THINKING, TOOL, SYSTEM, SUBAGENT}


def test_parse_error_counts_only_bad_json():
    conv = transcript.load(FIXTURE)
    # exactly one malformed line in the fixture; every recognised-but-unhandled `type` (the
    # queue-operation/mode/etc block) must NOT count.
    assert conv.parse_errors == 1


def test_title_priority_custom_beats_later_ai_title():
    conv = transcript.load(FIXTURE)
    assert conv.title == "Pinned: Launcher Investigation"


def test_title_priority_first_user_then_ai_title(tmp_path):
    lines = [
        json.dumps({"type": "user", "message": {"role": "user", "content": "first real question"}}),
    ]
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    conv = transcript.load(path)
    assert conv.title == "first real question"

    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "ai-title", "aiTitle": "A better title"}) + "\n")
    conv = transcript.update(conv)
    assert conv.title == "A better title"


def test_model_effort_context_tokens_from_latest_assistant():
    conv = transcript.load(FIXTURE)
    assert conv.model == "claude-opus-4-8"
    assert conv.effort == "high"
    assert conv.context_tokens == 200 + 80 + 0 + 20


def test_cwd_branch_entrypoint_captured():
    conv = transcript.load(FIXTURE)
    assert conv.cwd == "C:/x/proj"
    assert conv.git_branch == "main"
    assert conv.entrypoint == "cli"


def test_tool_result_joins_by_id_and_replaces_in_place():
    conv = transcript.load(FIXTURE)
    tool_items = [it for it in conv.items if it.kind == TOOL]
    read_item = next(it for it in tool_items if it.tool.name == "Read")
    assert read_item.tool.done
    assert "CHECKPOINT" in read_item.tool.result_text
    assert not read_item.tool.is_error

    bash_item = next(it for it in tool_items if it.tool.name == "Bash")
    assert bash_item.tool.done
    assert bash_item.tool.is_error
    assert "not a git repository" in bash_item.tool.result_text

    # tool position is preserved -- the TOOL item for Read still sits before the Bash one
    assert tool_items.index(read_item) < tool_items.index(bash_item)


def test_pending_tools_left_unresolved():
    conv = transcript.load(FIXTURE)
    pending_names = {it.tool.name for it in conv.items if it.kind == TOOL and not it.tool.done}
    assert pending_names == {"Agent", "Skill", "WebFetch", "mcp__airtable__list_bases", "CustomToolXYZ"}
    assert set(conv.pending) == {it.tool.id for it in conv.items if it.kind == TOOL and not it.tool.done}


def test_sidechain_run_folds_into_one_subagent_item():
    conv = transcript.load(FIXTURE)
    subagent_items = [it for it in conv.items if it.kind == SUBAGENT]
    assert len(subagent_items) == 1
    item = subagent_items[0]
    assert item.sidechain
    assert "investigate the pipe timeout" in item.detail
    assert "runner blocks on stdin" in item.detail


def test_hook_attachment_becomes_system_item():
    conv = transcript.load(FIXTURE)
    hooks = [it for it in conv.items if it.kind == SYSTEM and it.text.startswith("hook:")]
    assert len(hooks) == 1
    assert hooks[0].text == "hook: PreToolUse"
    assert "allowed" in hooks[0].detail


def test_bridge_status_becomes_remote_control_note():
    conv = transcript.load(FIXTURE)
    assert any(it.kind == SYSTEM and it.text == "remote control active" for it in conv.items)


def test_system_reminder_user_text_becomes_note():
    conv = transcript.load(FIXTURE)
    notes = [it for it in conv.items if it.kind == SYSTEM and it.text == "note"]
    assert len(notes) == 1
    assert "reminder text" in notes[0].detail


@pytest.mark.parametrize("name,input_,expected", [
    ("Read", {"file_path": "C:/x/proj/CHECKPOINT.md"}, "Read CHECKPOINT.md"),
    ("Edit", {"file_path": "C:/x/proj/foo.py"}, "Edit foo.py"),
    ("Write", {"file_path": "/x/bar.txt"}, "Write bar.txt"),
    ("Bash", {"command": "git status --short"}, "Bash git status --short"),
    ("Grep", {"pattern": "TODO"}, "Grep TODO"),
    ("Glob", {"pattern": "**/*.py"}, "Glob **/*.py"),
    ("Agent", {"description": "fix the launcher"}, "Agent fix the launcher"),
    ("Skill", {"skill": "checkpoint"}, "Skill checkpoint"),
    ("WebFetch", {"url": "https://example.com/docs"}, "WebFetch example.com"),
    ("mcp__airtable__list_bases", {"baseId": "app123"}, "airtable: list_bases app123"),
    ("CustomToolXYZ", {"foo": "bar value"}, "CustomToolXYZ bar value"),
    ("NoInput", {}, "NoInput"),
])
def test_chip_summary_table(name, input_, expected):
    assert transcript.chip_summary(name, input_) == expected


def test_chip_summary_bash_truncates_to_sixty_chars():
    long_cmd = "x" * 100
    chip = transcript.chip_summary("Bash", {"command": long_cmd})
    assert chip.startswith("Bash ")
    assert len(chip) <= 72


def test_update_after_append_matches_fresh_load(tmp_path):
    original = FIXTURE.read_text(encoding="utf-8")
    path = tmp_path / "session.jsonl"
    path.write_text(original, encoding="utf-8")

    conv = transcript.load(path)

    extra_lines = [
        json.dumps({
            "type": "assistant", "effort": "medium", "timestamp": "2026-09-06T10:01:00Z", "uuid": "a-3",
            "message": {
                "model": "claude-sonnet-5",
                "usage": {"input_tokens": 5, "output_tokens": 5, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
                "content": [{"type": "text", "text": "one more turn"}],
            },
        }),
        json.dumps({"type": "user", "message": {"role": "user", "content": "one more question"}}),
    ]
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("\n".join(extra_lines) + "\n")

    updated = transcript.update(conv)
    fresh = transcript.load(path)

    assert len(updated.items) == len(fresh.items)
    assert [it.kind for it in updated.items] == [it.kind for it in fresh.items]
    assert [it.text for it in updated.items] == [it.text for it in fresh.items]
    assert updated.model == fresh.model == "claude-sonnet-5"
    assert updated.title == fresh.title
    assert updated.offset == fresh.offset == path.stat().st_size


def test_truncated_trailing_line_is_deferred(tmp_path):
    path = tmp_path / "session.jsonl"
    complete = json.dumps({"type": "user", "message": {"role": "user", "content": "complete line"}})
    path.write_text(complete + "\n", encoding="utf-8")
    conv = transcript.load(path)
    assert len(conv.items) == 1
    assert conv.offset == path.stat().st_size

    # append a partial (unterminated) line -- update() must not consume it
    with open(path, "a", encoding="utf-8") as fh:
        fh.write('{"type": "user", "message": {"role": "user", "content": "cut off mid')
    updated = transcript.update(conv)
    assert len(updated.items) == 1
    assert updated.offset == conv.offset   # nothing new was consumed

    # finish the line and add the trailing newline -- now it should parse
    with open(path, "a", encoding="utf-8") as fh:
        fh.write('-record"}}\n')
    finished = transcript.update(updated)
    assert len(finished.items) == 2
    assert finished.items[-1].text == "cut off mid-record"


def test_shrunk_file_triggers_reload(tmp_path):
    path = tmp_path / "session.jsonl"
    path.write_text(json.dumps({"type": "user", "message": {"role": "user", "content": "hello there"}}) + "\n", encoding="utf-8")
    conv = transcript.load(path)
    assert conv.offset > 0

    path.write_text(json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}}) + "\n", encoding="utf-8")
    assert path.stat().st_size < conv.offset
    reloaded = transcript.update(conv)
    assert len(reloaded.items) == 1
    assert reloaded.items[0].text == "hi"


def test_five_megabyte_file_loads_under_one_second(tmp_path):
    path = tmp_path / "big.jsonl"
    line = json.dumps({
        "type": "assistant", "effort": "high", "timestamp": "2026-09-06T10:00:00Z",
        "message": {
            "model": "claude-opus-4-8",
            "usage": {"input_tokens": 1, "output_tokens": 1, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
            "content": [{"type": "text", "text": "padding " * 40}],
        },
    })
    target_bytes = 5 * 1024 * 1024
    reps = target_bytes // (len(line) + 1) + 1
    with open(path, "w", encoding="utf-8") as fh:
        for _ in range(reps):
            fh.write(line + "\n")
    assert path.stat().st_size >= target_bytes

    start = time.perf_counter()
    conv = transcript.load(path)
    elapsed = time.perf_counter() - start
    assert conv.items
    assert elapsed < 1.0, f"load took {elapsed:.3f}s for a {path.stat().st_size} byte file"


def test_caps_from_models_are_enforced(tmp_path):
    from pantheon.session_view.models import CHIP_CHARS, INPUT_CHARS, RESULT_CHARS, TEXT_CHARS

    path = tmp_path / "caps.jsonl"
    huge_text = "z" * 100_000
    huge_input = {"file_path": "x"}
    lines = [
        json.dumps({
            "type": "assistant", "timestamp": "t",
            "message": {"content": [
                {"type": "text", "text": huge_text},
                {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": huge_text}},
            ]},
        }),
        json.dumps({
            "type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": huge_text, "is_error": False},
            ]},
        }),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    conv = transcript.load(path)

    assistant_item = next(it for it in conv.items if it.kind == ASSISTANT)
    assert len(assistant_item.text) <= TEXT_CHARS

    tool_item = next(it for it in conv.items if it.kind == TOOL)
    assert len(tool_item.tool.summary) <= CHIP_CHARS
    assert len(tool_item.tool.input_text) <= INPUT_CHARS
    assert len(tool_item.tool.result_text) <= RESULT_CHARS
