"""tools page readers. Every fixture lives under
`tmp_path` -- never the real `~/.claude` or `~/.codex` (the hard rule: this page's tests never
assert on the real box's contents, so a fixture-only run says nothing about what is really
installed)."""
from __future__ import annotations

import json
import os

import pytest

from pantheon.tools import inventory


def _write_skill(base, name, description="a skill", skill_name=None):
    d = base / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {skill_name or name}\ndescription: {description}\n---\nBody text.\n",
        encoding="utf-8",
    )
    return d


# --------------------------------------------------------------------------- claude_skills

def test_claude_skills_reads_name_and_description_from_fixture(tmp_path):
    skills_dir = tmp_path / "skills"
    _write_skill(skills_dir, "checkpoint", description="Rewrite CHECKPOINT.md in place.")
    result = inventory.claude_skills(str(skills_dir))
    assert len(result) == 1
    assert result[0].name == "checkpoint"
    assert result[0].description == "Rewrite CHECKPOINT.md in place."


def test_claude_skills_missing_skill_md_gets_a_marker(tmp_path):
    skills_dir = tmp_path / "skills"
    (skills_dir / "half-built").mkdir(parents=True)
    result = inventory.claude_skills(str(skills_dir))
    assert result[0].name == "half-built"
    assert result[0].description == "(no SKILL.md)"


def test_claude_skills_missing_dir_returns_empty(tmp_path):
    assert inventory.claude_skills(str(tmp_path / "nope")) == []


# --------------------------------------------------------------------------- codex_skills + sync

def test_sync_state_marks_linked_unlinked_and_codex_only(tmp_path):
    claude_dir = tmp_path / "claude_skills"
    agents_dir = tmp_path / "agents_skills"
    agents_dir.mkdir(parents=True)

    linked = _write_skill(claude_dir, "checkpoint")
    _write_skill(claude_dir, "grill-me")  # never linked -- claude only

    os.symlink(linked, agents_dir / "checkpoint", target_is_directory=True)

    # A codex-only symlink pointing somewhere that is not a Claude skill at all.
    codex_only_target = tmp_path / "elsewhere" / "codex-native"
    codex_only_target.mkdir(parents=True)
    (codex_only_target / "SKILL.md").write_text(
        "---\nname: codex-native\ndescription: only on codex\n---\n", encoding="utf-8"
    )
    os.symlink(codex_only_target, agents_dir / "codex-native", target_is_directory=True)

    claude = inventory.claude_skills(str(claude_dir))
    codex = inventory.codex_skills(str(agents_dir))
    state = inventory.sync_state(claude, codex)

    assert state["checkpoint"] == "in sync"
    assert state["grill-me"] == "claude only"
    assert state["codex-native"] == "codex only"


def test_sync_state_marks_a_broken_symlink(tmp_path):
    """`sync_state` on the pure `Skill` values `codex_skills` would hand it for a dangling
    symlink -- exercised directly rather than round-tripped through a real filesystem symlink,
    because creating a real (non-junction) symlink needs a privilege this sandbox does not have
    (`os.symlink` here silently falls back to something `Path.is_symlink()` never detects, so a
    real dangling-link fixture cannot be built portably in this environment)."""
    claude = [inventory.Skill(name="obsidian", description="query the vault", path="/claude/obsidian")]
    codex = [inventory.Skill(name="obsidian", description="(link broken)", path="/agents/obsidian",
                              target="/claude/obsidian-moved")]
    state = inventory.sync_state(claude, codex)
    assert state["obsidian"] == "link broken"


def test_codex_skills_marks_a_dangling_symlink_broken(monkeypatch, tmp_path):
    """`codex_skills` itself, with `Path.is_symlink`/`Path.exists` monkeypatched to the shape a
    real dangling symlink has (`is_symlink() == True`, `exists() == False`) -- the platform-
    accurate behaviour this sandbox's own filesystem cannot produce (see the test above)."""
    agents_dir = tmp_path / "agents_skills"
    (agents_dir / "obsidian").mkdir(parents=True)

    from pathlib import Path as PathType

    real_is_symlink = PathType.is_symlink
    real_exists = PathType.exists

    def fake_is_symlink(self):
        return self.name == "obsidian" or real_is_symlink(self)

    def fake_exists(self):
        if self.name == "obsidian":
            return False
        return real_exists(self)

    monkeypatch.setattr(PathType, "is_symlink", fake_is_symlink)
    monkeypatch.setattr(PathType, "exists", fake_exists)
    monkeypatch.setattr(os, "readlink", lambda p: "/claude/skills/obsidian-moved")

    result = inventory.codex_skills(str(agents_dir))
    assert len(result) == 1
    assert result[0].description == "(link broken)"
    assert result[0].target == "/claude/skills/obsidian-moved"


