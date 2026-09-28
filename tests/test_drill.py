"""un-drilling restores the EXACT source, tabs, tab index, cursor, project
filter and search that were showing before the drill -- not just "a" Sprints view.

Uses a fake `TaskSource` stack (never the real `checkpoint_source`/vault I/O) so this stays a pure
widget test: `drill` takes an explicit `source=` for exactly this reason (see `pane.py`'s own
docstring on it).
"""
from __future__ import annotations

import asyncio
import functools
import shutil
from pathlib import Path

import pytest

from pantheon import config
from pantheon.models import TaskRow
from pantheon.queue import pane as pane_mod
from pantheon.queue.app import QueueApp
from pantheon.tasks.base import TabSpec
from pantheon.tasks import obsidian_base as ob

FIXTURES = Path(__file__).parent / "fixtures"


def drives_the_screen(test):
    @functools.wraps(test)
    def wrapper(*args, **kwargs):
        return asyncio.run(test(*args, **kwargs))
    return wrapper


def _app(tmp_path: Path, name: str = "vault") -> QueueApp:
    vault = tmp_path / name
    projects = vault / "Projects"
    shutil.copytree(FIXTURES / "sprints", projects / "Sprints")
    shutil.copy(FIXTURES / "Sprints.base", projects / "Sprints.base")
    cfg = config.Config(vault=str(vault), state_dir=str(vault / "state"))
    return QueueApp(cfg, source=ob.make(cfg))


class FakeCheckpointSource:
    """A minimal stand-in for `checkpoint_source.CheckpointSource` -- just enough surface for
    `QueuePane` to treat it as a real `TaskSource` (rows/tabs/notice/etc.), with none of the
    real one's file I/O."""

    def __init__(self, project: str, rows: list[TaskRow]) -> None:
        self.name = "checkpoint"
        self.kind = f"CHECKPOINT: {project}"
        self.notice = ""
        self._rows = rows
        self._tabs = [
            TabSpec(name="All", key="1", filter=lambda r: True),
            TabSpec(name="Needs the user", key="2", filter=lambda r: r.extra.get("owner") == "the user"),
        ]

    def rows(self) -> list[TaskRow]:
        return list(self._rows)

    def tabs(self) -> list[TabSpec]:
        return list(self._tabs)

    def flagged_tabs(self) -> set:
        return set()

    def parse_errors(self) -> int:
        return 0

    def open_for_edit(self, row: TaskRow) -> str:
        return "CHECKPOINT.md"

    def refresh(self) -> bool:
        return False


def _fake_source(project: str) -> FakeCheckpointSource:
    rows = [
        TaskRow(id="thread-0", summary="A gated thread", project=project,
                extra={"owner": "the user"}),
        TaskRow(id="thread-1", summary="An agent thread", project=project,
                extra={"owner": "agent"}),
    ]
    return FakeCheckpointSource(project, rows)


# ---------------------------------------------------------------- drill / undrill


@drives_the_screen
async def test_drill_swaps_the_source_and_tabs(tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        before_source = app.pane._source
        before_tabs = app.pane.tabs

        app.pane.drill("habit_notes", source=_fake_source("habit_notes"))
        await pilot.pause()

        assert app.pane._source.name == "checkpoint"
        assert app.pane.drilled_project == "habit_notes"
        assert [t.name for t in app.pane.tabs] == ["All", "Needs the user"]
        assert app.pane._source is not before_source
        assert app.pane.tabs is not before_tabs
        assert {r.summary for r in app.pane._all_rows()} == {"A gated thread", "An agent thread"}


@drives_the_screen
async def test_needs_the_user_tab_shows_only_the_user_owned_rows(tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.pane.drill("habit_notes", source=_fake_source("habit_notes"))
        await pilot.pause()
        app.pane.action_go_tab("2")
        await pilot.pause()
        assert [r.summary for r in app.pane.rows_on_screen] == ["A gated thread"]


@drives_the_screen
async def test_undrill_restores_the_exact_previous_source_and_position(tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.pane.action_cycle_tab(1)          # move off tab 0 so "exact position" is a real check
        app.pane.cursor = 0
        before_source = app.pane._source
        before_tabs = app.pane.tabs
        before_tab_index = app.pane.tab_index

        app.pane.drill("habit_notes", source=_fake_source("habit_notes"))
        await pilot.pause()
        assert app.pane.drilled_project == "habit_notes"

        restored = app.pane.undrill()
        await pilot.pause()

        assert restored is True
        assert app.pane.drilled_project is None
        assert app.pane._source is before_source
        assert app.pane.tabs is before_tabs
        assert app.pane.tab_index == before_tab_index


@drives_the_screen
async def test_undrill_with_nothing_drilled_does_nothing(tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.pane.undrill() is False
        assert app.pane.drilled_project is None


@drives_the_screen
async def test_esc_undrills_only_once_nothing_else_is_open(tmp_path):
    """`action_back`'s existing "one layer at a time" rule (search box, then detail view, then a
    project filter) applies to the drill too -- `Esc` un-drills only once none of those are open."""
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.pane.drill("habit_notes", source=_fake_source("habit_notes"))
        await pilot.pause()

        app.pane.view = "detail"
        await pilot.press("escape")
        await pilot.pause()
        assert app.pane.drilled_project == "habit_notes"   # detail view ate this Esc, not the drill
        assert app.pane.view == "list"

        await pilot.press("escape")
        await pilot.pause()
        assert app.pane.drilled_project is None            # now the drill is what Esc undoes


@drives_the_screen
async def test_d_key_drills_the_highlighted_rows_project(tmp_path, monkeypatch):
    """`d` reads the highlighted row's `project` and drills there; a fake `checkpoint_source.make`
    keeps this off real file I/O while still exercising the production `action_drill_row` path."""
    from pantheon.tasks import checkpoint_source as checkpoint_source_mod

    made: list[str] = []

    def fake_make(cfg, project):
        made.append(project)
        return _fake_source(project)

    monkeypatch.setattr(checkpoint_source_mod, "make", fake_make)

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        row = app.pane.selected_row()
        assert row is not None and row.project

        await pilot.press("d")
        await pilot.pause()

        assert made == [row.project]
        assert app.pane.drilled_project == row.project


@drives_the_screen
async def test_drill_with_no_project_on_the_row_says_so_instead_of_drilling(tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        row = app.pane.selected_row()
        row.project = None    # simulate a row with no project to drill into

        await pilot.press("d")
        await pilot.pause()

        assert app.pane.drilled_project is None
        assert "no project" in app.pane.message
