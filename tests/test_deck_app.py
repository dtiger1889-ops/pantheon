"""The combined desk view: three panels at desk width, one at phone width,
Tab moves between panels, and nothing is forked from the single-window apps."""
from __future__ import annotations

import asyncio
import functools
import json
import shutil
from pathlib import Path

from textual.widgets import Static

from pantheon import config as config_mod
from pantheon.deck.app import DeckApp
from pantheon.models import TmuxWindow, utcnow_iso
from pantheon.tasks import obsidian_base as ob

FIXTURES = Path(__file__).parent / "fixtures"
CWD = "C:/Home/x/Documents/Projects/hiking_log_v2"


def drives_the_screen(test):
    @functools.wraps(test)
    def wrapper(*args, **kwargs):
        return asyncio.run(test(*args, **kwargs))
    return wrapper


def _cfg(tmp_path: Path) -> config_mod.Config:
    projects = tmp_path / "Projects"
    shutil.copytree(FIXTURES / "sprints", projects / "Sprints")
    shutil.copy(FIXTURES / "Sprints.base", projects / "Sprints.base")
    # point the tool-inventory reader at fixture-shaped (here: simply absent) paths under
    # tmp_path -- the hard rule is that no test may assert on or read the real ~/.claude or
    # ~/.codex contents, so this deck-level test must not fall through to the real ones.
    tools_page = {
        "claude_skills_dir": str(tmp_path / "tools" / "claude_skills"),
        "agents_skills_dir": str(tmp_path / "tools" / "agents_skills"),
        "claude_settings": str(tmp_path / "tools" / "settings.json"),
        "codex_config": str(tmp_path / "tools" / "config.toml"),
        "codex_hooks": str(tmp_path / "tools" / "hooks.json"),
    }
    cfg = config_mod.Config(vault=str(tmp_path), state_dir=str(tmp_path / "state"),
                            projects_root="C:/Home/x/Documents/Projects",
                            tools_page=tools_page)
    cfg.events_file.parent.mkdir(parents=True, exist_ok=True)
    with open(cfg.events_file, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": utcnow_iso(), "source": "claude", "event": "Stop",
                             "session_id": "abc12345deadbeef", "cwd": CWD, "tmux_pane": "%3"}) + "\n")
    picture = json.loads((FIXTURES / "hud" / "hud.json").read_text(encoding="utf-8"))
    picture["fetched_at"] = utcnow_iso()
    cfg.hud_file.write_text(json.dumps(picture), encoding="utf-8")
    return cfg


def _app(tmp_path: Path) -> DeckApp:
    cfg = _cfg(tmp_path)
    windows = [TmuxWindow(3, "hiking_log_v2", "node", CWD, "%3", "pantheon")]
    app = DeckApp(cfg, window_source=lambda: windows, source=ob.make(cfg))
    return app


@drives_the_screen
async def test_desk_is_the_sidebar_beside_the_columns_table(tmp_path):
    """home layout: BUDGET on top, SESSIONS left, THE PIT's columns table right; the task
    list waits behind `t`. The conversations wall is gone."""
    app = _app(tmp_path)
    async with app.run_test(size=(180, 45)) as pilot:
        await pilot.pause()
        assert app.rail.display and app.sup.display and app.strip.display
        assert app.queue.display is False
        assert app.stage_usage.display is False
        assert not hasattr(app, "grid")
        header = str(app.query_one("#deck-header", Static).content)
        assert "PANTHEON" in header and "1 need you" in header
        assert app.sup.border_title.startswith("THE PIT")
        assert app.strip.border_title == "BUDGET"
        assert app.rail.border_title == "SESSIONS"
        assert app.rail.outer_size.width == 34
        await pilot.press("f")     # the old conversations key does nothing now
        await pilot.pause()
        assert app.sup.display is True


