"""Throwaway-trial conversations stay out of SESSIONS: a finished
conversation whose folder is under `[sessions] hide_folders` (temp folders by default) is never
listed and never makes a project heading. Nothing is deleted."""
from __future__ import annotations

import json
import os
from pathlib import Path

from pantheon import config as config_mod
from pantheon.models import AgentState, AgentStatus
from pantheon.session_view import models as session_models
from pantheon.session_view import recent as recent_mod


def write_transcript(home: Path, slug: str, sid: str, cwd: str | None, text: str) -> Path:
    folder = home / "projects" / slug
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{sid}.jsonl"
    rec = {"type": "user", "sessionId": sid, "message": {"role": "user", "content": text}}
    if cwd:
        rec["cwd"] = cwd
    path.write_text(json.dumps(rec) + "\n", encoding="utf-8")
    return path


def test_the_defaults_hide_every_spelling_of_a_temp_folder(monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Home\x\AppData\Local")
    s = config_mod.Config().sessions_settings()
    for cwd in ("C:/msys64/tmp/pw/asstest-folder", r"C:\msys64\tmp\pw\paprobe", "/tmp/pw/x",
                "/c/msys64/tmp/pw/nameprobe", "c:/home/x/appdata/local/temp/scratch",
                r"C:\Windows\Temp", "C:/msys64/tmp"):
        assert s.hides(cwd), cwd
    for cwd in ("C:/Home/x/Documents/Projects/loom-os", "C:/msys64/tmpfoo", "", "/tmpx"):
        assert not s.hides(cwd), cwd


def test_an_unset_variable_drops_that_entry_and_an_empty_list_hides_nothing(monkeypatch):
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    s = config_mod.Sessions.from_dict({"hide_folders": ["%LOCALAPPDATA%/Temp", "D:/scratch"]})
    assert s.folders() == ("d:/scratch",)
    assert not config_mod.Sessions.from_dict({"hide_folders": []}).hides("C:/msys64/tmp/x")


def test_the_sidebar_list_leaves_trial_folders_out(tmp_path):
    home = tmp_path / "claude"
    write_transcript(home, "C--msys64-tmp-pw-asstest-folder", "trial", r"C:\msys64\tmp\pw\asstest-folder",
                     "Reply with just the word ok.")
    write_transcript(home, "C--msys64-tmp-pw-paprobe", "nocwd", None, "probe")
    write_transcript(home, "C--Home-x-Documents-Projects-loom-os", "real",
                     r"C:\Home\x\Documents\Projects\loom-os", "Plan the week")
    recent_mod._recent_cache.clear()
    cfg = config_mod.Config(projects_root="C:/Home/x/Documents/Projects", claude_home=str(home),
                            state_dir=str(tmp_path / "state"))
    ended = AgentState(session_id="ended-trial", provider="claude", status=AgentStatus.GONE,
                       project="paprobe", cwd="C:/msys64/tmp/pw/paprobe")
    running = AgentState(session_id="running-trial", provider="claude", status=AgentStatus.WORKING,
                         project="paprobe", cwd="C:/msys64/tmp/pw/paprobe", window_index=9,
                         tmux_session="pantheon")
    got = recent_mod.entries([ended, running], cfg, claude_home=home, codex_home=tmp_path / "codex")
    ids = {e.session_id: e.group for e in got}
    assert "real" in ids
    assert "trial" not in ids and "nocwd" not in ids and "ended-trial" not in ids
    assert ids["running-trial"] == session_models.LIVE         # a running one is still shown
    # Nothing was deleted.
    assert (home / "projects" / "C--msys64-tmp-pw-asstest-folder" / "trial.jsonl").exists()


def test_the_real_config_file_carries_the_setting():
    root = Path(__file__).resolve().parents[1]
    for name in ("pantheon.toml", "pantheon.example.toml"):
        cfg = config_mod.load(root / name)
        assert "C:/msys64/tmp" in cfg.sessions_settings().hide_folders, name
