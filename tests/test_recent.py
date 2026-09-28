""" package C: `pantheon/session_view/recent.py`. Every home (`claude_home`
/ `codex_home`) is a synthetic `tmp_path` tree built line by line -- never a real transcript.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from pantheon import config as config_mod
from pantheon.dispatch import sessions as sessions_mod
from pantheon.models import AgentState, AgentStatus
from pantheon.session_view import models as session_models
from pantheon.session_view import recent as recent_mod


@pytest.fixture(autouse=True)
def _clear_recent_cache():
    recent_mod._recent_cache.clear()
    yield
    recent_mod._recent_cache.clear()


def make_cfg(tmp_path: Path) -> config_mod.Config:
    return config_mod.Config(state_dir=str(tmp_path / "state"), projects_root=str(tmp_path / "Claude"))


def write_claude_transcript(claude_home: Path, project_dir: str, session_id: str, lines: list[dict]) -> Path:
    folder = claude_home / "projects" / sessions_mod.slug_for(project_dir)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{session_id}.jsonl"
    path.write_text("\n".join(json.dumps(d) for d in lines) + "\n", encoding="utf-8")
    return path


def write_codex_rollout(codex_home: Path, name: str, lines: list[dict]) -> Path:
    folder = codex_home / "sessions" / "2026" / "09" / "06"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"rollout-{name}.jsonl"
    path.write_text("\n".join(json.dumps(d) for d in lines) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------- live_entries: claude


def test_live_entries_claude_row_gets_transcript_path_and_title(tmp_path):
    project = "C:/Home/x/Documents/Projects/habit_notes"
    home = tmp_path / "claude_home"
    path = write_claude_transcript(home, project, "sess-1", [
        {"type": "user", "message": {"content": "fix the launcher"}, "cwd": project},
        {"type": "ai-title", "aiTitle": "Fix the launcher gate"},
    ])
    cfg = make_cfg(tmp_path)
    row = AgentState(session_id="sess-1", provider="claude", status=AgentStatus.WORKING,
                     project="habit_notes", cwd=project, window_index=3, tmux_session="pantheon")

    out = recent_mod.live_entries([row], cfg, claude_home=home)

    assert len(out) == 1
    entry = out[0]
    assert entry.group == session_models.LIVE
    assert entry.session_id == "sess-1"
    assert entry.transcript_path == str(path)
    assert entry.title == "Fix the launcher gate"
    assert entry.provider == "claude"
    assert entry.window_index == 3
    assert entry.tmux_session == "pantheon"
    assert entry.status_text == AgentStatus.WORKING.label
    assert entry.style is None       # a plain working row gets no colour
    assert entry.needs_human is False


def test_live_entries_needs_human_row_carries_the_warning_style(tmp_path):
    cfg = make_cfg(tmp_path)
    row = AgentState(session_id="sess-2", provider="claude", status=AgentStatus.BLOCKED_PERMISSION,
                     project="almanac", cwd="C:/x/almanac")

    out = recent_mod.live_entries([row], cfg, claude_home=tmp_path / "claude_home")

    entry = out[0]
    assert entry.needs_human is True
    assert entry.style == "warning"
    assert entry.transcript_path is None   # no transcript on disk -> None, never a crash
    assert entry.title.startswith("untitled · almanac")   # no transcript -> untitled · project


def test_live_entries_claude_row_with_no_transcript_falls_back_to_project(tmp_path):
    cfg = make_cfg(tmp_path)
    row = AgentState(session_id="sess-3", provider="claude", status=AgentStatus.WORKING,
                     project="loom-os", cwd="C:/x/loom-os")

    out = recent_mod.live_entries([row], cfg, claude_home=tmp_path / "claude_home")

    assert out[0].transcript_path is None
    assert out[0].title.startswith("untitled · loom-os")   # never a bare id


# ---------------------------------------------------------------- live_entries: codex


def test_live_entries_codex_row_matches_newest_rollout_with_same_cwd(tmp_path):
    codex_home = tmp_path / "codex_home"
    cwd = "C:/Home/x/Documents/Projects/habit_notes"
    write_codex_rollout(codex_home, "old", [
        {"timestamp": "2026-09-06T10:00:00Z", "type": "session_meta", "payload": {"cwd": cwd}},
        {"timestamp": "2026-09-06T10:00:01Z", "type": "response_item",
         "payload": {"type": "message", "role": "user", "content": [{"text": "old task"}]}},
    ])
    newest = write_codex_rollout(codex_home, "new", [
        {"timestamp": "2026-09-06T12:00:00Z", "type": "session_meta", "payload": {"cwd": cwd}},
        {"timestamp": "2026-09-06T12:00:01Z", "type": "response_item",
         "payload": {"type": "message", "role": "user", "content": [{"text": "newest task"}]}},
    ])
    write_codex_rollout(codex_home, "other-cwd", [
        {"timestamp": "2026-09-06T13:00:00Z", "type": "session_meta", "payload": {"cwd": "C:/somewhere/else"}},
    ])
    cfg = make_cfg(tmp_path)
    row = AgentState(session_id="job-1", provider="codex", status=AgentStatus.RUNNING,
                     project="habit_notes", cwd=cwd, job_id="job-1")

    out = recent_mod.live_entries([row], cfg, codex_home=codex_home)

    entry = out[0]
    assert entry.transcript_path == str(newest)
    assert entry.title == "newest task"


def test_live_entries_codex_row_with_no_matching_rollout_falls_back_to_project(tmp_path):
    codex_home = tmp_path / "codex_home"
    write_codex_rollout(codex_home, "elsewhere", [
        {"timestamp": "2026-09-06T10:00:00Z", "type": "session_meta", "payload": {"cwd": "C:/nope"}},
    ])
    cfg = make_cfg(tmp_path)
    row = AgentState(session_id="job-2", provider="codex", status=AgentStatus.RUNNING,
                     project="hiking_log_v2", cwd="C:/x/hiking_log_v2", job_id="job-2")

    out = recent_mod.live_entries([row], cfg, codex_home=codex_home)

    assert out[0].transcript_path is None
    assert out[0].title.startswith("untitled · hiking_log_v2")   # never a bare id


# ---------------------------------------------------------------- recent_entries


def _touch(path: Path, mtime: float) -> None:
    os.utime(path, (mtime, mtime))


def test_recent_entries_are_sorted_newest_first_and_exclude_live_ids(tmp_path):
    home = tmp_path / "claude_home"
    p1 = write_claude_transcript(home, "C:/x/proj_a", "old-sess", [
        {"type": "user", "message": {"content": "task one"}, "cwd": "C:/x/proj_a"},
    ])
    p2 = write_claude_transcript(home, "C:/x/proj_b", "mid-sess", [
        {"type": "user", "message": {"content": "task two"}, "cwd": "C:/x/proj_b"},
    ])
    p3 = write_claude_transcript(home, "C:/x/proj_a", "new-sess", [
        {"type": "user", "message": {"content": "task three"}, "cwd": "C:/x/proj_a"},
    ])
    _touch(p1, 1_000_000)
    _touch(p2, 2_000_000)
    _touch(p3, 3_000_000)
    cfg = make_cfg(tmp_path)

    out = recent_mod.recent_entries(cfg, limit=20, exclude={"mid-sess"}, claude_home=home,
                                    codex_home=tmp_path / "codex_home")

    assert [e.session_id for e in out] == ["new-sess", "old-sess"]
    assert all(e.group == session_models.RECENT for e in out)


def test_claude_recent_project_is_the_cwd_basename_not_the_slug(tmp_path):
    # Regression: recent Claude rows were labelled with the raw
    # `~/.claude/projects/<slug>` directory name -- the `C--Home-x-Documents-Projects-...` wall.
    home = tmp_path / "claude_home"
    project = "C:/Home/x/Documents/Projects/project_lanterns"
    p = write_claude_transcript(home, project, "sess-x", [
        {"type": "user", "message": {"content": "hello"}, "cwd": project},
    ])
    _touch(p, 1_000_000)
    cfg = make_cfg(tmp_path)

    out = recent_mod.recent_entries(cfg, limit=20, claude_home=home, codex_home=tmp_path / "codex_home")

    assert len(out) == 1
    assert out[0].project == "project_lanterns"


def test_claude_recent_project_deslugs_when_the_transcript_has_no_cwd(tmp_path):
    home = tmp_path / "claude_home"
    cfg = make_cfg(tmp_path)
    project = f"{cfg.projects_root}/habit_notes"   # one level under projects_root
    p = write_claude_transcript(home, project, "sess-y", [
        {"type": "user", "message": {"content": "no cwd on this record"}},
    ])
    _touch(p, 1_000_000)

    out = recent_mod.recent_entries(cfg, limit=20, claude_home=home, codex_home=tmp_path / "codex_home")

    assert out[0].cwd == ""                     # nothing to read a cwd from
    # The projects_root prefix is stripped so it is legible; the slug flattens the underscore to a
    # dash and that is irreversible, so `habit-notes` (not the full `...-Claude-habit-notes` wall).
    assert out[0].project == "habit-notes"


def test_claude_recent_project_basename_handles_a_windows_backslash_cwd(tmp_path):
    # The live-desk bug: real transcripts store cwd with backslashes and MSYS
    # Python's PosixPath.name did not split on them, so every RECENT row drew the same
    # `C:\\Home\\x\\Documen…` prefix. The basename must tolerate backslashes.
    home = tmp_path / "claude_home"
    win_cwd = "C:\\Home\\x\\Documents\\Projects\\project_lanterns"
    p = write_claude_transcript(home, "C:/Home/x/Documents/Projects/project_lanterns", "sess-w", [
        {"type": "user", "message": {"content": "hi"}, "cwd": win_cwd},
    ])
    _touch(p, 1_000_000)
    cfg = make_cfg(tmp_path)

    out = recent_mod.recent_entries(cfg, limit=20, claude_home=home, codex_home=tmp_path / "codex_home")

    assert out[0].project == "project_lanterns"


def test_codex_recent_project_basename_handles_a_windows_backslash_cwd(tmp_path):
    codex_home = tmp_path / "codex_home"
    path = write_codex_rollout(codex_home, "winjob", [
        {"timestamp": "2026-09-06T09:00:00Z", "type": "session_meta",
         "payload": {"cwd": "C:\\Home\\x\\Documents\\Projects\\pottery_studios"}},
        {"timestamp": "2026-09-06T09:00:01Z", "type": "response_item",
         "payload": {"type": "message", "role": "user", "content": [{"text": "sweep"}]}},
    ])
    _touch(path, 1_000_000)
    cfg = make_cfg(tmp_path)

    out = recent_mod.recent_entries(cfg, limit=20, claude_home=tmp_path / "claude_home", codex_home=codex_home)

    assert out[0].project == "pottery_studios"


def test_recent_entries_include_codex_rollouts_with_first_user_message_as_title(tmp_path):
    codex_home = tmp_path / "codex_home"
    path = write_codex_rollout(codex_home, "job", [
        {"timestamp": "2026-09-06T09:00:00Z", "type": "session_meta", "payload": {"cwd": "C:/x/astra"}},
        {"timestamp": "2026-09-06T09:00:01Z", "type": "response_item",
         "payload": {"type": "message", "role": "developer", "content": [{"text": "<app-context>ignore"}]}},
        {"timestamp": "2026-09-06T09:00:02Z", "type": "response_item",
         "payload": {"type": "message", "role": "user", "content": [{"text": "ship the runner fix"}]}},
    ])
    cfg = make_cfg(tmp_path)

    out = recent_mod.recent_entries(cfg, limit=20, claude_home=tmp_path / "claude_home", codex_home=codex_home)

    assert len(out) == 1
    assert out[0].transcript_path == str(path)
    assert out[0].title == "ship the runner fix"
    assert out[0].project == "astra"


def test_recent_entries_respects_limit(tmp_path):
    home = tmp_path / "claude_home"
    for i in range(5):
        p = write_claude_transcript(home, f"C:/x/proj{i}", f"sess-{i}", [
            {"type": "user", "message": {"content": f"task {i}"}, "cwd": f"C:/x/proj{i}"},
        ])
        _touch(p, 1_000_000 + i)
    cfg = make_cfg(tmp_path)

    out = recent_mod.recent_entries(cfg, limit=3, claude_home=home, codex_home=tmp_path / "codex_home")

    assert len(out) == 3


def test_recent_entries_scan_is_cached_for_30_seconds(tmp_path, monkeypatch):
    home = tmp_path / "claude_home"
    p1 = write_claude_transcript(home, "C:/x/proj_a", "sess-a", [
        {"type": "user", "message": {"content": "task a"}, "cwd": "C:/x/proj_a"},
    ])
    _touch(p1, 1_000_000)
    cfg = make_cfg(tmp_path)
    codex_home = tmp_path / "codex_home"

    clock = [0.0]
    monkeypatch.setattr(recent_mod.time_mod, "monotonic", lambda: clock[0])

    first = recent_mod.recent_entries(cfg, limit=20, claude_home=home, codex_home=codex_home)
    assert [e.session_id for e in first] == ["sess-a"]

    # A second transcript appears, but the cache is still fresh (< 30s) -- it must not show up yet.
    p2 = write_claude_transcript(home, "C:/x/proj_b", "sess-b", [
        {"type": "user", "message": {"content": "task b"}, "cwd": "C:/x/proj_b"},
    ])
    _touch(p2, 2_000_000)
    clock[0] = 10.0
    still_cached = recent_mod.recent_entries(cfg, limit=20, claude_home=home, codex_home=codex_home)
    assert [e.session_id for e in still_cached] == ["sess-a"]

    # Past 30 seconds, the scan runs again and picks up the new file.
    clock[0] = 31.0
    refreshed = recent_mod.recent_entries(cfg, limit=20, claude_home=home, codex_home=codex_home)
    assert {e.session_id for e in refreshed} == {"sess-a", "sess-b"}


# ---------------------------------------------------------------- entries()


def test_entries_is_live_then_recent_excluding_live_ids(tmp_path):
    home = tmp_path / "claude_home"
    write_claude_transcript(home, "C:/x/live_proj", "live-sess", [
        {"type": "user", "message": {"content": "still going"}, "cwd": "C:/x/live_proj"},
    ])
    write_claude_transcript(home, "C:/x/other_proj", "recent-sess", [
        {"type": "user", "message": {"content": "finished earlier"}, "cwd": "C:/x/other_proj"},
    ])
    cfg = make_cfg(tmp_path)
    row = AgentState(session_id="live-sess", provider="claude", status=AgentStatus.WORKING,
                     project="live_proj", cwd="C:/x/live_proj")

    out = recent_mod.entries([row], cfg, claude_home=home, codex_home=tmp_path / "codex_home")

    assert out[0].session_id == "live-sess"
    assert out[0].group == session_models.LIVE
    ids = [e.session_id for e in out]
    assert "live-sess" not in ids[1:]   # never duplicated into the recent tail
    assert "recent-sess" in ids
