"""The project picker's data and the "New session" launch (no queue row)."""
from __future__ import annotations

import asyncio
import dataclasses
import functools
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from textual.app import App, ComposeResult
from textual.widgets import Button, Input, OptionList, TextArea, Tree

from pantheon import config as config_mod
from pantheon.dispatch import projects as projects_mod
from pantheon.models import Event, LaunchResult
from pantheon.widgets.modal import FolderBrowser, LaunchOptions, ProjectPicker, TextPrompt, _NERD_TREE_GLYPHS


def drives_the_screen(test):
    @functools.wraps(test)
    def wrapper(*args, **kwargs):
        return asyncio.run(test(*args, **kwargs))
    return wrapper


def iso(seconds_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_root(tmp_path: Path) -> Path:
    root = tmp_path / "Claude"
    for name, marker in (("loom-os", "CLAUDE.md"), ("Gazebo", ".git"), ("Plumb", "CLAUDE.md"), ("notes", None)):
        folder = root / name
        folder.mkdir(parents=True)
        if marker == ".git":
            (folder / ".git").mkdir()
        elif marker:
            (folder / marker).write_text("x", encoding="utf-8")
    (root / "_template").mkdir()
    (root / "_template" / "CLAUDE.md").write_text("x", encoding="utf-8")
    return root


def make_cfg(tmp_path: Path, root: Path):
    cfg = config_mod.Config(vault=str(tmp_path / "vault"), state_dir=str(tmp_path / "state"))
    cfg = dataclasses.replace(cfg, projects_root=str(root))
    config_mod.ensure_state_dirs(cfg)
    return cfg


def test_projects_are_the_marked_folders_under_the_root_plus_used_folders(tmp_path):
    root = make_root(tmp_path)
    cfg = make_cfg(tmp_path, root)
    elsewhere = tmp_path / "elsewhere" / "dune_tracker"
    elsewhere.mkdir(parents=True)
    (elsewhere / "CLAUDE.md").write_text("x", encoding="utf-8")
    junk = tmp_path / "Temp"            # a shell was opened here once; it is not a project
    junk.mkdir()
    events = [
        Event(ts=iso(60), event="SessionStart", source="claude", cwd=str(elsewhere)),
        Event(ts=iso(30), event="SessionStart", source="claude", cwd=str(junk)),
        Event(ts=iso(20), event="SessionStart", source="claude", cwd=str(root)),   # the root itself
    ]
    projects = projects_mod.list_projects(cfg, events)
    names = {p.name for p in projects}
    assert names == {"loom-os", "Gazebo", "Plumb", "dune_tracker"}   # no `notes`, `_template`, Temp, root
    outside = next(p for p in projects if p.name == "dune_tracker")
    assert outside.path == str(elsewhere).replace(chr(92), "/")      # the log's own spelling, never resolved


def test_recent_sort_puts_used_folders_first_newest_first(tmp_path):
    root = make_root(tmp_path)
    cfg = make_cfg(tmp_path, root)
    events = [
        Event(ts=iso(3600), event="SessionStart", source="claude", cwd=str(root / "Plumb")),
        Event(ts=iso(60), event="dispatch", source="pantheon", cwd=str(root / "Gazebo")),
    ]
    ordered = [p.name for p in projects_mod.sort_projects(projects_mod.list_projects(cfg, events), "recent")]
    assert ordered[:2] == ["Gazebo", "Plumb"]
    assert ordered[2] == "loom-os"
    by_name = [p.name for p in projects_mod.sort_projects(projects_mod.list_projects(cfg, events), "name")]
    assert by_name == ["Gazebo", "loom-os", "Plumb"]


def test_pinned_projects_are_listed_without_any_event_and_missing_ones_are_skipped(tmp_path):
    root = make_root(tmp_path)
    nested = root / "Apps" / "Condor-Buddy"          # nested under a container: the root scan misses it
    nested.mkdir(parents=True)
    cfg = dataclasses.replace(make_cfg(tmp_path, root),
                              pinned_projects=[str(nested), str(root / "Apps" / "gone"), str(root / "Plumb")])
    projects = projects_mod.list_projects(cfg)       # no events at all, as after the log rotates
    names = [p.name for p in projects]
    assert "Condor-Buddy" in names and "gone" not in names
    assert names.count("Plumb") == 1                 # pinned AND under the root: listed once
    events = [Event(ts=iso(60), event="SessionStart", source="claude", cwd=str(nested))]
    pinned = next(p for p in projects_mod.list_projects(cfg, events) if p.name == "Condor-Buddy")
    assert pinned.last_used is not None              # recency still comes from the log


def test_workspace_project_is_the_root_itself_with_its_own_recency(tmp_path):
    root = make_root(tmp_path)
    cfg = make_cfg(tmp_path, root)
    events = [Event(ts=iso(60), event="SessionStart", source="claude", cwd=str(root))]
    workspace = projects_mod.workspace_project(cfg, events)
    assert workspace.name == "Claude (workspace)"
    assert workspace.path == str(root).replace(chr(92), "/")
    assert workspace.last_used is not None
    assert projects_mod.workspace_project(cfg).last_used is None   # no events -> never


def test_filter_matches_any_case_and_every_word(tmp_path):
    root = make_root(tmp_path)
    cfg = make_cfg(tmp_path, root)
    projects = projects_mod.list_projects(cfg)
    assert [p.name for p in projects_mod.filter_projects(projects, "LOOM")] == ["loom-os"]
    assert [p.name for p in projects_mod.filter_projects(projects, "loom os")] == ["loom-os"]
    assert projects_mod.filter_projects(projects, "") == projects


class FakeProvider:
    name = "claude"

    def __init__(self, interactive_ok: bool = True) -> None:
        self.calls: list[tuple] = []
        self._caps = {"interactive_tmux"} if interactive_ok else set()

    def capabilities(self):
        return set(self._caps)

    def launch(self, project_dir, briefing_path, interactive):
        self.calls.append((project_dir, briefing_path, interactive))
        return LaunchResult(True, "tmux", 7, "loom-os", tmux_pane="%7", message="claude is working in window 7")


def _events(cfg) -> list[dict]:
    return [json.loads(line) for line in Path(cfg.events_file).read_text(encoding="utf-8").splitlines() if line.strip()]


def test_start_session_launches_without_a_briefing_and_logs_it(tmp_path):
    root = make_root(tmp_path)
    cfg = make_cfg(tmp_path, root)
    provider = FakeProvider()
    result = projects_mod.start_session(str(root / "loom-os"), cfg, provider)
    assert result.ok and result.window_index == 7
    assert provider.calls[0][1] == "" and provider.calls[0][2] is True   # no first message, interactive
    logged = _events(cfg)[-1]
    assert logged["event"] == "new_session" and logged["cwd"].endswith("loom-os")
    # `extra` fields are flattened into the record on write (events.append_event / Event.to_dict).
    assert logged["provider"] == "claude" and logged["window_index"] == 7


def test_start_session_types_the_first_message_when_given(tmp_path):
    root = make_root(tmp_path)
    cfg = make_cfg(tmp_path, root)
    provider = FakeProvider()
    projects_mod.start_session(str(root / "Gazebo"), cfg, provider, first_message="run the tests")
    path = Path(provider.calls[0][1])
    assert path.is_file() and path.read_text(encoding="utf-8").strip() == "run the tests"


def test_a_headless_job_without_a_message_is_refused(tmp_path):
    root = make_root(tmp_path)
    cfg = make_cfg(tmp_path, root)
    provider = FakeProvider(interactive_ok=False)
    result = projects_mod.start_session(str(root / "Gazebo"), cfg, provider, interactive=False)
    assert not result.ok and "first message" in result.message
    assert provider.calls == []


def test_a_missing_folder_is_refused(tmp_path):
    root = make_root(tmp_path)
    cfg = make_cfg(tmp_path, root)
    result = projects_mod.start_session(str(root / "gone"), cfg, FakeProvider())
    assert not result.ok and "not there" in result.message


class _PickerHarness(App):
    def __init__(self, projects, workspace=None, cfg=None, root=None) -> None:
        super().__init__()
        self.projects = projects
        self.workspace = workspace
        self.cfg = cfg
        self.root = root
        self.result = "unset"

    def compose(self) -> ComposeResult:
        yield from ()

    def on_mount(self) -> None:
        self.push_screen(
            ProjectPicker(self.projects, cfg=self.cfg, workspace=self.workspace, root=self.root),
            self._done,
        )

    def _done(self, value) -> None:
        self.result = value


def _projects(tmp_path):
    root = make_root(tmp_path)
    cfg = make_cfg(tmp_path, root)
    events = [Event(ts=iso(60), event="SessionStart", source="claude", cwd=str(root / "Plumb"))]
    return projects_mod.list_projects(cfg, events), root


@drives_the_screen
async def test_picker_lists_most_recent_first_and_enter_picks_it(tmp_path):
    projects, root = _projects(tmp_path)
    app = _PickerHarness(projects)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        options = app.screen.query_one(OptionList)
        assert options.option_count == 3
        assert "Plumb" in str(options.get_option_at_index(0).prompt)
        await pilot.press("enter")
        await pilot.pause()
    assert app.result.endswith("Plumb")


@drives_the_screen
async def test_picker_typing_filters_and_escape_cancels(tmp_path):
    projects, root = _projects(tmp_path)
    app = _PickerHarness(projects)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        assert isinstance(app.focused, Input)
        await pilot.press("g", "a", "z")
        await pilot.pause()
        options = app.screen.query_one(OptionList)
        assert options.option_count == 1 and "Gazebo" in str(options.get_option_at_index(0).prompt)
        await pilot.press("escape")
        await pilot.pause()
    assert app.result is None


@drives_the_screen
async def test_picker_sort_button_switches_to_a_to_z(tmp_path):
    projects, root = _projects(tmp_path)
    app = _PickerHarness(projects)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.click("#sort-name")
        await pilot.pause()
        options = app.screen.query_one(OptionList)
        assert "Gazebo" in str(options.get_option_at_index(0).prompt)
        await pilot.press("escape")


@drives_the_screen
async def test_picker_toggle_buttons_fit_on_phone_width(tmp_path):
    """Regression for the phone-width overflow: at 65 columns (the real phone terminal's width),
    the Tree/List/Recent/A-Z row's full "(Ctrl+X)"-suffixed labels pushed the A-Z button's region
    past `screen.size.width` -- clipped and unclickable (`pilot.click` would raise
    `textual.pilot.OutOfBounds`). Every button in the row must stay fully on screen instead."""
    projects, root = _projects(tmp_path)
    app = _PickerHarness(projects)
    async with app.run_test(size=(65, 26)) as pilot:
        await pilot.pause()
        screen_width = app.size.width
        for bid in ("#view-tree", "#view-list", "#sort-recent", "#sort-name"):
            button = app.screen.query_one(bid, Button)
            region = button.region
            assert region.x + region.width <= screen_width, (
                f"{bid} region {region} extends past screen width {screen_width}"
            )
        await pilot.click("#sort-name")   # would raise OutOfBounds before the fix
        await pilot.pause()
        await pilot.press("escape")


@drives_the_screen
async def test_picker_shows_workspace_root_first_and_sort_does_not_move_it(tmp_path):
    projects, root = _projects(tmp_path)
    cfg = make_cfg(tmp_path, root)
    workspace = projects_mod.workspace_project(cfg)
    app = _PickerHarness(projects, workspace=workspace)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        options = app.screen.query_one(OptionList)
        assert "Claude (workspace)" in str(options.get_option_at_index(0).prompt)
        await pilot.click("#sort-name")
        await pilot.pause()
        assert "Claude (workspace)" in str(options.get_option_at_index(0).prompt)
        app.screen.query_one("#filter", Input).focus()
        await pilot.press("g", "a", "z")
        await pilot.pause()
        # filtered out: it does not match "gaz"
        assert options.option_count == 1 and "Gazebo" in str(options.get_option_at_index(0).prompt)
        await pilot.press("escape")
        await pilot.pause()
    assert app.result is None


# ---------------------------------------------------------------- v2: tree view


def _project_leaf_name(node) -> str:
    return (node.data or {}).get("path", "").rsplit("/", 1)[-1]


@drives_the_screen
async def test_tree_view_lists_projects_and_expands_a_nested_marked_subfolder(tmp_path):
    root = make_root(tmp_path)
    nested = root / "Gazebo" / "pinecone_catalog"
    nested.mkdir(parents=True)
    (nested / ".git").mkdir()
    cfg = make_cfg(tmp_path, root)
    projects = projects_mod.list_projects(cfg)
    app = _PickerHarness(projects)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+t")
        await pilot.pause()
        assert app.screen.query_one("#tree", Tree).display is True
        assert app.screen.query_one("#projects", OptionList).display is False
        tree = app.screen.query_one("#tree", Tree)
        names = {_project_leaf_name(n) for n in tree.root.children}
        assert names == {"loom-os", "Gazebo", "Plumb"}
        guides_node = next(n for n in tree.root.children if _project_leaf_name(n) == "Gazebo")
        assert guides_node.allow_expand   # it holds a nested marked subfolder
        guides_node.expand()
        await pilot.pause()
        nested_names = {_project_leaf_name(n) for n in guides_node.children}
        assert nested_names == {"pinecone_catalog"}
        nested_node = next(iter(guides_node.children))
        assert (nested_node.data or {}).get("kind") == "project"
        await pilot.press("escape")
        await pilot.pause()
    assert app.result is None


@drives_the_screen
async def test_tree_shows_nesting_to_depth_three_and_a_deeper_container_collapses(tmp_path):
    """leftover, closed 2026-09-05: the old container test only peeked one level past a
    folder, so `Apps/Team/Widget` (a marked project two folders under an unmarked `Apps/`) never
    surfaced at all -- read as "the tree only shows one nested level" even though a *found*
    node's own expansion was already lazy and recursive. Now the container probe is bounded to
    `_MAX_TREE_DEPTH` (3) folder levels: `Widget` at depth 3 shows and expands the whole way down,
    while a marker four levels under `Apps/Team2/` is past the budget and `Team2` never appears at
    all -- "folders without CLAUDE.md/git markers collapse" (build brief), not a dead-end node."""
    root = make_root(tmp_path)
    (root / "Apps" / "Team" / "Widget").mkdir(parents=True)
    (root / "Apps" / "Team" / "Widget" / "CLAUDE.md").write_text("x", encoding="utf-8")
    (root / "Apps" / "Team2" / "Sub" / "Deep" / "TooFar").mkdir(parents=True)
    (root / "Apps" / "Team2" / "Sub" / "Deep" / "TooFar" / "CLAUDE.md").write_text(
        "x", encoding="utf-8"
    )
    cfg = make_cfg(tmp_path, root)
    projects = projects_mod.list_projects(cfg)
    app = _PickerHarness(projects, root=str(root))
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+t")
        await pilot.pause()
        tree = app.screen.query_one("#tree", Tree)
        apps_node = next(n for n in tree.root.children if _project_leaf_name(n) == "Apps")
        assert apps_node.allow_expand
        apps_node.expand()
        await pilot.pause()
        # `Team2`'s only marked project is four folders down -- past the depth-3 budget -- so it
        # never even shows as a container.
        assert not any(_project_leaf_name(n) == "Team2" for n in apps_node.children)
        team_node = next(n for n in apps_node.children if _project_leaf_name(n) == "Team")
        assert team_node.allow_expand
        team_node.expand()
        await pilot.pause()
        widget_node = next(iter(team_node.children))
        assert _project_leaf_name(widget_node) == "Widget"
        assert (widget_node.data or {}).get("kind") == "project"
        assert (widget_node.data or {}).get("depth") == 3
        assert not widget_node.allow_expand   # depth 3 is the budget's edge
        await pilot.press("escape")
        await pilot.pause()
    assert app.result is None


@drives_the_screen
async def test_tree_container_two_levels_deep_still_reaches_its_project(tmp_path):
    """Bug found running this brief: a container folder (no CLAUDE.md/git of its own) that itself
    sits inside another unmarked container -- `Apps/Team/SubTeam/Widget`, where only `Widget` is
    marked -- was included in the tree (the budget check correctly saw a project within reach) but
    then added as a dead LEAF once its own depth hit `_MAX_TREE_DEPTH`, because `_add_tree_node`
    re-applied the same depth cutoff to a CONTAINER's expand-ability that `_tree_children` had
    already used to decide the container belongs in the tree at all. The project below it became
    permanently unreachable -- no arrow, Enter/click did nothing. Only a *project* node's own
    further nesting is meant to stop at the depth budget."""
    root = make_root(tmp_path)
    (root / "Apps" / "Team" / "SubTeam" / "Widget").mkdir(parents=True)
    (root / "Apps" / "Team" / "SubTeam" / "Widget" / "CLAUDE.md").write_text("x", encoding="utf-8")
    cfg = make_cfg(tmp_path, root)
    projects = projects_mod.list_projects(cfg)
    app = _PickerHarness(projects, root=str(root))
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+t")
        await pilot.pause()
        tree = app.screen.query_one("#tree", Tree)
        apps_node = next(n for n in tree.root.children if _project_leaf_name(n) == "Apps")
        apps_node.expand()
        await pilot.pause()
        team_node = next(n for n in apps_node.children if _project_leaf_name(n) == "Team")
        team_node.expand()
        await pilot.pause()
        subteam_node = next(iter(team_node.children))
        assert _project_leaf_name(subteam_node) == "SubTeam"
        assert (subteam_node.data or {}).get("kind") == "container"
        assert subteam_node.allow_expand   # the bug: this used to be False, a dead end
        subteam_node.expand()
        await pilot.pause()
        widget_node = next(iter(subteam_node.children))
        assert _project_leaf_name(widget_node) == "Widget"
        assert (widget_node.data or {}).get("kind") == "project"
        await pilot.press("escape")
        await pilot.pause()
    assert app.result is None


@drives_the_screen
async def test_selection_survives_switching_from_list_to_tree_and_back(tmp_path):
    """Bug found running this brief: toggling Tree/List (or Recent/A-Z) always snapped the
    highlight back to the first row, even when the project you were just looking at was still on
    screen -- reads as "clunky" exactly the way the user described the picker. Highlighting a
    project, then round-tripping List -> Tree -> List, should land back on it each time."""
    projects, root = _projects(tmp_path)
    app = _PickerHarness(projects)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        options = app.screen.query_one("#projects", OptionList)
        # Plumb is row 0 (most recent); move down to something else first.
        names = [projects_mod.Project(p.name, p.path, p.last_used, p.modified) for p in projects]
        target = next(p for p in projects if p.name == "loom-os")
        target_index = next(i for i, o in enumerate(app.screen._shown) if o.path == target.path)
        options.highlighted = target_index
        await pilot.pause()
        await pilot.press("ctrl+t")
        await pilot.pause()
        tree = app.screen.query_one("#tree", Tree)
        current = tree.root.children[tree.cursor_line]
        assert _project_leaf_name(current) == "loom-os"
        await pilot.press("ctrl+l")
        await pilot.pause()
        options = app.screen.query_one("#projects", OptionList)
        assert options.highlighted is not None
        assert app.screen._shown[options.highlighted].path == target.path
        await pilot.press("escape")
        await pilot.pause()
    assert app.result is None


@drives_the_screen
async def test_tree_uses_nerd_glyphs_when_the_icons_setting_is_on(tmp_path):
    root = make_root(tmp_path)
    cfg = make_cfg(tmp_path, root)
    cfg = dataclasses.replace(cfg, appearance={"icons": True})
    projects = projects_mod.list_projects(cfg)
    app = _PickerHarness(projects, cfg=cfg)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+t")
        await pilot.pause()
        picker = app.screen
        assert picker._glyphs() == _NERD_TREE_GLYPHS
        assert app.screen.query_one("#tree", Tree).ICON_NODE == _NERD_TREE_GLYPHS["expand"]
        await pilot.press("escape")
        await pilot.pause()


@drives_the_screen
async def test_typing_filters_and_switches_from_tree_back_to_list(tmp_path):
    projects, root = _projects(tmp_path)
    app = _PickerHarness(projects)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+t")
        await pilot.pause()
        assert app.screen.query_one("#tree", Tree).display is True
        await pilot.press("g", "a", "z")
        await pilot.pause()
        # typing forces List
        assert app.screen.query_one("#tree", Tree).display is False
        options = app.screen.query_one("#projects", OptionList)
        assert options.display is True
        assert options.option_count == 1 and "Gazebo" in str(options.get_option_at_index(0).prompt)
        await pilot.press("backspace", "backspace", "backspace")
        await pilot.pause()
        # clearing it returns to the view that was open before the first keystroke
        assert app.screen.query_one("#tree", Tree).display is True
        assert app.screen.query_one("#projects", OptionList).display is False
        await pilot.press("escape")
        await pilot.pause()
    assert app.result is None


@drives_the_screen
async def test_ctrl_a_sorts_a_to_z(tmp_path):
    projects, root = _projects(tmp_path)
    app = _PickerHarness(projects)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+a")
        await pilot.pause()
        options = app.screen.query_one(OptionList)
        assert "Gazebo" in str(options.get_option_at_index(0).prompt)
        await pilot.press("escape")
        await pilot.pause()
    assert app.result is None


@drives_the_screen
async def test_add_folder_opens_a_directory_tree_not_a_typed_path_box(tmp_path):
    """S-UX pass: Ctrl+O now browses first (`FolderBrowser`); the old typed-path box is still one
    keystroke away (Ctrl+P) rather than gone."""
    projects, root = _projects(tmp_path)
    app = _PickerHarness(projects)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+o")
        await pilot.pause()
        assert len(app.screen_stack) == 3   # FolderBrowser on top of the picker on top of the harness
        assert isinstance(app.screen, FolderBrowser)
        await pilot.press("escape")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
    assert app.result is None


@drives_the_screen
async def test_add_folder_type_a_path_fallback_with_a_real_folder_returns_its_path(tmp_path):
    projects, root = _projects(tmp_path)
    target = tmp_path / "elsewhere2" / "some-folder"
    target.mkdir(parents=True)
    app = _PickerHarness(projects)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+o")
        await pilot.pause()
        await pilot.press("ctrl+g")
        await pilot.pause()
        assert len(app.screen_stack) == 4   # TextPrompt on top of FolderBrowser on top of the picker
        prompt_area = app.screen.query_one("#prompt-text", TextArea)
        prompt_area.text = str(target)
        await pilot.press("enter")
        await pilot.pause()
    assert app.result == str(target).replace("\\", "/")


@drives_the_screen
async def test_add_folder_type_a_path_fallback_with_a_missing_folder_shows_a_message(tmp_path):
    projects, root = _projects(tmp_path)
    missing = str(tmp_path / "nope-not-there")
    app = _PickerHarness(projects)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+o")
        await pilot.pause()
        await pilot.press("ctrl+g")
        await pilot.pause()
        prompt_area = app.screen.query_one("#prompt-text", TextArea)
        prompt_area.text = missing
        await pilot.press("enter")
        await pilot.pause()
        # back on the folder browser (TextPrompt popped itself): the message names the folder
        assert isinstance(app.screen, FolderBrowser)
        assert "not there" in app.screen.status_text
        await pilot.press("escape")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
    assert app.result is None


class _FolderBrowserHarness(App):
    def __init__(self, start: str) -> None:
        super().__init__()
        self.start = start
        self.result = "unset"

    def compose(self) -> ComposeResult:
        yield from ()

    def on_mount(self) -> None:
        self.push_screen(FolderBrowser(self.start), self._done)

    def _done(self, value) -> None:
        self.result = value


@drives_the_screen
async def test_folder_browser_only_shows_directories(tmp_path):
    target = tmp_path / "pick-me"
    target.mkdir()
    (tmp_path / "a-file.txt").write_text("x", encoding="utf-8")
    app = _FolderBrowserHarness(str(tmp_path))
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        tree = app.screen.query_one("#browser-tree")
        labels = {str(n.label) for n in tree.root.children}
        assert any("pick-me" in n for n in labels)
        assert not any("a-file" in n for n in labels)
        await pilot.press("escape")
        await pilot.pause()
    assert app.result is None


@drives_the_screen
async def test_folder_browser_enter_on_a_highlighted_folder_picks_it(tmp_path):
    target = tmp_path / "pick-me"
    target.mkdir()
    app = _FolderBrowserHarness(str(tmp_path))
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        tree = app.screen.query_one("#browser-tree")
        tree.cursor_line = 1   # line 0 is the start folder itself; 1 is its first child
        tree.focus()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause(0.2)
    assert app.result == str(target).replace("\\", "/")


@drives_the_screen
async def test_folder_browser_choose_button_picks_the_highlighted_folder(tmp_path):
    target = tmp_path / "pick-me"
    target.mkdir()
    app = _FolderBrowserHarness(str(tmp_path))
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        tree = app.screen.query_one("#browser-tree")
        tree.cursor_line = 1   # line 0 is the start folder itself; 1 is its first child
        await pilot.pause()
        await pilot.click("#choose")
        await pilot.pause()
    assert app.result == str(target).replace("\\", "/")


@drives_the_screen
async def test_folder_browser_lists_a_drive_letter_start_and_returns_the_drive_form(tmp_path):
    """2026-09-27: started at `C:/Users/...` (the projects root's parent), the tree stayed empty
    under MSYS2 Python because `DirectoryTree` resolved `C:/...` as a relative path."""
    import subprocess
    (tmp_path / "pick-me").mkdir()
    drive_form = subprocess.run(["cygpath", "-m", str(tmp_path)], capture_output=True,
                                text=True, check=True).stdout.strip()
    assert drive_form[1] == ":"
    app = _FolderBrowserHarness(drive_form)
    async with app.run_test(size=(100, 30)) as pilot:
        for _ in range(20):
            await pilot.pause(0.1)
            tree = app.screen.query_one("#browser-tree")
            if tree.root.children:
                break
        assert [str(n.label) for n in tree.root.children] == ["pick-me"]
        tree.cursor_line = 1
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause(0.2)
    assert app.result == drive_form + "/pick-me"


@drives_the_screen
async def test_folder_browser_click_opens_a_folder_instead_of_picking_it(tmp_path):
    (tmp_path / "outer" / "inner").mkdir(parents=True)
    app = _FolderBrowserHarness(str(tmp_path))
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.3)
        await pilot.click("#browser-tree", offset=(8, 1))
        await pilot.pause(0.3)
        assert isinstance(app.screen, FolderBrowser)
        tree = app.screen.query_one("#browser-tree")
        outer = tree.root.children[0]
        assert outer.is_expanded
        assert [str(n.label) for n in outer.children] == ["inner"]
    assert app.result == "unset"


