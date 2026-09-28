"""The first-run wizard:
when `pantheon.toml` is missing, three plain questions with the detected defaults already typed
in, then it writes the file in the same shape as the real one and says where. Every test hands in
its own `defaults` (a `Config` pointing at a `tmp_path` folder standing in for the vault) and its
own `target` path, so the real project's `pantheon.toml` and the user's real vault are never at risk
-- the module docstring's promise ("the vault is NEVER touched") is what these tests are proving.
"""
from __future__ import annotations

from pathlib import Path

from pantheon import config as config_mod
from pantheon import wizard


def canned(answers: list[str]):
    """A `read_line` stand-in for a fake stdin: one queued answer per question, in order."""
    it = iter(answers)
    return lambda _prompt: next(it)


def _defaults(tmp_path: Path, vault_exists: bool = True) -> config_mod.Config:
    vault = tmp_path / "vault"
    if vault_exists:
        vault.mkdir()
    return config_mod.Config(
        vault=str(vault).replace("\\", "/"),
        projects_root=str(tmp_path / "code").replace("\\", "/"),
        tmux_session="pantheon",
    )


# ---------------------------------------------------------------- accepting every default


def test_yes_accepts_every_default_and_asks_nothing(tmp_path, capsys):
    defaults = _defaults(tmp_path)
    target = tmp_path / "pantheon.toml"

    def refuses_to_be_called(_prompt):
        raise AssertionError("--yes must never ask a question")

    written = wizard.run(target, accept_defaults=True, read_line=refuses_to_be_called, defaults=defaults)

    assert written == target and target.exists()
    cfg = config_mod.load(target)
    assert cfg.vault == defaults.vault
    assert cfg.projects_root == defaults.projects_root
    assert cfg.tmux_session == "pantheon"
    assert "using the suggested answers" in capsys.readouterr().out


# ---------------------------------------------------------------- the three questions


def test_pressing_enter_three_times_keeps_every_suggested_answer(tmp_path):
    defaults = _defaults(tmp_path)
    target = tmp_path / "pantheon.toml"
    wizard.run(target, read_line=canned(["", "", ""]), defaults=defaults)

    cfg = config_mod.load(target)
    assert cfg.vault == defaults.vault
    assert cfg.projects_root == defaults.projects_root
    assert cfg.tmux_session == defaults.tmux_session


def test_a_typed_answer_overrides_the_default(tmp_path):
    defaults = _defaults(tmp_path)
    target = tmp_path / "pantheon.toml"
    other_vault = tmp_path / "elsewhere"
    wizard.run(
        target,
        read_line=canned([str(other_vault), "", "my-session"]),
        defaults=defaults,
    )

    cfg = config_mod.load(target)
    assert cfg.vault == str(other_vault).replace("\\", "/")
    assert cfg.projects_root == defaults.projects_root      # the blank answer kept the default
    assert cfg.tmux_session == "my-session"


def test_a_backslash_path_is_normalized_so_the_toml_still_parses(tmp_path):
    """A typed Windows-style path would otherwise land inside a TOML string as an invalid escape
    (`\\D` is not a recognized escape) and refuse to load back."""
    defaults = _defaults(tmp_path)
    target = tmp_path / "pantheon.toml"
    backslashed = str(tmp_path).replace("/", "\\") + "\\typed\\vault"
    wizard.run(target, read_line=canned([backslashed, "", ""]), defaults=defaults)

    text = target.read_text(encoding="utf-8")
    assert "\\t" not in text and "\\v" not in text     # no stray backslash escapes
    cfg = config_mod.load(target)                      # would raise if the TOML were invalid
    assert cfg.vault.endswith("/typed/vault")


def test_each_question_shows_its_default_in_the_prompt(tmp_path):
    defaults = _defaults(tmp_path)
    target = tmp_path / "pantheon.toml"
    seen_prompts: list[str] = []

    def recording(prompt: str) -> str:
        seen_prompts.append(prompt)
        return ""

    wizard.run(target, read_line=recording, defaults=defaults)

    assert len(seen_prompts) == 3
    assert defaults.vault in seen_prompts[0]
    assert defaults.projects_root in seen_prompts[1]
    assert defaults.tmux_session in seen_prompts[2]


