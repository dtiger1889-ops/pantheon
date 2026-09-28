"""`pantheon/tasks/source_note.py` (S-UI section 5.4, "Source note (O)") and the queue pane's `O`
binding that uses it. Every vault here is a `tmp_path` -- nothing under the real Obsidian vault is
ever touched."""
from __future__ import annotations

import asyncio
import functools
import shutil
from pathlib import Path

from pantheon import config
from pantheon.models import TaskRow
from pantheon.queue.app import QueueApp
from pantheon.tasks import obsidian_base as ob
from pantheon.tasks import source_note

FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------- resolve() / is_url()


def test_is_url_true_for_http_and_https():
    assert source_note.is_url("https://example.com/x")
    assert source_note.is_url("http://example.com/x")


def test_is_url_false_for_a_wikilink_or_plain_text():
    assert not source_note.is_url("[[Some note]]")
    assert not source_note.is_url("Some note")
    assert not source_note.is_url("")
    assert not source_note.is_url(None)


def test_resolve_finds_the_file_in_projects_by_exact_case_insensitive_name(tmp_path):
    note = tmp_path / "Projects" / "Hallway plans.md"
    note.parent.mkdir(parents=True)
    note.write_text("# plans", encoding="utf-8")
    found = source_note.resolve(tmp_path, "[[hallway PLANS]]")
    assert found == note


def test_resolve_falls_back_to_the_whole_vault_when_not_under_projects(tmp_path):
    note = tmp_path / "Archive" / "Old idea.md"
    note.parent.mkdir(parents=True)
    note.write_text("# idea", encoding="utf-8")
    found = source_note.resolve(tmp_path, "[[Old idea]]")
    assert found == note


def test_resolve_skips_trash_and_obsidian_folders(tmp_path):
    (tmp_path / ".trash").mkdir()
    (tmp_path / ".trash" / "Deleted note.md").write_text("gone", encoding="utf-8")
    (tmp_path / ".obsidian").mkdir()
    (tmp_path / ".obsidian" / "Deleted note.md").write_text("gone", encoding="utf-8")
    assert source_note.resolve(tmp_path, "[[Deleted note]]") is None


def test_resolve_prefers_projects_over_a_same_named_file_elsewhere(tmp_path):
    in_projects = tmp_path / "Projects" / "Dup.md"
    in_projects.parent.mkdir(parents=True)
    in_projects.write_text("projects copy", encoding="utf-8")
    elsewhere = tmp_path / "Other" / "Dup.md"
    elsewhere.parent.mkdir(parents=True)
    elsewhere.write_text("other copy", encoding="utf-8")
    assert source_note.resolve(tmp_path, "[[Dup]]") == in_projects


def test_resolve_returns_none_for_a_url():
    assert source_note.resolve(Path("C:/vault"), "https://example.com") is None


def test_resolve_returns_none_for_a_blank_source(tmp_path):
    assert source_note.resolve(tmp_path, None) is None
    assert source_note.resolve(tmp_path, "") is None


def test_resolve_returns_none_when_no_file_matches(tmp_path):
    (tmp_path / "Projects").mkdir()
    assert source_note.resolve(tmp_path, "[[Nothing here]]") is None


def test_resolve_handles_a_display_alias(tmp_path):
    """`[[Title|shown text]]` -- the alias names a location in Obsidian's own link picker, not a
    different file."""
    note = tmp_path / "Projects" / "Hallway plans.md"
    note.parent.mkdir(parents=True)
    note.write_text("# plans", encoding="utf-8")
    assert source_note.resolve(tmp_path, "[[Hallway plans|the plans]]") == note


def test_resolve_handles_a_heading_reference(tmp_path):
    """`[[Title#Heading]]` -- still the same file, just a heading inside it."""
    note = tmp_path / "Projects" / "Hallway plans.md"
    note.parent.mkdir(parents=True)
    note.write_text("# plans", encoding="utf-8")
    assert source_note.resolve(tmp_path, "[[Hallway plans#Measurements]]") == note


def test_resolve_handles_a_block_reference(tmp_path):
    """`[[Title#^block]]` -- a block reference, same rule as a heading."""
    note = tmp_path / "Projects" / "Hallway plans.md"
    note.parent.mkdir(parents=True)
    note.write_text("# plans", encoding="utf-8")
    assert source_note.resolve(tmp_path, "[[Hallway plans#^abc123]]") == note


def test_resolve_handles_a_heading_reference_with_a_display_alias(tmp_path):
    """`[[Title#Heading|shown text]]` -- both suffixes at once."""
    note = tmp_path / "Projects" / "Hallway plans.md"
    note.parent.mkdir(parents=True)
    note.write_text("# plans", encoding="utf-8")
    assert source_note.resolve(tmp_path, "[[Hallway plans#Measurements|the measurements]]") == note


# ---------------------------------------------------------------- the queue pane's `O` binding


def drives_the_screen(test):
    @functools.wraps(test)
    def wrapper(*args, **kwargs):
        return asyncio.run(test(*args, **kwargs))
    return wrapper


