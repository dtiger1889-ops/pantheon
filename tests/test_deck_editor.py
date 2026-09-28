"""The deck's side of the editor pane: `E` / `e` / `✎ Edit a file` ask once for
a file, relative to the highlighted session's folder, and hand it to `editor.open_beside_stage`;
an editor beside the list keeps the deck in its sidebar layout; `b` moves it out, never closes it.
`pantheon/editor.py` itself is tested against a fake tmux in tests/test_editor.py."""
from __future__ import annotations

from pantheon.deck import app as deck_app_mod
from pantheon.widgets.modal import TextPrompt
from tests.test_deck_app import CWD, _deck_with_entries, _entry, drives_the_screen


def _record_open(monkeypatch, where=None):
    calls = []

    def fake_open(cfg, path="", line=None, cwd="", **kw):
        calls.append((path, cwd))
        return {"ok": True, "action": "opened", "message": f"{path.rsplit('/', 1)[-1]} is open in nano beside the list"}

    monkeypatch.setattr(deck_app_mod.editor_mod, "open_beside_stage", fake_open)
    monkeypatch.setattr(deck_app_mod.editor_mod, "where", lambda cfg, tmux=None: where)
    return calls


async def _settle(app, pilot):
    await pilot.pause()
    await app.workers.wait_for_complete()
    await pilot.pause()


@drives_the_screen
async def test_E_asks_which_file_in_the_selected_sessions_folder_then_opens_it(tmp_path, monkeypatch):
    calls = _record_open(monkeypatch)
    app = _deck_with_entries(tmp_path, [_entry("s1")])
    async with app.run_test(size=(200, 50)) as pilot:
        await pilot.pause()
        app.rail.focus_table()
        app.rail.query_one("#sidebar-list").index = 3          # ＋, ✎, heading, then s1
        await pilot.pause()
        await pilot.press("E")
        await pilot.pause()
        assert isinstance(app.screen, TextPrompt)
        assert app.screen.title_text == deck_app_mod.EDIT_PROMPT_TITLE
        assert CWD in app.screen.placeholder
        await pilot.press(*"notes.md")
        await pilot.press("enter")
        await _settle(app, pilot)
        assert calls == [(f"{CWD}/notes.md", CWD)]
        assert "notes.md is open in nano beside the list" in app._message


@drives_the_screen
async def test_e_in_the_sidebar_does_the_same_as_E(tmp_path, monkeypatch):
    calls = _record_open(monkeypatch)
    app = _deck_with_entries(tmp_path, [_entry("s1")])
    async with app.run_test(size=(200, 50)) as pilot:
        await pilot.pause()
        app.rail.focus_table()
        app.rail.query_one("#sidebar-list").index = 3
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()
        assert isinstance(app.screen, TextPrompt)
        await pilot.press("escape")
        await _settle(app, pilot)
        assert calls == [] and app._message == deck_app_mod.EDIT_NOTHING


@drives_the_screen
async def test_an_open_editor_is_focused_or_brought_back_never_asked_about_again(tmp_path, monkeypatch):
    calls = _record_open(monkeypatch, where={"pane_id": "%9", "path": "/x/a.md",
                                             "window_index": 6, "beside": False})
    app = _deck_with_entries(tmp_path, [_entry("s1")])
    async with app.run_test(size=(200, 50)) as pilot:
        await pilot.pause()
        await pilot.press("E")
        await _settle(app, pilot)
        assert not isinstance(app.screen, TextPrompt)
        assert calls == [("", "")]


@drives_the_screen
async def test_an_editor_beside_the_list_keeps_the_sidebar_layout_and_b_moves_it_out(tmp_path, monkeypatch):
    _record_open(monkeypatch, where={"pane_id": "%9", "path": "/x/a.md", "window_index": 0, "beside": True})
    moved = []
    monkeypatch.setattr(deck_app_mod.stage_mod, "release_all",
                        lambda cfg, tmux=None: moved.append(1) or {
                            "ok": True, "action": "released",
                            "message": "the editor moved to its own window, EDIT (6), with your text; e brings it back"})
    app = _deck_with_entries(tmp_path, [_entry("s1")])
    async with app.run_test(size=(120, 50)) as pilot:
        await pilot.pause()
        assert app._mode == deck_app_mod.STAGED
        assert ("home",) in app.rail._items
        await pilot.press("b")
        await _settle(app, pilot)
        assert moved == [1]
        assert "with your text" in app._message


@drives_the_screen
async def test_quit_moves_the_editor_out_before_the_deck_exits(tmp_path, monkeypatch):
    _record_open(monkeypatch)
    moved = []
    monkeypatch.setattr(deck_app_mod.stage_mod, "release_all",
                        lambda cfg, tmux=None: moved.append(1) or {"ok": True, "action": "none", "message": ""})
    app = _deck_with_entries(tmp_path, [_entry("s1")])
    async with app.run_test(size=(200, 50)) as pilot:
        await pilot.pause()
        await pilot.press("q")
        await pilot.pause()
    assert moved == [1]