# ---------------------------------------------------------------- the vault check


def test_says_it_found_the_vault_when_the_default_folder_exists(tmp_path, capsys):
    defaults = _defaults(tmp_path, vault_exists=True)
    wizard.run(tmp_path / "pantheon.toml", read_line=canned(["", "", ""]), defaults=defaults)
    assert f"found your vault at {defaults.vault}" in capsys.readouterr().out


def test_says_it_did_not_find_the_vault_when_the_default_folder_is_missing(tmp_path, capsys):
    defaults = _defaults(tmp_path, vault_exists=False)
    wizard.run(tmp_path / "pantheon.toml", read_line=canned(["", "", ""]), defaults=defaults)
    out = capsys.readouterr().out
    assert "no folder at" in out and defaults.vault in out


def test_the_vault_folder_itself_is_never_touched(tmp_path):
    """The wizard only ever checks `is_dir()`; nothing under the vault is created, read, or
    written -- only `pantheon.toml` at `target` changes."""
    defaults = _defaults(tmp_path, vault_exists=True)
    before = sorted(Path(defaults.vault).iterdir())
    wizard.run(tmp_path / "pantheon.toml", accept_defaults=True, defaults=defaults)
    after = sorted(Path(defaults.vault).iterdir())
    assert before == after == []


# ---------------------------------------------------------------- the written file


def test_the_written_file_has_every_top_level_block(tmp_path):
    defaults = _defaults(tmp_path)
    target = tmp_path / "pantheon.toml"
    wizard.run(target, accept_defaults=True, defaults=defaults)
    text = target.read_text(encoding="utf-8")
    for block in ("[providers]", "[governor]", "[notify]", "[appearance]", "[standalone]", "[tools]"):
        assert block in text


def test_the_written_file_round_trips_through_config_load(tmp_path):
    defaults = _defaults(tmp_path)
    target = tmp_path / "pantheon.toml"
    wizard.run(target, accept_defaults=True, defaults=defaults)
    cfg = config_mod.load(target)
    # Everything not asked about comes straight from the dataclass defaults.
    assert cfg.governor_settings() == config_mod.Governor()
    assert cfg.notify_settings() == config_mod.Notify()
    assert cfg.appearance_settings() == config_mod.Appearance()
    assert cfg.standalone_settings() == config_mod.Standalone()


def test_run_prints_where_it_wrote_the_file(tmp_path, capsys):
    defaults = _defaults(tmp_path)
    target = tmp_path / "pantheon.toml"
    wizard.run(target, accept_defaults=True, defaults=defaults)
    assert f"saved your settings to {target}" in capsys.readouterr().out


def test_creates_missing_parent_directories(tmp_path):
    defaults = _defaults(tmp_path)
    target = tmp_path / "nested" / "folder" / "pantheon.toml"
    wizard.run(target, accept_defaults=True, defaults=defaults)
    assert target.exists()


# ---------------------------------------------------------------- main()/argparse


def test_main_yes_flag_writes_to_a_custom_target(tmp_path, monkeypatch):
    """Drives the module the way `bin/pantheon` does: `python -m pantheon.wizard --yes`. The real
    vault default is used here (nothing reads or writes it -- `is_dir()` only), so this only
    proves the CLI wiring, not the question logic (covered above with injected `defaults`)."""
    target = tmp_path / "pantheon.toml"
    rc = wizard.main(["--yes", "--target", str(target)])
    assert rc == 0
    assert target.exists()
    cfg = config_mod.load(target)
    assert cfg.vault == config_mod.Config().vault      # untouched, just read for the default


def test_main_without_yes_reads_from_a_fake_stdin(tmp_path, monkeypatch):
    target = tmp_path / "pantheon.toml"
    answers = iter(["", "", "phone-session"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))
    rc = wizard.main(["--target", str(target)])
    assert rc == 0
    cfg = config_mod.load(target)
    assert cfg.tmux_session == "phone-session"
