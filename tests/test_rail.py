"""The session sidebar, driven headlessly by Textual's own test pilot. No real tmux is touched: `tmuxctl.select_
window` is monkeypatched to record calls instead of shelling out, and `entries_source` is injected
so no test reads a real transcript file.
"""
from __future__ import annotations

import asyncio
import dataclasses

from textual.app import App, ComposeResult
from textual.widgets import ListView, Static

from pantheon import config as config_mod
from pantheon import tmuxctl
from pantheon.session_view import models as session_models
from pantheon.session_view.models import SessionEntry
from pantheon.supervisor.rail import MAX_NUMBERED, NO_AGENTS, SessionSidebar, SwitcherRail

CWD_A = "C:/Home/x/Documents/Projects/habit_notes"
CWD_B = "C:/Home/x/Documents/Projects/hiking_log_v2"
CWD_C = "C:/Home/x/Documents/Projects/loom-os"

LIVE_ENTRIES = [
    SessionEntry(session_id="aaa11111", group=session_models.LIVE, project="habit_notes", cwd=CWD_A,
                title="fix the launcher", provider="claude", status_text="blocked - permission",
                style="warning", needs_human=True, window_index=3, tmux_session="pantheon"),
    SessionEntry(session_id="bbb22222", group=session_models.LIVE, project="hiking_log_v2", cwd=CWD_B,
                title="ingest research", provider="claude", status_text="working",
                style=None, needs_human=False, window_index=4, tmux_session="pantheon"),
    SessionEntry(session_id="ccc33333", group=session_models.LIVE, project="loom-os", cwd=CWD_C,
                title="astra job", provider="codex", status_text="running",
                style=None, needs_human=False, window_index=5, tmux_session="pantheon"),
]

RECENT_ENTRIES = [
    SessionEntry(session_id="old11111", group=session_models.RECENT, project="habit_notes",
                cwd=CWD_A, title="earlier fix", provider="claude", modified_ts="2026-09-01T00:00:00.000Z"),
    SessionEntry(session_id="old22222", group=session_models.RECENT, project="habit_notes",
                cwd=CWD_A, title="even earlier", provider="claude", modified_ts="2026-08-01T00:00:00.000Z"),
    SessionEntry(session_id="old33333", group=session_models.RECENT, project="loom-os",
                cwd=CWD_C, title="old codex job", provider="codex", modified_ts="2026-08-15T00:00:00.000Z"),
]


def make_cfg(tmp_path) -> config_mod.Config:
    cfg = config_mod.Config(state_dir=str(tmp_path))
    return dataclasses.replace(cfg, projects_root="C:/Home/x/Documents/Projects")


class _SidebarApp(App):
    """Just enough app to mount and drive one `SessionSidebar`."""

    def __init__(self, cfg, rows=None, entries=None):
        super().__init__()
        rows = rows if rows is not None else []
        entries = entries if entries is not None else []
        self.sidebar = SessionSidebar(
            cfg, rows_source=lambda: rows, entries_source=lambda _rows: entries, id="rail",
        )

    def compose(self) -> ComposeResult:
        yield self.sidebar


def run_sidebar(tmp_path, entries=None, rows=None):
    app = _SidebarApp(make_cfg(tmp_path), rows=rows, entries=entries)
    captured = {}

    async def drive():
        async with app.run_test(size=(30, 24)) as pilot:
            await pilot.pause()
            captured["pilot"] = pilot
            captured["app"] = app
            captured["list_view"] = app.query_one("#sidebar-list", ListView)
            captured["status_text"] = str(app.query_one("#sidebar-status", Static).content)

    asyncio.run(drive())
    return captured


# ---------------------------------------------------------------- backward-compat alias


def test_switcher_rail_is_an_alias_for_session_sidebar():
    assert SwitcherRail is SessionSidebar


# ---------------------------------------------------------------- sections + ordering


def test_sidebar_shows_running_header_with_count(tmp_path):
    captured = run_sidebar(tmp_path, entries=list(LIVE_ENTRIES))
    assert captured["status_text"] == "3 running"