@drives_the_screen
async def test_t_shows_and_hides_the_task_list(tmp_path):
    app = _app(tmp_path)
    async with app.run_test(size=(180, 45)) as pilot:
        await pilot.pause()
        await pilot.press("t")
        await pilot.pause()
        assert app.queue.display is True and app.sup.display is True
        assert app.queue.size.width >= 54
        await pilot.press("t")
        await pilot.pause()
        assert app.queue.display is False


@drives_the_screen
async def test_phone_width_keeps_only_the_agents_panel(tmp_path):
    app = _app(tmp_path)
    async with app.run_test(size=(65, 26)) as pilot:
        await pilot.pause()
        assert app.sup.display is True
        assert app.queue.display is False and app.strip.display is False
        # The sidebar is a desk-width addition -- the supervisor pane already IS the switcher on
        # the phone (one column, one window). The phone tree is exactly what it always was.
        assert app.rail.display is False
        assert app.stage_usage.display is False
        status = str(app.query_one("#deck-status", Static).content)
        assert "F2 = task list" in status and "F3 = budget" in status
        # The phone tier must ADVERTISE session-starting -: the flow worked at
        # 65 columns all along but nothing on the phone said `n` existed, so it read as missing.
        assert "n = new session" in status
        assert app.is_running


@drives_the_screen
async def test_tab_walks_the_panels_left_to_right(tmp_path):
    """Panels are cycled as they are drawn: SESSIONS -> the agents table -> QUEUE (once `t` has
    opened it) -> back. The queue's own keys still work once it has the focus."""
    app = _app(tmp_path)
    async with app.run_test(size=(180, 45)) as pilot:
        await pilot.pause()
        await pilot.press("t")
        await pilot.pause()
        app.sup.focus_table()
        await pilot.pause()
        await pilot.press("tab")
        await pilot.pause()
        assert app.focused is not None and app.focused in list(app.queue.walk_children(with_self=True))
        await pilot.press("2")
        await pilot.pause()
        assert app.queue.tab.name == "Decide"
        await pilot.press("tab")
        await pilot.pause()
        assert app.focused in list(app.rail.walk_children(with_self=True))
        await pilot.press("tab")
        await pilot.pause()
        assert app.focused in list(app.sup.walk_children(with_self=True))


@drives_the_screen
async def test_command_bar_and_needs_you_card_agree(tmp_path):
    """: the top line said "1 need you" while the NEEDS YOU card said "nothing
    needs you" -- because the card's `HudStrip` was built with no `agents_source` at all
    (`hud/strip.py`'s own docstring already named the missing contract). Both must read the same
    folded rows (`self.sup.rows`): the pill from `_status_tally`, the card from `agents_source`."""
    from pantheon.hud import app as hud_app_mod
    from tests.test_hud_strip import widget_text

    app = _app(tmp_path)
    async with app.run_test(size=(200, 45)) as pilot:
        await pilot.pause()
        header = str(app.query_one("#deck-header", Static).content)
        assert "1 need you" in header
        card = app.strip.query_one(hud_app_mod.NeedsYouCard)
        card_text = widget_text(card.query_one(Static))
        assert "nothing needs you" not in card_text
        assert "hiking_log_v2" in card_text   # the same row the pill counted


@drives_the_screen
async def test_header_refresh_repaints_the_needs_you_card(tmp_path):
    """polish list item 3: the deck's own 5-second header refresh keeps the strip's NEEDS
    YOU card current instead of waiting up to 30s for the strip's own file-read recompose."""
    app = _app(tmp_path)
    async with app.run_test(size=(180, 45)) as pilot:
        await pilot.pause()
        calls = {"n": 0}
        original = app.strip.refresh_needs_you

        def _spy():
            calls["n"] += 1
            return original()

        app.strip.refresh_needs_you = _spy
        app._refresh_header()
        assert calls["n"] == 1


