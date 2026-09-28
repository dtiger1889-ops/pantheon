"""Drives the queue pane the way the user would, on a phone-sized screen and a desk-sized one."""
from __future__ import annotations

import asyncio
import functools
import shutil
from pathlib import Path

import pytest

from pantheon import config
from pantheon.models import TaskRow
from pantheon.queue import pane as pane_mod
from pantheon.queue.app import QueueApp, age_text, matches, shorten, wrap_summary
from pantheon.tasks import obsidian_base as ob
from pantheon.tasks import standalone as standalone_mod

FIXTURES = Path(__file__).parent / "fixtures"


def drives_the_screen(test):
    """Runs an async test without needing an extra pytest plugin installed."""
    @functools.wraps(test)
    def wrapper(*args, **kwargs):
        return asyncio.run(test(*args, **kwargs))
    return wrapper


def _app(tmp_path: Path, name: str = "vault") -> QueueApp:
    tmp_path = tmp_path / name
    projects = tmp_path / "Projects"
    shutil.copytree(FIXTURES / "sprints", projects / "Sprints")
    shutil.copy(FIXTURES / "Sprints.base", projects / "Sprints.base")
    cfg = config.Config(vault=str(tmp_path), state_dir=str(tmp_path / "state"))
    return QueueApp(cfg, source=ob.make(cfg))


def _standalone_app(tmp_path: Path, name: str = "tasks") -> QueueApp:
    """a QueueApp backed by the standalone source instead of Obsidian, for the
    open-for-edit tests below."""
    folder = tmp_path / name
    folder.mkdir(parents=True)
    for item in (FIXTURES / "standalone").iterdir():
        if item.is_file():
            shutil.copy(item, folder / item.name)
    cfg = config.Config(state_dir=str(tmp_path / "state"), standalone={"folder": str(folder)})
    return QueueApp(cfg, source=standalone_mod.make(cfg))


def _header(app: QueueApp) -> str:
    from textual.widgets import Static
    return str(app.query_one("#header", Static).content)


def _body(app: QueueApp) -> str:
    from textual.widgets import Static
    return str(app.query_one("#list", Static).content)


# ---------------------------------------------------------------- the two screen widths


@pytest.mark.parametrize("size", [(65, 26), (120, 40)])
@drives_the_screen
async def test_it_opens_and_counts_the_tabs_at_both_widths(tmp_path, size):
    """Below `NARROW` (66) the header keeps its phone prefix; at desk width the tab strip is lit
    pills instead and the `PANTHEON · queue` prefix is dropped -- the deck's own command bar
    already says where we are."""
    app = _app(tmp_path)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        header = _header(app)
        if size[0] < pane_mod.NARROW:
            assert "PANTHEON" in header and "queue" in header
        else:
            assert "PANTHEON" not in header
        assert "(every 5s)" in header                  # default refresh_seconds
        assert "Now 1" in header                       # one fixture is picked up
        body = _body(app)
        assert "Water the balcony plants" in body
        assert "Return the borrowed ladder" not in body   # finished work never shows


@drives_the_screen
async def test_refresh_seconds_zero_starts_no_timer_but_r_still_rereads(tmp_path, monkeypatch):
    """ `refresh_seconds = 0` means manual only: no
    timer ticks the task list, but pressing `r` still re-reads it."""
    calls = []
    monkeypatch.setattr(pane_mod.QueuePane, "set_interval", lambda self, *a, **k: calls.append(a))

    vault = tmp_path / "vault"
    projects = vault / "Projects"
    shutil.copytree(FIXTURES / "sprints", projects / "Sprints")
    shutil.copy(FIXTURES / "Sprints.base", projects / "Sprints.base")
    cfg = config.Config(vault=str(vault), state_dir=str(vault / "state"), refresh_seconds=0)
    app = QueueApp(cfg, source=ob.make(cfg))

    async with app.run_test() as pilot:
        await pilot.pause()
        assert calls == []                              # no timer was started
        assert "(manual: press r)" in _header(app)

        (projects / "Sprints" / "New task.md").write_text(
            '---\nsummary: "A brand new task"\nproject: workshop\ndone: false\nnext: true\n---\n',
            encoding="utf-8",
        )
        await pilot.press("r")
        await pilot.pause()
        assert "A brand new task" in _body(app)


