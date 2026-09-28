"""Slash-command completion source."""
from __future__ import annotations

from pantheon.notes import complete


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_command_file_stem_becomes_the_slash_name_with_frontmatter_description(tmp_path):
    home = tmp_path / ".claude"
    _write(home / "commands" / "checkpoint.md", "---\ndescription: save progress to CHECKPOINT.md\n---\nbody")
    commands = complete.list_commands(str(home))
    names = {c.name: c for c in commands}
    assert "/checkpoint" in names
    assert names["/checkpoint"].description == "save progress to CHECKPOINT.md"
    assert names["/checkpoint"].source == "command"


def test_command_file_with_no_frontmatter_has_an_empty_description(tmp_path):
    home = tmp_path / ".claude"
    _write(home / "commands" / "plain.md", "just body text, no frontmatter block")
    commands = complete.list_commands(str(home))
    names = {c.name: c for c in commands}
    assert names["/plain"].description == ""


def test_skill_directory_name_becomes_the_slash_name(tmp_path):
    home = tmp_path / ".claude"
    _write(home / "skills" / "grill-me" / "SKILL.md", "---\ndescription: interview before building\n---\n")
    commands = complete.list_commands(str(home))
    names = {c.name: c for c in commands}
    assert "/grill-me" in names
    assert names["/grill-me"].description == "interview before building"
    assert names["/grill-me"].source == "skill"


def test_extra_skills_dirs_are_swept_too(tmp_path):
    home = tmp_path / ".claude"
    codex_shared = tmp_path / ".agents" / "skills"
    _write(codex_shared / "outside" / "SKILL.md", "---\ndescription: check prior art\n---\n")
    commands = complete.list_commands(str(home), extra_skills_dirs=[str(codex_shared)])
    assert any(c.name == "/outside" for c in commands)


def test_default_skills_dirs_points_at_the_dot_agents_junction(tmp_path):
    home = tmp_path / ".claude"
    dirs = complete.default_skills_dirs(str(home))
    assert dirs == [str(tmp_path / ".agents" / "skills")]


def test_builtins_are_always_present(tmp_path):
    home = tmp_path / ".claude"   # nothing on disk at all
    commands = complete.list_commands(str(home))
    names = {c.name for c in commands}
    assert "/model" in names and "/checkpoint" in names


def test_a_command_file_wins_over_a_same_named_builtin(tmp_path):
    home = tmp_path / ".claude"
    _write(home / "commands" / "checkpoint.md", "---\ndescription: the real one\n---\n")
    commands = complete.list_commands(str(home))
    names = {c.name: c for c in commands}
    assert names["/checkpoint"].source == "command"
    assert names["/checkpoint"].description == "the real one"


def test_missing_claude_home_still_returns_the_builtins(tmp_path):
    commands = complete.list_commands(str(tmp_path / "does-not-exist"))
    assert any(c.name == "/model" for c in commands)


# ---------------------------------------------------------------- token_at


def test_token_at_line_start():
    assert complete.token_at("/che") == "/che"


def test_token_at_after_whitespace():
    assert complete.token_at("please run /che") == "/che"


def test_token_at_none_when_not_on_a_slash_token():
    assert complete.token_at("just typing words") is None
    assert complete.token_at("") is None


def test_token_at_stops_at_a_newline_even_given_the_whole_document():
    assert complete.token_at("first line\n/che") == "/che"


# ---------------------------------------------------------------- match / acceptance 4


def test_match_filters_by_prefix_case_insensitive_and_sorts(tmp_path):
    home = tmp_path / ".claude"
    _write(home / "commands" / "checkpoint.md", "---\ndescription: save progress\n---\n")
    _write(home / "commands" / "checklist.md", "---\ndescription: a list\n---\n")
    commands = complete.list_commands(str(home))
    matches = complete.match("/CHE", commands)
    assert [c.name for c in matches] == ["/checklist", "/checkpoint"]


def test_typing_slash_che_offers_checkpoint_with_its_description(tmp_path):
    """Acceptance 4: "Type /che at line start: the dropdown offers /checkpoint with its
    description" -- against a fixture commands/skills folder, end to end through this module."""
    home = tmp_path / ".claude"
    _write(home / "commands" / "checkpoint.md",
           "---\ndescription: save progress to CHECKPOINT.md\n---\nbody")
    commands = complete.list_commands(str(home))
    token = complete.token_at("/che")
    matches = complete.match(token, commands)
    assert any(c.name == "/checkpoint" and c.description == "save progress to CHECKPOINT.md"
              for c in matches)


def test_a_command_not_found_anywhere_matches_nothing():
    matches = complete.match("/totally-made-up", complete.list_commands("C:/does/not/exist"))
    assert matches == []


def test_bare_slash_matches_everything(tmp_path):
    home = tmp_path / ".claude"
    commands = complete.list_commands(str(home))
    assert complete.match("/", commands) == sorted(commands, key=lambda c: c.name)
