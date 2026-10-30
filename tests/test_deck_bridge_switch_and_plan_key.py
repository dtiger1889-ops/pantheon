"""The `[editor] vscode_bridge` switch and the planning page's deck key (`U`).

Switch off: `E` asks for a file exactly as before and `V` still works. Switch on, desk width: `E`
asks Edit here / Open in VS Code. Switch on, phone width: no choice, VS Code never offered.
`U` opens the planning window through `pantheon/planning/window.py`, tested against a fake tmux.
"""
from __future__ import annotations

from pantheon import config as config_mod
from pantheon import editor as editor_mod
from pantheon.deck import app as deck_app_mod
from pantheon.planning import window as plan_window
from pantheon.widgets.modal import Pick, TextPrompt
from tests.test_deck_app import _deck_with_entries, _entry, drives_the_screen


def _deck(tmp_path, bridge):
    app = _deck_with_entries(tmp_path, [_entry("s1")])
    if bridge is not None:
        app.cfg.editor["vscode_bridge"] = bridge
    return app


def _record(monkeypatch):
    here, vscode = [], []
    monkeypatch.setattr(deck_app_mod.editor_mod, "where", lambda cfg, tmux=None: None)
    monkeypatch.setattr(deck_app_mod.editor_mod, "open_beside_stage",
                        lambda cfg, path="", line=None, cwd="", **kw:
                        here.append(path) or {"ok": True, "action": "opened", "message": "open"})
    monkeypatch.setattr(deck_app_mod.editor_bridge_mod, "request_open",
                        lambda ecfg, state, path, line=None, **kw:
                        vscode.append(path) or {"ok": True, "message": "opening in VS Code"})
    return here, vscode


async def _to_row(app, pilot):
    await pilot.pause()
    app.rail.focus_table()
    app.rail.query_one("#sidebar-list").index = 3
    await pilot.pause()


async def _settle(app, pilot):
    await pilot.pause()
    await app.workers.wait_for_complete()
    await pilot.pause()


def test_switch_reads_only_a_real_true(tmp_path):
    cfg = config_mod.Config()
    assert editor_mod.vscode_bridge_enabled(cfg) is False
    cfg.editor["vscode_bridge"] = "false"
    assert editor_mod.vscode_bridge_enabled(cfg) is False
    cfg.editor["vscode_bridge"] = True
    assert editor_mod.vscode_bridge_enabled(cfg) is True


@drives_the_screen
async def test_switch_off_E_asks_for_a_file_as_before(tmp_path, monkeypatch):
    _record(monkeypatch)
    app = _deck(tmp_path, None)
    async with app.run_test(size=(200, 50)) as pilot:
        await _to_row(app, pilot)
        await pilot.press("E")
        await pilot.pause()
        assert isinstance(app.screen, TextPrompt)


@drives_the_screen
async def test_switch_off_V_still_opens_vscode(tmp_path, monkeypatch):
    _, vscode = _record(monkeypatch)
    app = _deck(tmp_path, False)
    async with app.run_test(size=(200, 50)) as pilot:
        await _to_row(app, pilot)
        await pilot.press("V")
        await _settle(app, pilot)
    assert len(vscode) == 1


@drives_the_screen
async def test_switch_on_E_offers_the_two_choices_and_here_asks_for_a_file(tmp_path, monkeypatch):
    here, vscode = _record(monkeypatch)
    app = _deck(tmp_path, True)
    async with app.run_test(size=(200, 50)) as pilot:
        await _to_row(app, pilot)
        await pilot.press("E")
        await pilot.pause()
        assert isinstance(app.screen, Pick)
        labels = [o.label for o in app.screen.options]
        assert labels == ["Edit here", "Open in VS Code"]
        await pilot.press("1")
        await pilot.pause()
        assert isinstance(app.screen, TextPrompt)
    assert vscode == []


@drives_the_screen
async def test_switch_on_choosing_vscode_uses_the_bridge(tmp_path, monkeypatch):
    here, vscode = _record(monkeypatch)
    app = _deck(tmp_path, True)
    async with app.run_test(size=(200, 50)) as pilot:
        await _to_row(app, pilot)
        await pilot.press("E")
        await pilot.pause()
        await pilot.press("2")
        await _settle(app, pilot)
        assert "opening in VS Code" in app._message
    assert len(vscode) == 1 and here == []


@drives_the_screen
async def test_switch_on_at_phone_width_hides_vscode(tmp_path, monkeypatch):
    _record(monkeypatch)
    app = _deck(tmp_path, True)
    async with app.run_test(size=(70, 40)) as pilot:
        await pilot.pause()
        assert app._mode == deck_app_mod.PHONE
        app.action_edit_file()
        await pilot.pause()
        assert not isinstance(app.screen, Pick)
        assert isinstance(app.screen, TextPrompt)


# ------------------------------------------------------------------ U: the planning window

def test_U_is_bound_once_and_not_the_pit_key():
    keys = [b.key for b in deck_app_mod.DeckApp.BINDINGS]
    assert keys.count("U") == 1 and keys.count("P") == 1
    action = {b.key: b.action for b in deck_app_mod.DeckApp.BINDINGS}
    assert action["U"] == "open_planning" and action["P"] == "toggle_pit"


def test_U_is_on_the_help_page():
    from pantheon import keys
    assert "    U    the usage-planning page" in keys.KEYS_TEXT


def test_plan_window_opens_a_new_window_running_bin_plan(monkeypatch, tmp_path):
    calls = []
    switched = []
    monkeypatch.setattr(plan_window.tmuxctl, "window_index_by_name", lambda s, n, tmux=None: None)
    monkeypatch.setattr(plan_window.tmuxctl, "new_window",
                        lambda s, n, cwd, command=None, tmux=None, detached=False:
                        calls.append((s, n, command, detached)) or 5)
    monkeypatch.setattr(plan_window.tmuxctl, "select_window",
                        lambda s, i, tmux=None: switched.append(i) or True)
    result = plan_window.open_window(config_mod.Config())
    assert result["ok"] and result["action"] == "opened"
    session, name, command, detached = calls[0]
    assert name == "PLAN" and "bin/plan" in command and detached is False
    # new-window selects only in the base session, so the first
    # U must also select it across the group or an attached client stays on DECK.
    assert switched == [5]


def test_plan_window_reuses_the_open_one(monkeypatch):
    switched = []
    monkeypatch.setattr(plan_window.tmuxctl, "window_index_by_name", lambda s, n, tmux=None: 4)
    monkeypatch.setattr(plan_window.tmuxctl, "select_window",
                        lambda s, i, tmux=None: switched.append(i) or True)
    monkeypatch.setattr(plan_window.tmuxctl, "new_window",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("second window")))
    result = plan_window.open_window(config_mod.Config())
    assert switched == [4] and result["action"] == "switched"


def test_plan_window_says_so_when_tmux_will_not(monkeypatch):
    monkeypatch.setattr(plan_window.tmuxctl, "window_index_by_name", lambda s, n, tmux=None: None)
    monkeypatch.setattr(plan_window.tmuxctl, "new_window", lambda *a, **k: None)
    result = plan_window.open_window(config_mod.Config())
    assert not result["ok"] and "nothing changed" in result["message"]


def test_plan_window_is_not_treated_as_an_agent_window():
    from pantheon import tmuxfeed
    from pantheon.pit import tmux_pit
    assert "PLAN" in tmux_pit.RESERVED_WINDOWS and "PLAN" in tmuxfeed.SKIP_WINDOWS