@drives_the_screen
async def test_folder_browser_ctrl_p_falls_back_to_typing_a_path(tmp_path):
    target = tmp_path / "typed-folder"
    target.mkdir()
    app = _FolderBrowserHarness(str(tmp_path))
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+g")
        await pilot.pause()
        assert isinstance(app.screen, TextPrompt)
        area = app.screen.query_one("#prompt-text", TextArea)
        area.text = str(target)
        await pilot.press("enter")
        await pilot.pause()
    assert app.result == str(target).replace("\\", "/")


# ---------------------------------------------------------------- S-UX pass: quick actions


@drives_the_screen
async def test_most_recent_jumps_straight_to_the_last_used_project(tmp_path):
    """Quick action (Ctrl+E / the `Most recent` pill): Plumb is the most recently used project in
    `_projects` regardless of the current sort/filter/view -- picking it should not require
    navigating to it first."""
    projects, root = _projects(tmp_path)
    app = _PickerHarness(projects)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.press("a", "b", "c")   # filtered to nothing -- most-recent still finds Plumb
        await pilot.pause()
        await pilot.press("ctrl+e")
        await pilot.pause()
    assert app.result.endswith("Plumb")


@drives_the_screen
async def test_most_recent_button_does_the_same_as_the_key(tmp_path):
    projects, root = _projects(tmp_path)
    app = _PickerHarness(projects)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.click("#most-recent")
        await pilot.pause()
    assert app.result.endswith("Plumb")