@drives_the_screen
async def test_header_and_status_clock_respect_the_twelve_hour_setting(tmp_path):
    """polish list item 1, and item 6 (comes free from item 1): the command bar's own
    clock and the status row's `vault read` / `usage read` times all use `models.format_clock`,
    so `[appearance] clock = "12h"` changes every one of them the same way."""
    cfg = _cfg(tmp_path)
    cfg.appearance["clock"] = "12h"   # Config is frozen; `appearance` is a plain mutable dict field
    windows = [TmuxWindow(3, "hiking_log_v2", "node", CWD, "%3", "pantheon")]
    app = DeckApp(cfg, window_source=lambda: windows, source=ob.make(cfg))
    async with app.run_test(size=(180, 45)) as pilot:
        await pilot.pause()
        app._refresh_header()
        header = str(app.query_one("#deck-header", Static).content)
        status_right = str(app.query_one("#deck-status-right", Static).content)
        assert "am" in header or "pm" in header
        assert ("am" in status_right or "pm" in status_right)
        assert "vault read" in status_right and "usage read" in status_right


@drives_the_screen
async def test_question_mark_overlay_glosses_the_panel_names(tmp_path):
    app = _app(tmp_path)
    async with app.run_test(size=(180, 45)) as pilot:
        await pilot.press("question_mark")
        await pilot.pause()
        from textual.containers import VerticalScroll
        panel = app.query_one("#deck-keys", VerticalScroll)
        assert panel.display is True
        text = str(app.query_one("#deck-keys-body", Static).content)
        assert "THE PIT" in text and "QUEUE" in text and "BUDGET" in text
        assert "work this task with claude" in text
        await pilot.press("q")
        await pilot.pause()
        assert panel.display is False and app.is_running


@drives_the_screen
async def test_question_mark_cycles_keys_then_connectors_then_tools_then_closes(tmp_path):
    """step 6's second page, and third, built for the combined deck too: a four-press
    cycle -- keys, connectors, tools, closed -- not a plain toggle (mirrors the standalone
    supervisor's own overlay)."""
    from textual.containers import VerticalScroll

    app = _app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        panel = app.query_one("#deck-keys", VerticalScroll)
        assert panel.display is False
        await pilot.press("question_mark")
        await pilot.pause()
        assert panel.display is True
        page1_text = str(app.query_one("#deck-keys-body", Static).content)
        assert "jump to that agent's window" in page1_text
        await pilot.press("question_mark")
        await pilot.pause()
        assert panel.display is True
        page2_text = str(app.query_one("#deck-keys-body", Static).content)
        assert "MCP servers the CLI sees" in page2_text
        assert "claude.ai connectors" in page2_text
        await pilot.press("question_mark")
        await pilot.pause()
        assert panel.display is True
        page3_text = str(app.query_one("#deck-keys-body", Static).content)
        assert "SKILLS" in page3_text and "HOOKS" in page3_text and "GUARDS" in page3_text
        assert "no skills found" in page3_text  # the fixture path in _cfg() has none
        await pilot.press("question_mark")
        await pilot.pause()
        assert panel.display is False
        assert app.is_running


@drives_the_screen
async def test_connectors_word_at_the_top_of_page_one_jumps_to_page_two(tmp_path):
    """The `[connectors]` clickable word (top of page 0) reaches the same page a second `?`
    press does, without needing that second key press."""
    from textual.containers import VerticalScroll

    app = _app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("question_mark")
        await pilot.pause()
        nav_text = str(app.query_one("#deck-keys-nav", Static).content)
        assert "[connectors]" in nav_text and "[tools]" in nav_text
        await pilot.click("#deck-keys-nav")
        await pilot.pause()
        panel = app.query_one("#deck-keys", VerticalScroll)
        assert panel.display is True
        text = str(app.query_one("#deck-keys-body", Static).content)
        assert "MCP servers the CLI sees" in text


@drives_the_screen
async def test_tools_action_jumps_straight_to_page_three(tmp_path):
    """`action_show_tools` (the `[tools]` clickable word): reaches page 2 without three
    `?` presses, the same shortcut `action_show_connectors` gives page 1."""
    app = _app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("question_mark")
        await pilot.pause()
        app.action_show_tools()
        await pilot.pause()
        from textual.containers import VerticalScroll

        panel = app.query_one("#deck-keys", VerticalScroll)
        assert panel.display is True
        text = str(app.query_one("#deck-keys-body", Static).content)
        assert "SKILLS" in text and "HOOKS" in text and "GUARDS" in text


