"""The scratchpad's store: tabs ARE files.

Every test works against a `tmp_path` folder, never the repo's real `state/` (project rule).
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from pantheon.notes import store


def test_list_tabs_is_empty_for_a_folder_that_does_not_exist_yet(tmp_path):
    assert store.list_tabs(tmp_path / "notes") == []


def test_create_writes_an_empty_file_and_returns_its_slug(tmp_path):
    d = tmp_path / "notes"
    slug = store.create(d, "Codex handoff notes")
    assert slug == "codex-handoff-notes"
    assert (d / "codex-handoff-notes.md").exists()
    assert store.read(d, slug) == ""


def test_create_collision_suffixes(tmp_path):
    d = tmp_path / "notes"
    a = store.create(d, "untitled")
    b = store.create(d, "untitled")
    c = store.create(d, "untitled")
    assert (a, b, c) == ("untitled", "untitled-2", "untitled-3")


def test_slugify_never_empty():
    assert store.slugify("") == "untitled"
    assert store.slugify("!!!") == "untitled"
    assert store.slugify("  Prompt Draft  ") == "prompt-draft"


def test_write_then_read_roundtrips_and_leaves_no_tmp_file(tmp_path):
    d = tmp_path / "notes"
    slug = store.create(d, "draft")
    store.write(d, slug, "line one\nline two")
    assert store.read(d, slug) == "line one\nline two"
    assert not (d / f"{slug}.md.tmp").exists()


def test_write_is_atomic_a_crash_mid_write_leaves_the_old_content(tmp_path, monkeypatch):
    d = tmp_path / "notes"
    slug = store.create(d, "draft")
    store.write(d, slug, "first version")

    def boom(*args, **kwargs):
        raise OSError("disk pulled")

    monkeypatch.setattr(store.os, "replace", boom)
    try:
        store.write(d, slug, "second version, never lands")
    except OSError:
        pass
    assert store.read(d, slug) == "first version"


def test_list_tabs_is_newest_first_by_mtime(tmp_path):
    d = tmp_path / "notes"
    store.write(d, "oldest", "a")
    time.sleep(0.01)
    store.write(d, "middle", "b")
    time.sleep(0.01)
    store.write(d, "newest", "c")
    assert store.list_tabs(d) == ["newest", "middle", "oldest"]


def test_rename_moves_the_file_and_returns_the_new_slug(tmp_path):
    d = tmp_path / "notes"
    slug = store.create(d, "untitled")
    store.write(d, slug, "keep me")
    new_slug = store.rename(d, slug, "Codex handoff")
    assert new_slug == "codex-handoff"
    assert not (d / f"{slug}.md").exists()
    assert store.read(d, new_slug) == "keep me"


def test_rename_to_the_same_slug_is_a_no_op(tmp_path):
    d = tmp_path / "notes"
    slug = store.create(d, "same name")
    assert store.rename(d, slug, "same name") == slug


def test_rename_collision_suffixes_against_other_tabs(tmp_path):
    d = tmp_path / "notes"
    store.create(d, "taken")
    slug = store.create(d, "other")
    new_slug = store.rename(d, slug, "taken")
    assert new_slug == "taken-2"


def test_delete_removes_the_file(tmp_path):
    d = tmp_path / "notes"
    slug = store.create(d, "gone soon")
    store.delete(d, slug)
    assert slug not in store.list_tabs(d)


def test_delete_of_a_missing_file_does_not_raise(tmp_path):
    store.delete(tmp_path / "notes", "never-existed")  # must not raise


def test_session_roundtrips_tabs_and_focus_never_content(tmp_path):
    d = tmp_path / "notes"
    store.write_session(d, ["b", "a"], "a")
    session = store.read_session(d)
    assert session == {"tabs": ["b", "a"], "focused": "a"}
    raw = json.loads((d / store.SESSION_FILE).read_text(encoding="utf-8"))
    assert "content" not in raw and set(raw) == {"tabs", "focused"}


def test_session_missing_file_is_empty(tmp_path):
    assert store.read_session(tmp_path / "notes") == {"tabs": [], "focused": None}


def test_session_corrupt_file_is_empty_not_fatal(tmp_path):
    d = tmp_path / "notes"
    d.mkdir(parents=True)
    (d / store.SESSION_FILE).write_text("{not json", encoding="utf-8")
    assert store.read_session(d) == {"tabs": [], "focused": None}


# ---------------------------------------------------------------- acceptance 1: restore after a
# hard kill (`tmux kill-window`) -- the fixture stands in for "the process died mid-session".


def test_restore_reopens_the_same_tabs_and_focus_as_a_hard_kill_left_them(tmp_path):
    d = tmp_path / "notes"
    store.write(d, "handoff", "draft one content")
    store.write(d, "scratch", "draft two content")
    store.write_session(d, ["scratch", "handoff"], "handoff")

    tabs, focused = store.restore(d)

    assert tabs == ["scratch", "handoff"]
    assert focused == "handoff"
    assert store.read(d, "handoff") == "draft one content"
    assert store.read(d, "scratch") == "draft two content"


def test_restore_drops_a_session_tab_whose_file_is_gone(tmp_path):
    d = tmp_path / "notes"
    store.write(d, "still-here", "x")
    store.write_session(d, ["still-here", "deleted-outside"], "deleted-outside")
    tabs, focused = store.restore(d)
    assert tabs == ["still-here"]
    assert focused == "still-here"       # falls back since the session's focus no longer exists


def test_restore_appends_a_file_the_session_never_recorded(tmp_path):
    """A crash mid-`create()` could in principle leave a `.md` file with no session entry --
    restore must not silently drop it."""
    d = tmp_path / "notes"
    store.write(d, "known", "a")
    store.write_session(d, ["known"], "known")
    store.write(d, "orphaned", "b")   # written after the session, as if a crash landed here
    tabs, focused = store.restore(d)
    assert set(tabs) == {"known", "orphaned"}
    assert focused == "known"


def test_restore_with_no_session_file_falls_back_to_list_tabs_order(tmp_path):
    d = tmp_path / "notes"
    store.write(d, "only-one", "x")
    tabs, focused = store.restore(Path(d))
    assert tabs == ["only-one"]
    assert focused == "only-one"


def test_restore_of_an_empty_folder_has_no_tabs_and_no_focus(tmp_path):
    tabs, focused = store.restore(tmp_path / "notes")
    assert tabs == []
    assert focused is None