@drives_the_screen
async def test_most_recent_with_nothing_used_yet_shows_a_message_and_stays_open(tmp_path):
    root = make_root(tmp_path)
    cfg = make_cfg(tmp_path, root)
    projects = projects_mod.list_projects(cfg)   # no events -> nothing has a last_used
    app = _PickerHarness(projects)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+e")
        await pilot.pause()
        assert "nothing used yet" in app.screen.status_text
        await pilot.press("escape")
        await pilot.pause()
    assert app.result is None


@drives_the_screen
async def test_most_recent_prefers_the_workspace_when_it_is_the_newest(tmp_path):
    root = make_root(tmp_path)
    cfg = make_cfg(tmp_path, root)
    events = [
        Event(ts=iso(3600), event="SessionStart", source="claude", cwd=str(root / "Plumb")),
        Event(ts=iso(30), event="SessionStart", source="claude", cwd=str(root)),   # newer, the root itself
    ]
    projects = projects_mod.list_projects(cfg, events)
    workspace = projects_mod.workspace_project(cfg, events)
    app = _PickerHarness(projects, workspace=workspace)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.press("ctrl+e")
        await pilot.pause()
    assert app.result == workspace.path


# ---------------------------------------------------------------- v2: launch options


class _LaunchOptionsHarness(App):
    def __init__(self) -> None:
        super().__init__()
        self.result = "unset"

    def compose(self) -> ComposeResult:
        yield from ()

    def on_mount(self) -> None:
        self.push_screen(
            LaunchOptions(
                ["default", "opus", "sonnet"],
                ["low", "medium", "high"],
                [("a", "auto"), ("d", "default"), ("x", "acceptEdits"), ("p", "plan")],
            ),
            self._done,
        )

    def _done(self, value) -> None:
        self.result = value


