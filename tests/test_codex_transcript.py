"""Tests for `pantheon/session_view/codex_transcript.py`.

`tests/fixtures/session_view/codex_rollout.jsonl` is a hand-built synthetic rollout covering every
record shape the spec names -- fake cwd, fake call ids, never a copy of a real rollout file.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from pantheon.session_view import codex_transcript
from pantheon.session_view.models import ASSISTANT, SYSTEM, THINKING, TOOL, USER

FIXTURE = Path(__file__).parent / "fixtures" / "session_view" / "codex_rollout.jsonl"


def test_load_parses_every_kind():
    conv = codex_transcript.load(FIXTURE)
    kinds = {item.kind for item in conv.items}
    assert kinds == {USER, ASSISTANT, THINKING, TOOL, SYSTEM}


def test_provider_is_codex():
    conv = codex_transcript.load(FIXTURE)
    assert conv.provider == "codex"


def test_parse_error_counts_only_bad_json():
    conv = codex_transcript.load(FIXTURE)
    assert conv.parse_errors == 1


def test_developer_and_app_context_become_system():
    conv = codex_transcript.load(FIXTURE)
    system_items = [it for it in conv.items if it.kind == SYSTEM]
    assert any(it.text == "developer note" for it in system_items)
    notes = [it for it in system_items if it.text == "note"]
    assert len(notes) == 1
    assert "AGENTS.md" in notes[0].detail


def test_real_user_text_becomes_user_item():
    conv = codex_transcript.load(FIXTURE)
    user_items = [it for it in conv.items if it.kind == USER]
    assert len(user_items) == 1
    assert "pipe timeout" in user_items[0].text


def test_assistant_output_text():
    conv = codex_transcript.load(FIXTURE)
    assistant_items = [it for it in conv.items if it.kind == ASSISTANT]
    assert len(assistant_items) == 1
    assert "file-redirection" in assistant_items[0].text


def test_reasoning_with_summary_and_without():
    conv = codex_transcript.load(FIXTURE)
    thinking_items = [it for it in conv.items if it.kind == THINKING]
    assert len(thinking_items) == 2
    assert "blocks reading stdin" in thinking_items[0].text
    assert thinking_items[1].text == "(reasoning not shared by Codex)"


def test_model_from_world_state():
    conv = codex_transcript.load(FIXTURE)
    assert conv.model == "gpt-5.1-codex"


def test_context_tokens_from_token_usage_record():
    conv = codex_transcript.load(FIXTURE)
    assert conv.context_tokens == 4321


def test_cwd_and_entrypoint_from_session_meta():
    conv = codex_transcript.load(FIXTURE)
    assert conv.cwd == "C:/x/proj"
    assert conv.entrypoint == "codex-cli"


def test_exec_tool_call_resolved_and_pending_one_left():
    conv = codex_transcript.load(FIXTURE)
    tools = [it for it in conv.items if it.kind == TOOL]
    assert len(tools) == 2
    exec_item = next(it for it in tools if it.tool.name == "exec")
    assert exec_item.tool.done
    assert not exec_item.tool.is_error
    assert "wrote 42 bytes" in exec_item.tool.result_text
    assert exec_item.tool.summary.startswith("exec ")
    assert len(exec_item.tool.summary) <= 72

    patch_item = next(it for it in tools if it.tool.name == "apply_patch")
    assert not patch_item.tool.done
    assert patch_item.tool.id in conv.pending


def test_task_complete_becomes_turn_done():
    conv = codex_transcript.load(FIXTURE)
    assert any(it.kind == SYSTEM and it.text == "turn done" for it in conv.items)


def test_update_after_append_matches_fresh_load(tmp_path):
    original = FIXTURE.read_text(encoding="utf-8")
    path = tmp_path / "rollout.jsonl"
    path.write_text(original, encoding="utf-8")

    conv = codex_transcript.load(path)

    extra = [
        json.dumps({"timestamp": "2026-09-06T12:00:13Z", "type": "response_item",
                    "payload": {"type": "message", "role": "user",
                                "content": [{"type": "input_text", "text": "one more thing"}]}}),
        json.dumps({"timestamp": "2026-09-06T12:00:14Z", "type": "token_usage_record",
                    "payload": {"usage": {"total_tokens": 5000}}}),
    ]
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("\n".join(extra) + "\n")

    updated = codex_transcript.update(conv)
    fresh = codex_transcript.load(path)

    assert len(updated.items) == len(fresh.items)
    assert [it.kind for it in updated.items] == [it.kind for it in fresh.items]
    assert updated.context_tokens == fresh.context_tokens == 5000
    assert updated.offset == fresh.offset == path.stat().st_size


def test_truncated_trailing_line_is_deferred(tmp_path):
    path = tmp_path / "rollout.jsonl"
    complete = json.dumps({"timestamp": "t", "type": "response_item",
                            "payload": {"type": "message", "role": "user",
                                        "content": [{"type": "input_text", "text": "complete"}]}})
    path.write_text(complete + "\n", encoding="utf-8")
    conv = codex_transcript.load(path)
    assert len(conv.items) == 1

    with open(path, "a", encoding="utf-8") as fh:
        fh.write('{"timestamp": "t2", "type": "response_item", "payload": {"type": "message"')
    updated = codex_transcript.update(conv)
    assert len(updated.items) == 1
    assert updated.offset == conv.offset


def test_shrunk_file_triggers_reload(tmp_path):
    path = tmp_path / "rollout.jsonl"
    rec1 = json.dumps({"timestamp": "t", "type": "response_item",
                        "payload": {"type": "message", "role": "user",
                                    "content": [{"type": "input_text", "text": "hello there codex"}]}})
    path.write_text(rec1 + "\n", encoding="utf-8")
    conv = codex_transcript.load(path)
    assert conv.offset > 0

    rec2 = json.dumps({"timestamp": "t", "type": "response_item",
                        "payload": {"type": "message", "role": "user",
                                    "content": [{"type": "input_text", "text": "hi"}]}})
    path.write_text(rec2 + "\n", encoding="utf-8")
    assert path.stat().st_size < conv.offset
    reloaded = codex_transcript.update(conv)
    assert len(reloaded.items) == 1
    assert reloaded.items[0].text == "hi"


def test_five_megabyte_file_loads_under_one_second(tmp_path):
    path = tmp_path / "big_rollout.jsonl"
    line = json.dumps({
        "timestamp": "2026-09-06T12:00:00Z", "type": "response_item",
        "payload": {"type": "message", "role": "assistant",
                    "content": [{"type": "output_text", "text": "padding " * 40}]},
    })
    target_bytes = 5 * 1024 * 1024
    reps = target_bytes // (len(line) + 1) + 1
    with open(path, "w", encoding="utf-8") as fh:
        for _ in range(reps):
            fh.write(line + "\n")
    assert path.stat().st_size >= target_bytes

    start = time.perf_counter()
    conv = codex_transcript.load(path)
    elapsed = time.perf_counter() - start
    assert conv.items
    assert elapsed < 1.0, f"load took {elapsed:.3f}s for a {path.stat().st_size} byte file"


def test_caps_from_models_are_enforced(tmp_path):
    from pantheon.session_view.models import RESULT_CHARS, TEXT_CHARS

    path = tmp_path / "caps.jsonl"
    huge_text = "z" * 100_000
    lines = [
        json.dumps({"timestamp": "t", "type": "response_item",
                    "payload": {"type": "message", "role": "assistant",
                                "content": [{"type": "output_text", "text": huge_text}]}}),
        json.dumps({"timestamp": "t", "type": "response_item",
                    "payload": {"type": "custom_tool_call", "call_id": "c1", "name": "exec",
                                "input": {"cmd": "echo hi"}}}),
        json.dumps({"timestamp": "t", "type": "response_item",
                    "payload": {"type": "custom_tool_call_output", "call_id": "c1",
                                "output": [{"type": "text", "text": huge_text}]}}),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    conv = codex_transcript.load(path)

    assistant_item = next(it for it in conv.items if it.kind == ASSISTANT)
    assert len(assistant_item.text) <= TEXT_CHARS

    tool_item = next(it for it in conv.items if it.kind == TOOL)
    assert len(tool_item.tool.result_text) <= RESULT_CHARS


# ---------------------------------------------------------------------------------- find_rollout

def _write_rollout(path: Path, cwd: str, originator: str, ts: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = json.dumps({"timestamp": ts, "type": "session_meta",
                       "payload": {"cwd": cwd, "originator": originator, "cli_version": "0.9.0"}})
    path.write_text(rec + "\n", encoding="utf-8")


def test_find_rollout_picks_newest_matching_cwd(tmp_path):
    codex_home = tmp_path / "codex_home"
    day = codex_home / "sessions" / "2026" / "09" / "06"

    _write_rollout(day / "rollout-2026-09-06T10-00-00-aaa.jsonl", "C:/x/proj", "codex-cli", "2026-09-06T10:00:00Z")
    _write_rollout(day / "rollout-2026-09-06T11-00-00-bbb.jsonl", "C:/x/proj", "codex-cli", "2026-09-06T11:00:00Z")
    _write_rollout(day / "rollout-2026-09-06T12-00-00-ccc.jsonl", "C:/x/other", "codex-cli", "2026-09-06T12:00:00Z")

    found = codex_transcript.find_rollout("C:/x/proj", "2026-09-06T09:00:00Z", codex_home=codex_home)
    assert found is not None
    assert "bbb" in found.name


def test_find_rollout_respects_not_before(tmp_path):
    codex_home = tmp_path / "codex_home"
    day = codex_home / "sessions" / "2026" / "09" / "06"
    _write_rollout(day / "rollout-2026-09-06T08-00-00-old.jsonl", "C:/x/proj", "codex-cli", "2026-09-06T08:00:00Z")

    found = codex_transcript.find_rollout("C:/x/proj", "2026-09-06T09:00:00Z", codex_home=codex_home)
    assert found is None


def test_find_rollout_no_match_returns_none(tmp_path):
    codex_home = tmp_path / "codex_home"
    found = codex_transcript.find_rollout("C:/nowhere", "2026-01-01T00:00:00Z", codex_home=codex_home)
    assert found is None