def test_codex_skills_missing_dir_returns_empty(tmp_path):
    assert inventory.codex_skills(str(tmp_path / "nope")) == []


# --------------------------------------------------------------------------- hooks + guards

FIXTURE_SETTINGS = {
    "hooks": {
        "PreToolUse": [
            {
                "matcher": "Write|Edit",
                "hooks": [{"type": "command", "command": "powershell.exe -File \"C:/hooks/guard.ps1\""}],
            }
        ],
        "SessionStart": [
            {"matcher": "", "hooks": [{"type": "command", "command": "powershell.exe -File \"C:/hooks/orient.ps1\""}]}
        ],
    },
    "permissions": {"allow": ["Bash(git *)"], "deny": ["Bash(rm -rf *)"]},
}


def test_claude_hooks_flattens_events_and_matchers(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps(FIXTURE_SETTINGS), encoding="utf-8")
    hooks = inventory.claude_hooks(str(settings))
    events = {h.event for h in hooks}
    assert events == {"PreToolUse", "SessionStart"}
    pre = next(h for h in hooks if h.event == "PreToolUse")
    assert pre.matcher == "Write|Edit"
    assert pre.command.endswith("guard.ps1\"")


def test_claude_guards_reads_allow_and_deny(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps(FIXTURE_SETTINGS), encoding="utf-8")
    guards = inventory.claude_guards(str(settings))
    assert guards.allow == ["Bash(git *)"]
    assert guards.deny == ["Bash(rm -rf *)"]


def test_claude_hooks_missing_file_returns_empty(tmp_path):
    assert inventory.claude_hooks(str(tmp_path / "nope.json")) == []
    guards = inventory.claude_guards(str(tmp_path / "nope.json"))
    assert guards.allow == [] and guards.deny == []


def test_codex_hooks_reads_the_same_shape(tmp_path):
    hooks_json = tmp_path / "hooks.json"
    hooks_json.write_text(json.dumps(FIXTURE_SETTINGS), encoding="utf-8")
    hooks = inventory.codex_hooks(str(hooks_json))
    assert len(hooks) == 2


FIXTURE_TOML = """
model = "gpt-6-astra"
sandbox_mode = "danger-full-access"

[mcp_servers.monarch]
command = "node"
args = ["monarch-server.js"]

[mcp_servers.monarch.env]
MONARCH_TOKEN = "sk-should-never-be-read"

[mcp_servers.node_repl]
command = "node"
"""


def test_codex_config_returns_model_sandbox_and_server_names(tmp_path):
    config_toml = tmp_path / "config.toml"
    config_toml.write_text(FIXTURE_TOML, encoding="utf-8")
    cfg = inventory.codex_config(str(config_toml))
    assert cfg["model"] == "gpt-6-astra"
    assert cfg["sandbox_mode"] == "danger-full-access"
    assert cfg["mcp_servers"] == ["monarch", "node_repl"]


def test_codex_config_never_surfaces_a_token_shaped_value(tmp_path):
    """The hard rule: even though the fixture TOML carries a fake token in a server's own `env`
    table, `codex_config`'s return value must not contain it anywhere."""
    config_toml = tmp_path / "config.toml"
    config_toml.write_text(FIXTURE_TOML, encoding="utf-8")
    cfg = inventory.codex_config(str(config_toml))
    dumped = json.dumps(cfg)
    assert "sk-should-never-be-read" not in dumped
    assert "MONARCH_TOKEN" not in dumped


def test_codex_config_never_reads_auth_json(tmp_path):
    """Never opens a file named `auth.json`, even if one sits right beside `config.toml` with a
    real-looking token in it -- `codex_config` takes only the TOML path and must not go looking
    for siblings."""
    (tmp_path / "auth.json").write_text(
        json.dumps({"tokens": {"access_token": "sk-should-never-be-read"}}), encoding="utf-8"
    )
    config_toml = tmp_path / "config.toml"
    config_toml.write_text(FIXTURE_TOML, encoding="utf-8")
    cfg = inventory.codex_config(str(config_toml))
    assert "sk-should-never-be-read" not in json.dumps(cfg)


def test_codex_config_missing_file_returns_defaults(tmp_path):
    cfg = inventory.codex_config(str(tmp_path / "nope.toml"))
    assert cfg == {"model": None, "sandbox_mode": None, "mcp_servers": []}