def test_no_sessions_says_so_in_the_status_line(tmp_path):
    captured = run_sidebar(tmp_path, entries=[])
    assert NO_AGENTS in captured["status_text"]


def test_sidebar_lists_live_entries_then_recent_grouped_by_project(tmp_path):
    captured = run_sidebar(tmp_path, entries=list(LIVE_ENTRIES) + list(RECENT_ENTRIES))
    sidebar = captured["app"].sidebar
    # `＋ New session`, then ONE heading per project with its running sessions and then its
    # finished ones nested under it.
    shape = [(s[0], s[1].session_id if s[0] == "session" else s[1] if len(s) > 1 else "")
             for s in sidebar._items]
    assert shape == [
        ("new", ""), ("edit", ""),
        ("project", "habit_notes"), ("session", "aaa11111"), ("session", "old11111"), ("session", "old22222"),
        ("project", "hiking_log_v2"), ("session", "bbb22222"),
        ("project", "loom-os"), ("session", "ccc33333"), ("session", "old33333"),
    ]


def test_a_project_heading_never_repeats(tmp_path):
    #: loom-os showed three times, habit_notes twice -- the old list grouped
    # finished conversations by runs in time order. Interleave them and check each appears once.
    mixed = [RECENT_ENTRIES[0], RECENT_ENTRIES[2], RECENT_ENTRIES[1]]
    captured = run_sidebar(tmp_path, entries=list(LIVE_ENTRIES) + mixed)
    headings = [s[1] for s in captured["app"].sidebar._items if s[0] == "project"]
    assert sorted(headings) == sorted(set(headings))


# ---------------------------------------------------------------- Selected / Activated messages


def test_moving_the_cursor_posts_selected_with_the_entry(tmp_path):
    received = []

    class _App(_SidebarApp):
        def on_session_sidebar_selected(self, event: SessionSidebar.Selected) -> None:
            received.append(event.entry.session_id)

    async def drive():
        app = _App(make_cfg(tmp_path), entries=list(LIVE_ENTRIES))
        async with app.run_test(size=(30, 24)) as pilot:
            await pilot.pause()
            list_view = app.query_one("#sidebar-list", ListView)
            list_view.focus()
            list_view.index = 5   # bbb22222 (after ＋ and ✎)
            await pilot.pause()

    asyncio.run(drive())
    assert "bbb22222" in received


def test_cursor_landing_on_a_section_label_skips_to_the_nearest_real_row(tmp_path):
    from pantheon.dispatch.projects import Project

    async def drive():
        app = _SidebarApp(make_cfg(tmp_path), entries=list(LIVE_ENTRIES))
        app.sidebar._projects_source = lambda: [Project("canvas", "C:/x/canvas", None)]
        async with app.run_test(size=(30, 24)) as pilot:
            await pilot.pause()
            list_view = app.query_one("#sidebar-list", ListView)
            list_view.focus()
            label = next(i for i, s in enumerate(app.sidebar._items) if s[0] == "label")
            list_view.index = label
            await pilot.pause()
            return app.sidebar._items[list_view.index]

    spec = asyncio.run(drive())
    assert spec[0] != "label"


def test_enter_posts_activated_with_the_entry(tmp_path):
    received = []

    class _App(_SidebarApp):
        def on_session_sidebar_activated(self, event: SessionSidebar.Activated) -> None:
            received.append(event.entry.session_id)

    async def drive():
        app = _App(make_cfg(tmp_path), entries=list(LIVE_ENTRIES))
        async with app.run_test(size=(30, 24)) as pilot:
            await pilot.pause()
            list_view = app.query_one("#sidebar-list", ListView)
            list_view.focus()
            await pilot.press("enter")
            await pilot.pause()

    asyncio.run(drive())
    assert received == ["aaa11111"]


# ---------------------------------------------------------------- jump: j / 1-9