@drives_the_screen
async def test_the_narrow_screen_drops_the_age_column_first(tmp_path):
    """Design rule R8: on a phone a column goes away rather than every column getting squeezed."""
    app = _app(tmp_path)
    async with app.run_test(size=(65, 26)) as pilot:
        await pilot.pause()
        summary_w, project_w, effort_w, show_age = app.pane._columns()
        assert show_age is False and project_w == 8
        assert max(len(line) for line in _body(app).splitlines()) <= 65

    app = _app(tmp_path, "wide")
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        summary_w, project_w, effort_w, show_age = app.pane._columns()
        assert show_age is True and project_w == 12
        assert max(len(line) for line in _body(app).splitlines()) <= 120


@drives_the_screen
async def test_the_summary_column_caps_at_a_readable_width_on_a_desk_monitor(tmp_path):
    """polish list item 5: at 200 columns the summary column used to stretch to ~165
    characters; project/effort/age must sit at a fixed, readable position instead."""
    app = _app(tmp_path, "desk")
    async with app.run_test(size=(200, 50)) as pilot:
        await pilot.pause()
        summary_w, project_w, effort_w, show_age = app.pane._columns()
        assert summary_w == pane_mod.QueuePane.SUMMARY_CAP
        assert show_age is True and project_w == 12
        # rows are `.rstrip()`-ed, so nothing stretches into the leftover width -- lines stay
        # far short of the full 200 columns even though the pane itself is that wide.
        body_lines = [l for l in _body(app).splitlines() if l.strip()]
        assert max(len(line) for line in body_lines) < 200


# ---------------------------------------------------------------- moving around


@drives_the_screen
async def test_pressing_2_switches_to_decide(tmp_path):
    app = _app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert app.pane.tab.name == "Now"
        await pilot.press("2")
        await pilot.pause()
        assert app.pane.tab.name == "Decide"
        assert "Decide" in _header(app)                  # desk header: a pill, not a `[bracket]`
        assert "Choose a shelving unit for the hallway" in _body(app)
        assert "Rebuild the photo archive index" not in _body(app)   # that one is Claude's


@drives_the_screen
async def test_tab_key_walks_forward_and_back(tmp_path):
    app = _app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("tab")
        await pilot.pause()
        assert app.pane.tab.name == "Decide"
        await pilot.press("shift+tab")
        await pilot.pause()
        assert app.pane.tab.name == "Now"


@drives_the_screen
async def test_the_plate_tab_shows_its_size_headings(tmp_path):
    app = _app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("4")
        await pilot.pause()
        body = _body(app)
        assert app.pane.tab.name == "Agent's plate"
        for heading in ("small", "medium", "large", "unsized"):
            assert f"-- {heading}" in body


@drives_the_screen
async def test_a_blocked_row_says_blocked_in_words(tmp_path):
    """Design rule R5: colour is never the only signal."""
    app = _app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("2")
        await pilot.pause()
        assert "blocked" in _body(app)


# ---------------------------------------------------------------- details, search, filter


@drives_the_screen
async def test_enter_opens_the_details_and_esc_closes_them(tmp_path):
    app = _app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("4")
        await pilot.pause()
        app.pane.cursor = 0
        app.pane.redraw()
        await pilot.press("enter")
        await pilot.pause()
        detail = _body(app)
        assert app.pane.view == "detail"
        assert "Routing" in detail                      # the assignment history
        assert "Workshop notes index" in detail         # source shown without the [[ ]]
        assert "[[" not in detail
        assert ".md" in detail                          # the file name
        await pilot.press("escape")
        await pilot.pause()
        assert app.pane.view == "list"