@drives_the_screen
async def test_launch_options_returns_the_chosen_dict():
    app = _LaunchOptionsHarness()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.press("down")            # model: default -> opus
        await pilot.press("enter")           # advances to the effort column
        await pilot.pause()
        await pilot.press("down", "down")    # effort: default -> low -> medium
        await pilot.press("enter")           # advances to the mode column
        await pilot.pause()
        await pilot.press("down")            # mode: default -> auto
        await pilot.press("enter")           # last column: confirms
        await pilot.pause()
    assert app.result == {"model": "opus", "effort": "medium", "mode": "auto"}


@drives_the_screen
async def test_launch_options_escape_returns_none():
    app = _LaunchOptionsHarness()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
    assert app.result is None


# ---------------------------------------------------------------- v2: `pantheon open`


def test_open_subcommand_with_fake_provider_launches_and_prints(tmp_path, monkeypatch, capsys):
    root = make_root(tmp_path)
    cfg = make_cfg(tmp_path, root)
    provider = FakeProvider()
    monkeypatch.setattr(projects_mod, "get_providers", lambda cfg: {"claude": provider})
    monkeypatch.setattr(config_mod, "load", lambda *a, **k: cfg)

    rc = projects_mod.main(["open", "loom-os"])

    captured = capsys.readouterr()
    assert rc == 0
    assert "window 7" in captured.out
    assert provider.calls and provider.calls[0][0].endswith("loom-os") and provider.calls[0][2] is True