@drives_the_screen
async def test_escape_closes_the_keys_overlay_from_either_page(tmp_path):
    from textual.containers import VerticalScroll

    app = _app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("question_mark")
        await pilot.press("question_mark")   # now on the connectors page
        await pilot.pause()
        panel = app.query_one("#deck-keys", VerticalScroll)
        assert panel.display is True
        await pilot.press("escape")
        await pilot.pause()
        assert panel.display is False
        assert app.is_running
        assert app.query_one("#panes").display is True   # #panes is back and focusable
        assert app.sup.display is True


# ---------------------------------------------------------------- the pit toggle


@drives_the_screen
async def test_shift_p_enters_the_pit_and_jumps_there(tmp_path, monkeypatch):
    from pantheon import tmuxctl
    from pantheon.deck import app as deck_app_mod

    calls = {"select": []}
    monkeypatch.setattr(deck_app_mod.tmux_pit, "is_active", lambda cfg: False)
    monkeypatch.setattr(deck_app_mod.tmux_pit, "enter_pit",
                        lambda cfg, tmux=None: {"joined": ["a", "b"], "pit_index": 7, "already": False})
    monkeypatch.setattr(tmuxctl, "select_window",
                        lambda session, index, tmux=None: calls["select"].append((session, index)) or True)

    app = _app(tmp_path)
    async with app.run_test(size=(180, 45)) as pilot:
        await pilot.pause()
        await pilot.press("P")
        await pilot.pause()
        status = str(app.query_one("#deck-status", Static).content)

    assert calls["select"] == [("pantheon", 7)]
    assert "2 agents tiled together" in status


@drives_the_screen
async def test_shift_p_again_leaves_the_pit_and_returns_to_the_deck(tmp_path, monkeypatch):
    from pantheon import tmuxctl
    from pantheon.deck import app as deck_app_mod

    calls = {"select": []}
    monkeypatch.setattr(deck_app_mod.tmux_pit, "is_active", lambda cfg: True)
    monkeypatch.setattr(deck_app_mod.tmux_pit, "leave_pit", lambda cfg, tmux=None: ["a", "b"])
    monkeypatch.setattr(tmuxctl, "select_window",
                        lambda session, index, tmux=None: calls["select"].append((session, index)) or True)

    app = _app(tmp_path)
    async with app.run_test(size=(180, 45)) as pilot:
        await pilot.pause()
        await pilot.press("P")
        await pilot.pause()
        status = str(app.query_one("#deck-status", Static).content)

    assert calls["select"] == [("pantheon", 0)]
    assert "2 windows restored" in status


@drives_the_screen
async def test_shift_p_is_refused_below_desk_width(tmp_path, monkeypatch):
    """acceptance 5 / S-UX R1: too narrow to tile side by side is too narrow to enter."""
    from pantheon.deck import app as deck_app_mod

    entered = []
    monkeypatch.setattr(deck_app_mod.tmux_pit, "is_active", lambda cfg: False)
    monkeypatch.setattr(deck_app_mod.tmux_pit, "enter_pit", lambda cfg, tmux=None: entered.append(1))

    app = _app(tmp_path)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        await pilot.press("P")
        await pilot.pause()
        status = str(app.query_one("#deck-status", Static).content)

    assert entered == []
    assert "120+ columns" in status


# ---------------------------------------------------------------- the stage and the sidebar


def _entry(session_id="s1", project="hiking_log_v2", title="fix the launcher", window_index=3,
           group=None, provider="claude"):
    from pantheon.session_view import models as m

    return m.SessionEntry(
        session_id=session_id, group=group or m.LIVE, project=project, cwd=CWD, title=title,
        provider=provider, status_text="working", window_index=window_index, tmux_session="pantheon",
    )