@drives_the_screen
async def test_search_finds_the_one_row_with_that_word_in_its_reply(tmp_path):
    app = _app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("7")                          # By project: every open row
        await pilot.press("slash")
        await pilot.pause()
        await pilot.press(*"marmaladefig")               # appears in exactly one reply
        await pilot.pause()
        assert len(app.pane.rows_on_screen) == 1
        assert app.pane.rows_on_screen[0].id == "Water the balcony plants"
        assert "1 found" in _header(app)
        await pilot.press("escape")
        await pilot.pause()
        assert app.pane.search_text == "" and len(app.pane.rows_on_screen) > 1


@drives_the_screen
async def test_p_cycles_through_the_projects(tmp_path):
    app = _app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("7")
        await pilot.pause()
        everything = len(app.pane.rows_on_screen)
        await pilot.press("p")
        await pilot.pause()
        first = app.pane.project_filter
        assert first in {"archive", "garden", "household", "workshop"}
        assert all(r.project == first for r in app.pane.rows_on_screen)
        assert len(app.pane.rows_on_screen) < everything
        assert f"project: {first}" in _header(app)
        await pilot.press("p")
        await pilot.pause()
        assert app.pane.project_filter != first


@drives_the_screen
async def test_question_mark_shows_the_key_list_in_plain_words(tmp_path):
    app = _app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("question_mark")
        await pilot.pause()
        text = _body(app)
        assert "jump to a tab" in text and "search the list" in text
        assert "Words on this screen" in text


@drives_the_screen
async def test_question_mark_names_the_source_kind_for_both_sources(tmp_path):
    """a stranger reading the `?` overlay knows where the rows come from."""
    app = _app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("question_mark")
        await pilot.pause()
        assert _body(app).startswith("source: Obsidian Base: Sprints")

    standalone_app = _standalone_app(tmp_path)
    async with standalone_app.run_test(size=(120, 40)) as pilot:
        await pilot.press("question_mark")
        await pilot.pause()
        assert _body(standalone_app).startswith("source: folder: tasks")          # every term is glossed


@drives_the_screen
async def test_a_missing_vault_says_so_and_the_app_stays_up(tmp_path):
    cfg = config.Config(vault=str(tmp_path / "not there"), state_dir=str(tmp_path / "state"))
    app = QueueApp(cfg, source=ob.make(cfg))
    app.pane.message = f"vault folder not found at {cfg.sprints_dir}; fix it in pantheon.toml"
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        from textual.widgets import Static
        assert "fix it in pantheon.toml" in str(app.query_one("#message", Static).content)
        assert app.is_running


@drives_the_screen
async def test_o_says_where_the_folder_is_when_there_is_no_tmux(tmp_path, monkeypatch):
    """Never attaches to a tmux session; with no tmux around it just names the folder. At a phone
    width `o` keeps this exact behaviour; at desk width `o` now
    opens the task's own note in Obsidian instead (see the next test)."""
    monkeypatch.delenv("TMUX", raising=False)
    app = _app(tmp_path)
    async with app.run_test(size=(65, 26)) as pilot:
        await pilot.press("o")
        await pilot.pause()
        assert "not inside tmux" in app.pane.message and "Sprints" in app.pane.message


@drives_the_screen
async def test_o_opens_the_tasks_own_note_in_obsidian_at_desk_width(tmp_path, monkeypatch):
    """at 120 columns and wider, `o` fires the `obsidian://` URI for the highlighted
    task's own file instead of opening a shell window."""
    from pantheon.queue import pane as pane_mod

    calls = []
    monkeypatch.setattr(pane_mod.subprocess, "run", lambda argv, **k: calls.append(argv))
    app = _app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        row = app.pane.selected_row()
        await pilot.press("o")
        await pilot.pause()
        assert f"asked Obsidian to open {row.id}" == app.pane.message
    assert len(calls) == 1
    uri = calls[0][-1]
    assert uri.startswith("obsidian://open?path=")
    assert row.id.replace(" ", "%20") in uri


# ---------------------------------------------------------------- the standalone
# source's own open-for-edit path (App.suspend + $EDITOR, never Obsidian)


