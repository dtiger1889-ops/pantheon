"""workflow presets (acceptance 1-3), plus build B step 1: `pantheon open`
applies --model / --effort to the new Claude window with no deck open."""
from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from pantheon import config as config_mod
from pantheon import session_ctl
from pantheon.dispatch import presets as presets_mod
from pantheon.dispatch import projects as projects_mod
from pantheon.models import LaunchResult

PRESETS = {
    "hikinglog-reader": {"project": "hiking_log_v2", "who": "codex-pc", "model": "sonnet",
                         "effort": "medium", "mode": "acceptEdits", "message": "read the guide"},
    "templated": {"who": "claude", "message": "Re-orient in {project}."},
}


def _cfg(tmp_path, presets=PRESETS):
    root = tmp_path / "Claude"
    for name in ("hiking_log_v2", "loom-os"):
        (root / name).mkdir(parents=True)
        (root / name / "CLAUDE.md").write_text("x", encoding="utf-8")
    cfg = config_mod.Config(vault=str(tmp_path / "vault"), state_dir=str(tmp_path / "state"))
    cfg = dataclasses.replace(cfg, projects_root=str(root), presets=presets)
    config_mod.ensure_state_dirs(cfg)
    return cfg


class FakeProvider:
    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[tuple] = []

    def capabilities(self):
        return {"interactive_tmux"}

    def launch(self, project_dir, briefing_path, interactive, options=None):
        self.calls.append((project_dir, briefing_path, interactive, dict(options or {})))
        return LaunchResult(True, "tmux", 7, Path(project_dir).name, tmux_pane="%7",
                            message=f"{self.name} is open in window 7")


@pytest.fixture
def wired(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    providers = {"claude": FakeProvider("claude"), "codex": FakeProvider("codex")}
    typed: list[tuple] = []
    monkeypatch.setattr(projects_mod, "get_providers", lambda cfg: providers)
    monkeypatch.setattr(config_mod, "load", lambda *a, **k: cfg)
    monkeypatch.setattr(session_ctl, "set_model", lambda t, v, tmux=None: typed.append(("model", t, v)))
    monkeypatch.setattr(session_ctl, "set_effort", lambda t, v, tmux=None: typed.append(("effort", t, v)))
    monkeypatch.setattr(session_ctl, "set_mode", lambda t, c, v, tmux=None: typed.append(("mode", t, v)))
    return cfg, providers, typed


# ---------------------------------------------------------------- acceptance 1


def test_resolve_returns_the_preset_verbatim(tmp_path):
    assert presets_mod.resolve("hikinglog-reader", _cfg(tmp_path), {}) == PRESETS["hikinglog-reader"]


def test_a_typed_flag_wins_and_the_rest_stays(tmp_path):
    got = presets_mod.resolve("hikinglog-reader", _cfg(tmp_path), {"effort": "high", "model": None})
    assert got == {**PRESETS["hikinglog-reader"], "effort": "high"}


def test_the_message_names_the_project(tmp_path):
    got = presets_mod.resolve("templated", _cfg(tmp_path), {"project": "C:/x/loom-os"})
    assert got["message"] == "Re-orient in loom-os."


def test_presets_come_from_pantheon_toml(tmp_path):
    toml = tmp_path / "pantheon.toml"
    toml.write_text('[presets.night]\nwho = "claude"\nmodel = "opus"\nnot_a_field = 1\n', encoding="utf-8")
    assert presets_mod.load(config_mod.load(toml)) == {"night": {"who": "claude", "model": "opus"}}


def test_the_shipped_toml_block_is_only_a_commented_template():
    shipped = config_mod.load(Path(config_mod.PROJECT_ROOT) / "pantheon.example.toml")
    assert presets_mod.load(shipped) == {}


# ---------------------------------------------------------------- acceptance 2


def test_open_with_a_preset_and_no_who_uses_the_presets_who(wired, capsys):
    cfg, providers, typed = wired
    rc = projects_mod.main(["open", "--preset", "hikinglog-reader"])
    assert rc == 0
    assert providers["claude"].calls == []
    (project_dir, _brief, interactive, options), = providers["codex"].calls
    assert project_dir.endswith("hiking_log_v2") and interactive is True     # codex-pc
    assert options == {"model": "sonnet", "effort": "medium", "mode": "acceptEdits"}
    assert typed == []            # Codex took its choices as flags; nothing typed into a window


def test_a_typed_who_beats_the_preset(wired):
    cfg, providers, typed = wired
    assert projects_mod.main(["open", "loom-os", "--preset", "hikinglog-reader", "--who", "claude"]) == 0
    assert providers["codex"].calls == [] and providers["claude"].calls[0][0].endswith("loom-os")


# ---------------------------------------------------------------- acceptance 3


def test_an_unknown_preset_names_the_file_and_launches_nothing(wired, capsys):
    cfg, providers, typed = wired
    rc = projects_mod.main(["open", "loom-os", "--preset", "nope"])
    out = capsys.readouterr().out
    assert rc != 0
    assert "nope" in out and "pantheon.toml" in out and "hikinglog-reader" in out
    assert providers["claude"].calls == [] and providers["codex"].calls == []


def test_no_project_and_a_preset_without_one_launches_nothing(wired, capsys):
    cfg, providers, typed = wired
    assert projects_mod.main(["open", "--preset", "templated"]) != 0
    assert "which project" in capsys.readouterr().out
    assert providers["claude"].calls == []


# ---------------------------------------------------------------- build B step 1


def test_open_puts_model_and_effort_on_the_claude_command_line(wired, capsys):
    """Nothing is typed after launch: a `/effort` typed while the first message was already
    running never reached the session. The picks go in as start flags instead."""
    cfg, providers, typed = wired
    rc = projects_mod.main(["open", "loom-os", "--model", "sonnet", "--effort", "high"])
    assert rc == 0
    assert typed == []
    assert providers["claude"].calls[0][3] == {"start_model": "sonnet", "start_effort": "high"}


def test_a_resumed_session_is_not_retyped(wired):
    cfg, providers, typed = wired
    assert projects_mod.main(["open", "loom-os", "--resume", "--model", "sonnet"]) == 0
    assert typed == []


def test_open_without_choices_types_nothing(wired):
    cfg, providers, typed = wired
    assert projects_mod.main(["open", "loom-os"]) == 0
    assert typed == []
