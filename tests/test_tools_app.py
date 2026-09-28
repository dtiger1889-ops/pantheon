"""tools page: `render_report`/`build_snapshot`
against fixture dirs under `tmp_path`, never the real `~/.claude` or `~/.codex`."""
from __future__ import annotations

import json
import os

from pantheon import config as config_mod
from pantheon.tools import app as tools_app


def _cfg(tmp_path, **tools_page):
    defaults = {
        "claude_skills_dir": str(tmp_path / "claude_skills"),
        "agents_skills_dir": str(tmp_path / "agents_skills"),
        "claude_settings": str(tmp_path / "settings.json"),
        "codex_config": str(tmp_path / "config.toml"),
        "codex_hooks": str(tmp_path / "hooks.json"),
    }
    defaults.update(tools_page)
    return config_mod.Config(state_dir=str(tmp_path / "state"), tools_page=defaults)


def _write_skill(base, name, description="a skill"):
    d = base / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {description}\n---\n", encoding="utf-8")
    return d


def test_hook_filename_pulls_the_script_out_of_a_full_command():
    cmd = 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "C:/Home/x/.claude/hooks/guard.ps1"'
    assert tools_app.hook_filename(cmd) == "guard.ps1"


def test_hook_filename_falls_back_to_the_whole_string_when_no_script_matches():
    assert tools_app.hook_filename("echo hi") == "echo hi"


def test_missing_sources_report_none_found_not_an_exception(tmp_path):
    """a missing source (no skills dir, no settings.json, no hooks.json) shows
    a plain 'none found'/'no skills found' line rather than raising."""
    cfg = _cfg(tmp_path)
    report = tools_app.render_report(cfg)
    assert "no skills found" in report
    assert "Claude hooks: none found" in report
    assert "Codex hooks: none found" in report
    assert "none found" in report  # the empty deny list


def test_render_report_has_all_three_sections_in_order(tmp_path):
    claude_dir = tmp_path / "claude_skills"
    _write_skill(claude_dir, "checkpoint", "Rewrite CHECKPOINT.md")
    cfg = _cfg(tmp_path, claude_skills_dir=str(claude_dir))
    report = tools_app.render_report(cfg)
    assert report.index("SKILLS") < report.index("HOOKS") < report.index("GUARDS")
    assert "checkpoint" in report
    assert "claude only" in report  # never linked into agents_skills_dir


def test_filter_narrows_to_matching_skill_names(tmp_path):
    claude_dir = tmp_path / "claude_skills"
    _write_skill(claude_dir, "checkpoint")
    _write_skill(claude_dir, "obsidian")
    cfg = _cfg(tmp_path, claude_skills_dir=str(claude_dir))
    report = tools_app.render_report(cfg, query="check")
    assert "checkpoint" in report
    assert "obsidian" not in report


def test_filter_with_no_matches_says_so(tmp_path):
    claude_dir = tmp_path / "claude_skills"
    _write_skill(claude_dir, "checkpoint")
    cfg = _cfg(tmp_path, claude_skills_dir=str(claude_dir))
    report = tools_app.render_report(cfg, query="zzz")
    assert "no skills matching 'zzz'" in report


def test_sort_by_sync_groups_the_states(tmp_path):
    claude_dir = tmp_path / "claude_skills"
    agents_dir = tmp_path / "agents_skills"
    agents_dir.mkdir(parents=True)
    linked = _write_skill(claude_dir, "aaa-linked")
    _write_skill(claude_dir, "zzz-unlinked")
    os.symlink(linked, agents_dir / "aaa-linked", target_is_directory=True)
    cfg = _cfg(tmp_path, claude_skills_dir=str(claude_dir), agents_skills_dir=str(agents_dir))
    names_by_name = tools_app.sorted_skill_names(
        {"aaa-linked": "in sync", "zzz-unlinked": "claude only"}, sort="name"
    )
    names_by_sync = tools_app.sorted_skill_names(
        {"aaa-linked": "in sync", "zzz-unlinked": "claude only"}, sort="sync"
    )
    assert names_by_name == ["aaa-linked", "zzz-unlinked"]
    # "claude only" sorts before "in sync" alphabetically
    assert names_by_sync == ["zzz-unlinked", "aaa-linked"]


def test_build_snapshot_matches_the_shape_bin_tools_show_prints(tmp_path):
    claude_dir = tmp_path / "claude_skills"
    _write_skill(claude_dir, "checkpoint")
    cfg = _cfg(tmp_path, claude_skills_dir=str(claude_dir))
    snap = tools_app.build_snapshot(cfg)
    assert set(snap.keys()) == {"skills", "hooks", "guards"}
    assert "checkpoint" in snap["skills"]["claude"]
    # round-trips through json.dumps the way bin/tools_show does
    json.dumps(snap)


def test_no_write_calls_anywhere_in_the_tools_package():
    """`grep` for a write call in `tools/` finds none. Enforced here in
    Python rather than a shell grep so it runs the same way on any machine pytest runs on."""
    import pathlib
    import re

    pkg_dir = pathlib.Path(tools_app.__file__).parent
    write_pattern = re.compile(r'open\([^)]*["\']w')
    for path in pkg_dir.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert not write_pattern.search(text), f"{path} opens a file for writing"
        assert ".write_text(" not in text
        assert "os.remove" not in text and "shutil.rmtree" not in text