def _app(tmp_path: Path) -> QueueApp:
    projects = tmp_path / "Projects"
    shutil.copytree(FIXTURES / "sprints", projects / "Sprints")
    shutil.copy(FIXTURES / "Sprints.base", projects / "Sprints.base")
    cfg = config.Config(vault=str(tmp_path), state_dir=str(tmp_path / "state"))
    return QueueApp(cfg, source=ob.make(cfg))


def _select(app: QueueApp, row: TaskRow) -> None:
    app.pane.rows_on_screen = [row]
    app.pane.cursor = 0


@drives_the_screen
async def test_capital_o_opens_the_source_note_in_obsidian_at_desk_width(tmp_path, monkeypatch):
    from pantheon.queue import pane as pane_mod

    calls = []
    monkeypatch.setattr(pane_mod.subprocess, "run", lambda argv, **k: calls.append(argv))
    app = _app(tmp_path)
    note = tmp_path / "Projects" / "Hallway plans.md"
    note.write_text("# plans", encoding="utf-8")
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        _select(app, TaskRow(id="x", project="household", source="[[Hallway plans]]"))
        await pilot.press("O")
        await pilot.pause()
        assert "opened the source note" in app.pane.message
    assert len(calls) == 1
    uri = calls[0][-1]
    assert uri.startswith("obsidian://open?path=")
    assert "Hallway" in uri


@drives_the_screen
async def test_capital_o_opens_a_shell_window_at_phone_width(tmp_path, monkeypatch):
    from pantheon.queue import pane as pane_mod

    monkeypatch.delenv("TMUX", raising=False)
    app = _app(tmp_path)
    note = tmp_path / "Projects" / "Hallway plans.md"
    note.write_text("# plans", encoding="utf-8")
    # decision 5: the row toolbar (and so `_toolbar_collapsed`, which `O` now branches on
    # instead of a pane-level width constant) collapses under 80 columns, not 120 -- 65 is the
    # canonical phone width used elsewhere in this suite.
    async with app.run_test(size=(65, 26)) as pilot:
        await pilot.pause()
        _select(app, TaskRow(id="x", project="household", source="[[Hallway plans]]"))
        await pilot.press("O")
        await pilot.pause()
        assert "not inside tmux" in app.pane.message
        assert "Hallway plans.md" in app.pane.message


@drives_the_screen
async def test_capital_o_with_a_url_source_shows_the_url_and_opens_nothing(tmp_path, monkeypatch):
    from pantheon.queue import pane as pane_mod

    calls = []
    monkeypatch.setattr(pane_mod.subprocess, "run", lambda argv, **k: calls.append(argv))
    app = _app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        _select(app, TaskRow(id="x", project="household", source="https://example.com/plans"))
        await pilot.press("O")
        await pilot.pause()
        assert "https://example.com/plans" in app.pane.message
    assert calls == []


@drives_the_screen
async def test_capital_o_with_no_source_says_so(tmp_path):
    app = _app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        _select(app, TaskRow(id="x", project="household", source=None))
        await pilot.press("O")
        await pilot.pause()
        assert "no source note recorded" in app.pane.message


@drives_the_screen
async def test_capital_o_not_found_says_the_title(tmp_path):
    app = _app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        _select(app, TaskRow(id="x", project="household", source="[[Nothing here]]"))
        await pilot.press("O")
        await pilot.pause()
        assert 'source note not found for "Nothing here"' == app.pane.message


@drives_the_screen
async def test_capital_o_with_no_row_selected_says_so(tmp_path):
    app = _app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app.pane.rows_on_screen = []
        await pilot.press("O")
        await pilot.pause()
        assert "no task is highlighted" in app.pane.message


@drives_the_screen
async def test_detail_view_shows_the_resolved_path(tmp_path):
    app = _app(tmp_path)
    note = tmp_path / "Projects" / "Hallway plans.md"
    note.write_text("# plans", encoding="utf-8")
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        _select(app, TaskRow(id="x", project="household", source="[[Hallway plans]]"))
        app.pane.view = "detail"
        app.pane.redraw()
        await pilot.pause()
        from textual.widgets import Static
        text = str(app.query_one("#list", Static).content)
        assert "source note file:" in text
        assert str(note) in text


@drives_the_screen
async def test_detail_view_says_not_found_when_the_note_is_missing(tmp_path):
    app = _app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        _select(app, TaskRow(id="x", project="household", source="[[Nothing here]]"))
        app.pane.view = "detail"
        app.pane.redraw()
        await pilot.pause()
        from textual.widgets import Static
        text = str(app.query_one("#list", Static).content)
        assert "source note file:" in text and "not found" in text


def test_source_note_is_in_the_queue_toolbar_order_and_action_catalogue():
    from pantheon.queue.pane import QUEUE_ACTION_DEFS, QUEUE_TOOLBAR_ORDER

    assert "source_note" in QUEUE_TOOLBAR_ORDER
    assert QUEUE_ACTION_DEFS["source_note"].key == "O"
    assert QUEUE_ACTION_DEFS["source_note"].label == "Source note"