def test_open_subcommand_with_a_message_for_a_headless_job(tmp_path, monkeypatch, capsys):
    root = make_root(tmp_path)
    cfg = make_cfg(tmp_path, root)
    provider = FakeProvider(interactive_ok=False)
    monkeypatch.setattr(projects_mod, "get_providers", lambda cfg: {"claude": provider})
    monkeypatch.setattr(config_mod, "load", lambda *a, **k: cfg)

    rc = projects_mod.main(["open", "Gazebo", "--who", "claude", "--message", "run the tests"])

    assert rc == 0
    assert provider.calls[0][1] and Path(provider.calls[0][1]).read_text(encoding="utf-8").strip() == "run the tests"


def test_open_subcommand_with_an_unknown_provider_fails(tmp_path, monkeypatch, capsys):
    root = make_root(tmp_path)
    cfg = make_cfg(tmp_path, root)
    monkeypatch.setattr(projects_mod, "get_providers", lambda cfg: {})
    monkeypatch.setattr(config_mod, "load", lambda *a, **k: cfg)

    rc = projects_mod.main(["open", "loom-os"])

    captured = capsys.readouterr()
    assert rc == 1
    assert "not switched on" in captured.out


def test_codex_job_flags_reach_codex_exec(tmp_path):
    """the launch-options screen\x27s model / effort / sandbox become codex exec flags."""
    from pantheon.dispatch import codex_job
    root = make_root(tmp_path)
    cfg = make_cfg(tmp_path, root)
    plain = codex_job.codex_argv(cfg, "C:/p")
    assert plain[-3:] == ["-C", "C:/p", "-"] and "-m" not in plain
    argv = codex_job.codex_argv(cfg, "C:/p", model="gpt-5.6", effort="high", sandbox="read-only")
    assert argv[argv.index("-m") + 1] == "gpt-5.6"
    assert "model_reasoning_effort=high" in argv
    assert argv[argv.index("-s") + 1] == "read-only"


def test_claude_resume_types_continue():
    """the who-pick\x27s \"resume the last session in that folder\" adds --continue."""
    from pantheon.providers.claude import claude_command
    assert claude_command("claude") == "claude"
    assert claude_command("claude", {"resume": True}) == "claude --continue"
    assert claude_command("claude", {"model": "opus"}) == "claude"
