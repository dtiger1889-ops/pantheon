"""step 2 (presence) and step 6 (the deck's own mirror of a sent notification), wired into
`pantheon/deck/app.py` -- the combined desk view is the one place both pieces the standalone
notifier can't reach on its own (a live human at the keyboard, a screen to show the line on) live.
"""
from __future__ import annotations

import asyncio
import functools
import json
import shutil
from pathlib import Path

from textual.widgets import Static

from pantheon import config as config_mod
from pantheon.deck.app import DeckApp, _sent_channel_text
from pantheon.notify import presence as presence_mod
from pantheon.tasks import obsidian_base as ob

FIXTURES = Path(__file__).parent / "fixtures"


def drives_the_screen(test):
    @functools.wraps(test)
    def wrapper(*args, **kwargs):
        return asyncio.run(test(*args, **kwargs))
    return wrapper


def _cfg(tmp_path: Path) -> config_mod.Config:
    projects = tmp_path / "Projects"
    shutil.copytree(FIXTURES / "sprints", projects / "Sprints")
    shutil.copy(FIXTURES / "Sprints.base", projects / "Sprints.base")
    cfg = config_mod.Config(vault=str(tmp_path), state_dir=str(tmp_path / "state"),
                            projects_root="C:/Home/x/Documents/Projects")
    cfg.events_file.parent.mkdir(parents=True, exist_ok=True)
    return cfg


def _app(tmp_path: Path) -> DeckApp:
    cfg = _cfg(tmp_path)
    app = DeckApp(cfg, window_source=lambda: [], source=ob.make(cfg))
    return app


def _status(app: DeckApp) -> str:
    return str(app.query_one("#deck-status", Static).content)


# ---------------------------------------------------------------- presence: keys and clicks


@drives_the_screen
async def test_a_key_press_touches_the_presence_file(tmp_path):
    app = _app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert not Path(app.cfg.presence_file).exists()
        await pilot.press("j")
        await pilot.pause()
        assert presence_mod.read_deck_touch(app.cfg) is not None


@drives_the_screen
async def test_a_key_press_does_not_swallow_the_event(tmp_path):
    """The presence touch rides along with normal key handling -- Tab still switches panels.

    at desk width the deck is SESSIONS | the agents table, the focus starts in the table,
    and Tab steps into the sidebar, not into the QUEUE (off screen until `t` asks for it)."""
    app = _app(tmp_path)
    async with app.run_test(size=(180, 45)) as pilot:
        await pilot.pause()
        assert app.focused is not None and app.focused in list(app.sup.walk_children(with_self=True))
        await pilot.press("tab")
        await pilot.pause()
        assert app.focused is not None and app.focused in list(app.rail.walk_children(with_self=True))
        assert presence_mod.read_deck_touch(app.cfg) is not None


@drives_the_screen
async def test_a_click_touches_the_presence_file(tmp_path):
    app = _app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert not Path(app.cfg.presence_file).exists()
        await pilot.click("#deck-header")
        await pilot.pause()
        assert presence_mod.read_deck_touch(app.cfg) is not None


@drives_the_screen
async def test_the_30_second_timer_touches_presence_only_while_focused(tmp_path):
    app = _app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        Path(app.cfg.state_dir).mkdir(parents=True, exist_ok=True)
        if Path(app.cfg.presence_file).exists():
            Path(app.cfg.presence_file).unlink()
        app.app_focus = False
        app._touch_presence_if_focused()
        assert not Path(app.cfg.presence_file).exists()
        app.app_focus = True
        app._touch_presence_if_focused()
        assert presence_mod.read_deck_touch(app.cfg) is not None


# ---------------------------------------------------------------- the sent line


def _append_sent(cfg, record: dict) -> None:
    path = cfg.notify_sent_file
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


@drives_the_screen
async def test_a_new_sent_line_shows_once_in_the_status_line(tmp_path):
    app = _app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        _append_sent(app.cfg, {
            "title": "Claude needs you", "body": "loom-os, window 3",
            "channels": ["toast"], "detail": "",
            "results": [{"channel": "toast", "ok": True, "detail": ""}],
        })
        app._check_sent_notifications()
        await pilot.pause()
        assert _status(app) == "sent: Claude needs you - loom-os, window 3 -> toast"


@drives_the_screen
async def test_lines_already_present_before_the_deck_opened_are_not_shown(tmp_path):
    cfg = _cfg(tmp_path)
    _append_sent(cfg, {"title": "Old news", "body": "before the deck opened",
                       "channels": ["ntfy"], "detail": ""})
    app = DeckApp(cfg, window_source=lambda: [], source=ob.make(cfg))
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert "Old news" not in _status(app)


@drives_the_screen
async def test_only_the_most_recent_of_several_new_lines_is_shown(tmp_path):
    app = _app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        _append_sent(app.cfg, {"title": "First", "body": "a", "channels": ["ntfy"], "detail": ""})
        _append_sent(app.cfg, {"title": "Second", "body": "b", "channels": ["ntfy"], "detail": ""})
        app._check_sent_notifications()
        await pilot.pause()
        assert "Second" in _status(app)
        assert "First" not in _status(app)


@drives_the_screen
async def test_no_new_lines_leaves_the_status_line_untouched(tmp_path):
    app = _app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app._say("jumped to window 3")
        app._check_sent_notifications()
        await pilot.pause()
        assert _status(app) == "jumped to window 3"


@drives_the_screen
async def test_a_dry_run_record_shows_the_intended_channels(tmp_path):
    """A dry-run notification never reaches `channels.py`'s senders, so it has no `results` --
    the deck falls back to the channels the notifier decided on."""
    app = _app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        _append_sent(app.cfg, {"title": "Codex job failed", "body": "pottery_studios",
                               "channels": ["toast", "ntfy"], "detail": "", "dry_run": True})
        app._check_sent_notifications()
        await pilot.pause()
        assert _status(app).endswith("-> toast+ntfy")


def test_sent_channel_text_prefers_successful_results_over_the_intended_list():
    record = {"channels": ["toast", "ntfy"],
             "results": [{"channel": "toast", "ok": True}, {"channel": "ntfy", "ok": False}]}
    assert _sent_channel_text(record) == "toast"


def test_sent_channel_text_falls_back_to_channels_when_nothing_succeeded():
    record = {"channels": ["toast"], "results": [{"channel": "toast", "ok": False}]}
    assert _sent_channel_text(record) == "toast"


def test_sent_channel_text_is_a_placeholder_when_nothing_is_named():
    assert _sent_channel_text({}) == "?"