def _recent(session_id="old1", provider="claude"):
    from pantheon.session_view import models as m

    return _entry(session_id, title="earlier work", window_index=None, group=m.RECENT, provider=provider)


def _deck_with_entries(tmp_path: Path, entries) -> DeckApp:
    """A deck whose sidebar is handed a fixed session list, so nothing here reads the user's
    real `~/.claude` or his workspace folder."""
    cfg = _cfg(tmp_path)
    windows = [TmuxWindow(3, "hiking_log_v2", "node", CWD, "%3", "pantheon")]
    app = DeckApp(cfg, window_source=lambda: windows, source=ob.make(cfg))
    app.rail._entries_source = lambda rows: list(entries)
    app.rail._projects_source = lambda: []
    return app


def _record_stage(monkeypatch):
    from pantheon.deck import app as deck_app_mod

    calls = []

    def fake_stage(cfg, window_index, session_id="", tmux=None, min_width=0):
        calls.append((window_index, session_id))
        return {"ok": True, "action": "staged", "message": "on screen"}

    monkeypatch.setattr(deck_app_mod.stage_mod, "stage", fake_stage)
    return calls


def _record_start(monkeypatch, window_index=7):
    from pantheon.deck import app as deck_app_mod
    from pantheon.models import LaunchResult

    calls = []

    def fake_start(project_dir, cfg, provider, interactive=None, first_message=None, launch_options=None):
        calls.append((project_dir, interactive, first_message, launch_options))
        return LaunchResult(True, "tmux", window_index, "hiking_log_v2", message="claude is open")

    monkeypatch.setattr(deck_app_mod.projects_mod, "start_session", fake_start)
    return calls


async def _settle(pilot):
    await pilot.pause()
    await pilot.app.workers.wait_for_complete()
    await pilot.pause()


@drives_the_screen
async def test_clicking_a_running_session_stages_its_window(tmp_path, monkeypatch):
    staged = _record_stage(monkeypatch)
    entry = _entry(window_index=3)
    app = _deck_with_entries(tmp_path, [entry])
    async with app.run_test(size=(180, 45)) as pilot:
        await pilot.pause()
        app.rail.post_message(app.rail.Activated(entry))
        await _settle(pilot)
    assert staged == [(3, "s1")]


@drives_the_screen
async def test_clicking_a_finished_conversation_resumes_it_then_stages_the_new_window(tmp_path, monkeypatch):
    staged = _record_stage(monkeypatch)
    started = _record_start(monkeypatch, window_index=7)
    entry = _recent("old1")
    app = _deck_with_entries(tmp_path, [entry])
    async with app.run_test(size=(180, 45)) as pilot:
        await pilot.pause()
        app.sup.providers = {"claude": object()}
        app.rail.post_message(app.rail.Activated(entry))
        await _settle(pilot)
        await _settle(pilot)
    assert started == [(CWD, True, "", {"resume": "old1"})]
    assert staged == [(7, "old1")]


@drives_the_screen
async def test_a_conversation_still_open_elsewhere_asks_first_and_no_starts_nothing(tmp_path, monkeypatch):
    from pantheon.deck import app as deck_app_mod
    from pantheon.widgets.modal import Confirm

    started = _record_start(monkeypatch)
    entry = _entry("desk1", window_index=None)       # hooks say running, no tmux window
    app = _deck_with_entries(tmp_path, [entry])
    async with app.run_test(size=(180, 45)) as pilot:
        await pilot.pause()
        app.sup.providers = {"claude": object()}
        app.rail.post_message(app.rail.Activated(entry))
        await pilot.pause()
        assert isinstance(app.screen, Confirm)
        assert app.screen.question == deck_app_mod.CONFIRM_LIVE_ELSEWHERE
        await pilot.press("n")
        await _settle(pilot)
        assert not isinstance(app.screen, Confirm)
    assert started == []