def test_j_jumps_the_currently_selected_live_row(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(tmuxctl, "select_window", lambda session, index, tmux=None: calls.append((session, index)) or True)

    async def drive():
        app = _SidebarApp(make_cfg(tmp_path), entries=list(LIVE_ENTRIES))
        async with app.run_test(size=(30, 24)) as pilot:
            await pilot.pause()
            list_view = app.query_one("#sidebar-list", ListView)
            list_view.focus()
            list_view.index = 7  # ccc33333 -> window 5
            await pilot.press("j")
            await pilot.pause()

    asyncio.run(drive())
    assert calls == [("pantheon", 5)]


def test_a_number_key_chooses_that_running_session_like_a_click(tmp_path, monkeypatch):
    calls = []

    class _App(_SidebarApp):
        def on_session_sidebar_activated(self, event) -> None:
            calls.append(event.entry.window_index)

    async def drive():
        app = _App(make_cfg(tmp_path), entries=list(LIVE_ENTRIES))
        async with app.run_test(size=(30, 24)) as pilot:
            await pilot.pause()
            list_view = app.query_one("#sidebar-list", ListView)
            list_view.focus()
            await pilot.press("2")   # bbb22222 -> window 4
            await pilot.pause()

    asyncio.run(drive())
    assert calls == [4]


def test_j_on_a_recent_row_does_nothing_rather_than_crash(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(tmuxctl, "select_window", lambda *a, **k: calls.append(a) or True)

    async def drive():
        app = _SidebarApp(make_cfg(tmp_path), entries=list(LIVE_ENTRIES) + list(RECENT_ENTRIES))
        async with app.run_test(size=(30, 24)) as pilot:
            await pilot.pause()
            list_view = app.query_one("#sidebar-list", ListView)
            list_view.focus()
            recent_row_index = 4   # "old11111", under the habit_notes heading
            list_view.index = recent_row_index
            await pilot.pause()
            assert app.sidebar.selected().session_id == "old11111"
            await pilot.press("j")
            await pilot.pause()

    asyncio.run(drive())
    assert calls == []


def test_a_number_beyond_live_count_does_nothing(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(tmuxctl, "select_window", lambda *a, **k: calls.append(a) or True)

    async def drive():
        app = _SidebarApp(make_cfg(tmp_path), entries=list(LIVE_ENTRIES))
        async with app.run_test(size=(30, 24)) as pilot:
            await pilot.pause()
            list_view = app.query_one("#sidebar-list", ListView)
            list_view.focus()
            await pilot.press("9")   # only 3 live rows
            await pilot.pause()

    asyncio.run(drive())
    assert calls == []


# ---------------------------------------------------------------- one line per row (no wall of text)


def test_rows_are_built_no_wrap_with_an_ellipsis(tmp_path):
    # Regression: long titles wrapped the sidebar into an unreadable wall.
    sidebar = SessionSidebar(make_cfg(tmp_path))
    text = sidebar._line("Resume the diagnosis of the slowness of the Obsidian vault " * 4)
    assert text.no_wrap is True
    assert text.overflow == "ellipsis"


# ---------------------------------------------------------------- no per-tick blink


def test_an_unchanged_refresh_does_not_rebuild_the_list(tmp_path):
    # Regression: "the sidebar is constantly blinking" -- every tick used to
    # clear+refill the ListView. Identical content must reuse the same widgets (no clear = no flash).
    box = {"entries": list(LIVE_ENTRIES) + list(RECENT_ENTRIES)}

    async def drive():
        app = _SidebarApp(make_cfg(tmp_path), entries=None)
        app.sidebar._entries_source = lambda _rows: box["entries"]
        async with app.run_test(size=(30, 24)) as pilot:
            await pilot.pause()
            list_view = app.query_one("#sidebar-list", ListView)
            before = [id(w) for w in list_view.children]
            box["entries"] = list(LIVE_ENTRIES) + list(RECENT_ENTRIES)  # equal by value
            app.sidebar.refresh_rows()
            await pilot.pause()
            after = [id(w) for w in list_view.children]
            return before, after

    before, after = asyncio.run(drive())
    assert before and before == after


def test_a_changed_refresh_does_rebuild_the_list(tmp_path):
    box = {"entries": list(LIVE_ENTRIES)}

    async def drive():
        app = _SidebarApp(make_cfg(tmp_path), entries=None)
        app.sidebar._entries_source = lambda _rows: box["entries"]
        async with app.run_test(size=(30, 24)) as pilot:
            await pilot.pause()
            list_view = app.query_one("#sidebar-list", ListView)
            before = [id(w) for w in list_view.children]
            box["entries"] = list(LIVE_ENTRIES[:2])  # one live row fewer -> content changed
            app.sidebar.refresh_rows()
            await pilot.pause()
            after = [id(w) for w in list_view.children]
            return before, after

    before, after = asyncio.run(drive())
    assert before != after


# ---------------------------------------------------------------- refresh_rows keeps the cursor


def test_refresh_rows_keeps_the_highlighted_session_across_redraws(tmp_path):
    box = {"entries": list(LIVE_ENTRIES)}

    async def drive():
        app = _SidebarApp(make_cfg(tmp_path), entries=None)
        app.sidebar._entries_source = lambda _rows: box["entries"]
        async with app.run_test(size=(30, 24)) as pilot:
            await pilot.pause()
            list_view = app.query_one("#sidebar-list", ListView)
            list_view.focus()
            list_view.index = 7  # ccc33333
            await pilot.pause()
            # a redraw (same entries, new order-preserving list) should keep ccc33333 highlighted
            box["entries"] = list(LIVE_ENTRIES)
            app.sidebar.refresh_rows()
            await pilot.pause()
            return app.sidebar.selected()

    entry = asyncio.run(drive())
    assert entry is not None
    assert entry.session_id == "ccc33333"


def test_a_tenth_live_row_is_not_numbered_but_still_reachable(tmp_path, monkeypatch):
    entries = []
    for i in range(MAX_NUMBERED + 1):
        sid = f"s{i}"
        entries.append(SessionEntry(session_id=sid, group=session_models.LIVE, project=sid,
                                    cwd=f"C:/x/{sid}", title=sid, provider="claude",
                                    window_index=i + 10, tmux_session="pantheon"))
    calls = []
    monkeypatch.setattr(tmuxctl, "select_window", lambda session, index, tmux=None: calls.append((session, index)) or True)

    async def drive():
        app = _SidebarApp(make_cfg(tmp_path), entries=entries)
        async with app.run_test(size=(30, 30)) as pilot:
            await pilot.pause()
            list_view = app.query_one("#sidebar-list", ListView)
            list_view.focus()
            list_view.index = 2 * (MAX_NUMBERED + 1) + 1   # `＋` (0), `✎` (1), then heading + row per project
            await pilot.press("j")
            await pilot.pause()

    asyncio.run(drive())
    assert len(calls) == 1


# ---------------------------------------------------------------- the Orca-shaped extras


def _collect(tmp_path, entries, projects=None, press_at=None, staged=None):
    got = []

    class _App(_SidebarApp):
        def on_session_sidebar_new_session(self, event) -> None:
            got.append(("new",))

        def on_session_sidebar_project_chosen(self, event) -> None:
            got.append(("project", event.name, event.path))

        def on_session_sidebar_home(self, event) -> None:
            got.append(("home",))

        def on_session_sidebar_activated(self, event) -> None:
            got.append(("session", event.entry.session_id))

    async def drive():
        app = _App(make_cfg(tmp_path), entries=entries)
        if projects is not None:
            app.sidebar._projects_source = lambda: projects
        async with app.run_test(size=(40, 30)) as pilot:
            await pilot.pause()
            if staged:
                app.sidebar.set_stage(True, staged)
                await pilot.pause()
            list_view = app.query_one("#sidebar-list", ListView)
            list_view.focus()
            list_view.index = press_at(app.sidebar._items)
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            got.append(("items", list(app.sidebar._items)))

    asyncio.run(drive())
    return got


def test_new_session_line_posts_new_session(tmp_path):
    got = _collect(tmp_path, list(LIVE_ENTRIES), press_at=lambda items: 0)
    assert got[0] == ("new",)


def test_a_project_heading_posts_its_name_and_folder(tmp_path):
    got = _collect(tmp_path, list(LIVE_ENTRIES), press_at=lambda items: 2)
    assert got[0] == ("project", "habit_notes", CWD_A)


def test_projects_with_no_recent_conversation_are_listed_at_the_bottom(tmp_path):
    from pantheon.dispatch.projects import Project

    projects = [Project("canvas", "C:/x/canvas", None), Project("loom-os", CWD_C, None)]
    got = _collect(tmp_path, list(LIVE_ENTRIES), projects=projects,
                   press_at=lambda items: len(items) - 1)
    items = got[-1][1]
    label = [i for i, s in enumerate(items) if s[0] == "label"][0]
    assert [s[1] for s in items[label + 1:]] == ["canvas"]      # loom-os already has a heading
    assert got[0] == ("project", "canvas", "C:/x/canvas")


def test_more_line_opens_the_rest_of_a_projects_finished_conversations(tmp_path):
    many = [dataclasses.replace(RECENT_ENTRIES[0], session_id=f"old{i}", title=f"t{i}") for i in range(5)]
    got = _collect(tmp_path, many, press_at=lambda items: next(i for i, s in enumerate(items) if s[0] == "more"))
    items = got[-1][1]
    assert not any(s[0] == "more" for s in items)
    assert sum(1 for s in items if s[0] == "session") == 5


def test_staged_shows_the_back_line_and_marks_the_session_on_screen(tmp_path):
    got = _collect(tmp_path, list(LIVE_ENTRIES), staged="bbb22222", press_at=lambda items: 1)
    assert got[0] == ("home",)
    items = got[-1][1]
    assert items[1] == ("home",)


def test_a_session_row_shows_pushed_age_when_the_entry_has_one(tmp_path):
    from datetime import datetime, timedelta, timezone


    sidebar = SessionSidebar(make_cfg(tmp_path))
    entry = dataclasses.replace(LIVE_ENTRIES[1])
    object.__setattr__(entry, "last_push_at", (datetime.now(timezone.utc) - timedelta(minutes=4)).isoformat())
    text = sidebar._row_text(("session", entry, 2))
    assert "pushed 4m" in text.plain and "working" in text.plain


# ---------------------------------------------------------------- the editor line and `e`


def _edit_requests(tmp_path, steps, editor_open=False):
    got = []

    class _App(_SidebarApp):
        def on_session_sidebar_edit_file(self, event) -> None:
            got.append(event.folder)

    async def drive():
        app = _App(make_cfg(tmp_path), entries=list(LIVE_ENTRIES))
        async with app.run_test(size=(40, 30)) as pilot:
            await pilot.pause()
            if editor_open:
                app.sidebar.set_stage(False, editor_open=True)
                await pilot.pause()
            list_view = app.query_one("#sidebar-list", ListView)
            list_view.focus()
            for step in steps:
                if isinstance(step, int):
                    list_view.index = step
                else:
                    await pilot.press(step)
                await pilot.pause()
            got.append(("items", list(app.sidebar._items)))

    asyncio.run(drive())
    return got


def test_e_on_a_session_asks_for_the_editor_in_that_sessions_folder(tmp_path):
    got = _edit_requests(tmp_path, [5, "e"])        # bbb22222, in hiking_log_v2
    assert got[0] == CWD_B


def test_the_edit_line_remembers_the_session_the_cursor_just_left(tmp_path):
    got = _edit_requests(tmp_path, [7, 1, "enter"])  # ccc33333 (loom-os), then `✎ Edit a file`
    assert got[0] == CWD_C
    assert got[-1][1][1] == ("edit",)


def test_an_editor_alone_beside_the_list_gets_the_back_line_in_its_own_words(tmp_path):
    from pantheon.supervisor.rail import HOME_EDITOR_LABEL

    got = _edit_requests(tmp_path, [], editor_open=True)
    items = got[-1][1]
    assert items[:3] == [("new",), ("home",), ("edit",)]
    sidebar = SessionSidebar(make_cfg(tmp_path))
    sidebar.editor_open = True
    assert sidebar._row_text(("home",)).plain == HOME_EDITOR_LABEL