@drives_the_screen
async def test_o_suspends_and_opens_the_editor_for_the_standalone_source(tmp_path, monkeypatch):
    """The standalone source has no Obsidian to hand off to, so `o` always suspends the deck into
    an editor -- at any width, unlike the Obsidian source's desk/phone split."""
    from contextlib import contextmanager

    from pantheon.queue import pane as pane_mod

    monkeypatch.delenv("EDITOR", raising=False)
    calls = []
    monkeypatch.setattr(pane_mod.subprocess, "run", lambda argv, **k: calls.append(argv))

    @contextmanager
    def fake_suspend():
        yield

    app = _standalone_app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        monkeypatch.setattr(app, "suspend", fake_suspend)
        row = app.pane.selected_row()
        await pilot.press("o")
        await pilot.pause()
        assert f"back from editing {row.id}" == app.pane.message
    assert calls == [["notepad", row.path]]


@drives_the_screen
async def test_o_uses_the_editor_env_var_when_set(tmp_path, monkeypatch):
    from contextlib import contextmanager

    from pantheon.queue import pane as pane_mod

    monkeypatch.setenv("EDITOR", "myeditor")
    calls = []
    monkeypatch.setattr(pane_mod.subprocess, "run", lambda argv, **k: calls.append(argv))

    @contextmanager
    def fake_suspend():
        yield

    app = _standalone_app(tmp_path)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        monkeypatch.setattr(app, "suspend", fake_suspend)
        await pilot.press("o")
        await pilot.pause()
    assert calls[0][0] == "myeditor"


@drives_the_screen
async def test_o_reports_when_this_screen_cannot_suspend(tmp_path):
    """Textual's headless test driver cannot suspend, so this exercises the real
    `SuspendNotSupported` path with no monkeypatching at all."""
    app = _standalone_app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        row = app.pane.selected_row()
        await pilot.press("o")
        await pilot.pause()
        assert "editor cannot open from here" in app.pane.message
        assert row.path in app.pane.message


@drives_the_screen
async def test_o_with_no_row_selected_says_so(tmp_path):
    app = _standalone_app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("6")   # Notes: everyone open, but force an empty tab to clear the cursor
        app.pane.rows_on_screen = []
        await pilot.press("o")
        await pilot.pause()
        assert "no file recorded" in app.pane.message


def test_toolbar_reads_open_in_editor_for_the_standalone_source():
    from pantheon.queue.pane import queue_toolbar_actions

    row = TaskRow(id="x", project="household")
    obsidian_labels = {a.action_name: a.label for a in queue_toolbar_actions(row, "obsidian_base")}
    standalone_labels = {a.action_name: a.label for a in queue_toolbar_actions(row, "standalone")}
    assert obsidian_labels["open_folder"] == "Open note"
    assert standalone_labels["open_folder"] == "Open in editor"


# ---------------------------------------------------------------- the small text helpers


def test_a_long_summary_wraps_to_two_lines_then_trails_off():
    lines = wrap_summary("one two three four five six seven eight nine ten eleven twelve", 20)
    assert len(lines) == 2
    assert all(len(line) <= 20 for line in lines)
    assert lines[-1].endswith("...")
    assert wrap_summary("short", 20) == ["short"]
    assert wrap_summary("", 20) == [""]


def test_age_is_days_then_weeks_then_months():
    from datetime import date
    today = date(2026, 9, 1)
    assert age_text("2026-08-30", today) == "2d"
    assert age_text("2026-08-01", today) == "4w"
    assert age_text("2026-01-01", today) == "8mo"
    assert age_text(None, today) == ""
    assert age_text("not a date", today) == ""


def test_search_looks_in_every_lane():
    row = TaskRow(id="a file", summary="a summary", project="proj", note="a note", reply="a reply")
    for needle in ("summary", "PROJ", "note", "reply", "file"):
        assert matches(row, needle)
    assert matches(row, "") is True
    assert matches(row, "nowhere") is False


def test_shorten_marks_where_it_cut():
    assert shorten("household", 8).endswith("…")
    assert shorten("garden", 8) == "garden"
    assert shorten(None, 8) == ""
