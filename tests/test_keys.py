"""pantheon/keys.py: the plain-words key list `bin/pantheon --keys` prints, plus step 7's
`source_kind` -- the one line naming where the queue's rows come from, without touching disk
beyond what `pantheon.toml` already says (no Base read, no folder read)."""
from __future__ import annotations

from pantheon import config, keys


def test_source_kind_names_the_obsidian_base_by_its_file_stem():
    cfg = config.Config(base_file="Projects/Sprints.base")
    assert keys.source_kind(cfg) == "Obsidian Base: Sprints"


def test_source_kind_follows_a_different_base_file():
    cfg = config.Config(base_file="Projects/OtherProject.base")
    assert keys.source_kind(cfg) == "Obsidian Base: OtherProject"


def test_source_kind_names_the_standalone_folder_when_that_is_the_task_source():
    cfg = config.Config(task_source="standalone", standalone={"folder": "C:/Home/x/pantheon-tasks"})
    assert keys.source_kind(cfg) == "folder: pantheon-tasks"


def test_source_kind_on_the_real_config():
    assert keys.source_kind(config.load()) == "Obsidian Base: Sprints"


def test_keys_text_still_has_every_section():
    assert "deck (tmux window 0)" in keys.KEYS_TEXT
    assert "queue (tmux window 1)" in keys.KEYS_TEXT
    assert "hud (tmux window 2)" in keys.KEYS_TEXT
    assert "tools (pantheon --tools" in keys.KEYS_TEXT


def test_tools_section_is_reachable_by_name():
    assert keys.section("tools") == keys.TOOLS


def test_section_falls_back_to_the_whole_list_for_an_unknown_name():
    assert keys.section("nope") == keys.KEYS_TEXT
    assert keys.section("queue") == keys.QUEUE