@drives_the_screen
async def test_a_conversation_still_open_elsewhere_resumes_on_yes(tmp_path, monkeypatch):
    _record_stage(monkeypatch)
    started = _record_start(monkeypatch)
    entry = _entry("desk1", window_index=None)
    app = _deck_with_entries(tmp_path, [entry])
    async with app.run_test(size=(180, 45)) as pilot:
        await pilot.pause()
        app.sup.providers = {"claude": object()}
        app.rail.post_message(app.rail.Activated(entry))
        await pilot.pause()
        await pilot.press("y")
        await _settle(pilot)
    assert [c[3] for c in started] == [{"resume": "desk1"}]


@drives_the_screen
async def test_a_finished_codex_conversation_says_why_it_cannot_reopen(tmp_path, monkeypatch):
    from pantheon.deck import app as deck_app_mod

    started = _record_start(monkeypatch)
    entry = _recent("cx1", provider="codex")
    app = _deck_with_entries(tmp_path, [entry])
    async with app.run_test(size=(180, 45)) as pilot:
        await pilot.pause()
        app.rail.post_message(app.rail.Activated(entry))
        await pilot.pause()
        status = str(app.query_one("#deck-status", Static).content)
    assert started == []
    assert status == deck_app_mod.CODEX_NO_RESUME


@drives_the_screen
async def test_new_session_line_opens_the_project_picker(tmp_path, monkeypatch):
    from pantheon.dispatch import projects as projects_mod
    from pantheon.widgets.modal import ProjectPicker

    monkeypatch.setattr(projects_mod, "list_projects",
                        lambda cfg, events=(): [projects_mod.Project("hiking_log_v2", CWD, None)])
    app = _deck_with_entries(tmp_path, [])
    async with app.run_test(size=(180, 45)) as pilot:
        await pilot.pause()
        app.rail.post_message(app.rail.NewSession())
        await pilot.pause()
        assert isinstance(app.screen, ProjectPicker)


@drives_the_screen
async def test_a_project_heading_opens_the_menu_and_new_claude_skips_the_project_step(tmp_path):
    from pantheon.deck import app as deck_app_mod
    from pantheon.widgets.modal import Pick

    app = _deck_with_entries(tmp_path, [])
    async with app.run_test(size=(180, 45)) as pilot:
        await pilot.pause()
        app.rail.post_message(app.rail.ProjectChosen("hiking_log_v2", CWD))
        await pilot.pause()
        assert isinstance(app.screen, Pick)
        assert app.screen.title_text == deck_app_mod.PROJECT_MENU_TITLE.format(name="hiking_log_v2")
        assert [o.label for o in app.screen.options] == [
            "New Claude session here", "New Codex job here", "Resume a conversation here", "Open folder"]
        await pilot.press("c")
        await pilot.pause()
        # The folder is already chosen: the next question is the model, not the project picker.
        assert isinstance(app.screen, Pick) and app.screen.title_text.startswith("model for hiking_log_v2")
        assert app.sup._ns["project_dir"] == CWD and app.sup._ns["provider"] == "claude"


@drives_the_screen
async def test_quit_puts_a_staged_session_back_first(tmp_path, monkeypatch):
    from pantheon.deck import app as deck_app_mod

    released = []
    monkeypatch.setattr(deck_app_mod.stage_mod, "release",
                        lambda cfg, tmux=None: released.append(1) or {"ok": True, "action": "released"})
    app = _deck_with_entries(tmp_path, [])
    async with app.run_test(size=(180, 45)) as pilot:
        await pilot.pause()
        await pilot.press("q")
        await pilot.pause()
    assert released == [1]


@drives_the_screen
async def test_the_pit_puts_a_staged_session_back_before_tiling(tmp_path, monkeypatch):
    from pantheon.deck import app as deck_app_mod

    order = []
    monkeypatch.setattr(deck_app_mod.stage_mod, "release",
                        lambda cfg, tmux=None: order.append("release") or {"ok": True, "action": "none"})
    monkeypatch.setattr(deck_app_mod.tmux_pit, "is_active", lambda cfg: False)
    monkeypatch.setattr(deck_app_mod.tmux_pit, "enter_pit",
                        lambda cfg, tmux=None: order.append("pit") or {"joined": [], "pit_index": None})
    app = _deck_with_entries(tmp_path, [])
    async with app.run_test(size=(180, 45)) as pilot:
        await pilot.pause()
        await pilot.press("P")
        await _settle(pilot)
    assert order == ["release", "pit"]


def _staged(monkeypatch, box):
    from pantheon import stage as stage_mod
    from pantheon.deck import app as deck_app_mod

    monkeypatch.setattr(deck_app_mod.stage_mod, "current",
                        lambda cfg, verify=False, tmux=None: box.get("staged"))
    return stage_mod.Staged("%4", "hiking_log_v2", "s1")


@drives_the_screen
async def test_staged_layout_is_usage_and_sidebar_only_even_though_the_pane_is_narrow(tmp_path, monkeypatch):
    from pantheon.deck import app as deck_app_mod
    from textual.widgets import Footer

    box = {}
    box["staged"] = _staged(monkeypatch, box)
    app = _deck_with_entries(tmp_path, [_entry("s1", window_index=0)])
    async with app.run_test(size=(38, 50)) as pilot:
        await pilot.pause()
        assert app._mode == deck_app_mod.STAGED
        assert app.rail.display and app.stage_usage.display
        assert not app.sup.display and not app.strip.display and not app.queue.display
        assert all(not f.display for f in app.query(Footer))
        assert not app.screen.has_class("narrow")
        usage = str(app.stage_usage.content)
        assert "claude" in usage and "codex" in usage and "5h" in usage and "week" in usage
        rows = [s for s in app.rail._items if s[0] == "session"]
        assert app.rail._items[1] == ("home",)
        assert app.rail.is_on_stage(rows[0][1])


@drives_the_screen
async def test_a_staged_session_that_exits_returns_the_deck_to_home(tmp_path, monkeypatch):
    from pantheon.deck import app as deck_app_mod

    box = {}
    box["staged"] = _staged(monkeypatch, box)
    app = _deck_with_entries(tmp_path, [])
    async with app.run_test(size=(180, 45)) as pilot:
        await pilot.pause()
        assert app._mode == deck_app_mod.STAGED
        box["staged"] = None          # stage.current(verify=True) found the pane gone
        app._refresh_header()
        await pilot.pause()
        assert app._mode == deck_app_mod.DESK
        assert app.sup.display and app.strip.display and not app.stage_usage.display


@drives_the_screen
async def test_back_line_and_b_put_the_staged_session_back(tmp_path, monkeypatch):
    from pantheon.deck import app as deck_app_mod

    box = {}
    box["staged"] = _staged(monkeypatch, box)
    released = []

    def fake_release(cfg, tmux=None):
        released.append(1)
        box["staged"] = None
        return {"ok": True, "action": "released", "message": "hiking_log_v2 is back in its own window"}

    monkeypatch.setattr(deck_app_mod.stage_mod, "release", fake_release)
    app = _deck_with_entries(tmp_path, [])
    async with app.run_test(size=(180, 45)) as pilot:
        await pilot.pause()
        app.rail.post_message(app.rail.Home())
        await _settle(pilot)
        assert released == [1] and app._mode == deck_app_mod.DESK
        status = str(app.query_one("#deck-status", Static).content)
        assert "back in its own window" in status
        await pilot.press("b")          # nothing staged now: says so, releases nothing
        await _settle(pilot)
    assert released == [1]


@drives_the_screen
async def test_a_session_the_new_session_steps_opened_is_staged_on_the_desk_only(tmp_path, monkeypatch):
    staged = _record_stage(monkeypatch)
    for size, expected in (((180, 45), [(9, "")]), ((65, 26), [])):
        staged.clear()
        app = _deck_with_entries(tmp_path / str(size[0]), [])
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            app.sup.post_message(app.sup.SessionStarted(9))
            await _settle(pilot)
        assert staged == expected, size
