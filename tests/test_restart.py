"""restart Pantheon from inside Pantheon -- `R` on the deck, a pick, confirm(s), then ONE
`tmux run-shell -b` carrying the launcher's own restart. The tmux call is captured by a fake
`tmuxctl.run`; no test reaches a real server (tests/conftest.py)."""
from __future__ import annotations

import subprocess

import pytest

from pantheon import restart as restart_mod
from pantheon import tmuxctl
from pantheon.widgets.modal import Confirm, Pick
from tests.test_deck_app import _app, drives_the_screen


class FakeTmux:
    def __init__(self, returncode: int = 0) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.returncode = returncode

    def __call__(self, *args, tmux=None, check=False):
        self.calls.append(args)
        return subprocess.CompletedProcess(["tmux", *args], self.returncode, "", "")


@pytest.fixture
def fake(monkeypatch):
    f = FakeTmux()
    monkeypatch.setattr(tmuxctl, "run", f)
    # The deck starts the pinned Assistant when it opens; these tests count only the
    # restart's own tmux calls, so that start is a no-op here (tests/test_assistant.py covers it).
    from pantheon import assistant as assistant_mod
    monkeypatch.setattr(assistant_mod, "ensure", lambda cfg, **kw: {"ok": True, "action": "none", "message": ""})
    # The per-tick screen reads (startup questions, the phone link, permission prompts) capture
    # agent panes through `tmuxctl.run` too; a tick landing mid-test once added a `capture-pane`
    # of `pantheon:3` to the calls counted here. Off for these tests.
    from pantheon.supervisor import pane as pane_mod
    monkeypatch.setattr(pane_mod.SupervisorPane, "_mark_dialogs", lambda self, rows: rows)
    monkeypatch.setattr(pane_mod.SupervisorPane, "_mark_approvals", lambda self, rows: rows)
    return f


def _status(app) -> str:
    return app._message


def test_launcher_line_is_the_launchers_own_restart(tmp_path):
    from pantheon import config as config_mod
    cfg = config_mod.Config(state_dir=str(tmp_path / "state"))
    line = restart_mod.launcher_line(cfg, root=tmp_path / "checkout")
    assert f"{(tmp_path / 'checkout' / 'bin' / 'pantheon').as_posix()}" in line
    assert " --restart --no-attach " in line and "--all" not in line
    assert line.startswith("PANTHEON_SESSION=pantheon ")
    assert "restart.log" in line and line.endswith("&")
    everything = restart_mod.launcher_line(cfg, everything=True, root=tmp_path / "checkout")
    assert " --restart --all --no-attach " in everything


@drives_the_screen
async def test_R_then_deck_only_then_yes_runs_one_run_shell(tmp_path, fake):
    app = _app(tmp_path)
    async with app.run_test(size=(160, 50)) as pilot:
        await pilot.pause()
        await pilot.press("R")
        await pilot.pause()
        assert isinstance(app.screen, Pick)
        assert fake.calls == []
        await pilot.press("r")
        await pilot.pause()
        assert isinstance(app.screen, Confirm)
        assert app.screen.question == restart_mod.CONFIRM_DECK
        assert "your agents keep running" in app.screen.question
        assert fake.calls == []            # nothing fires while a modal is open
        await pilot.press("y")
        await pilot.pause()
        assert len(fake.calls) == 1
        args = fake.calls[0]
        assert args[:2] == ("run-shell", "-b")
        assert "bin/pantheon' --restart --no-attach" in args[2] or "bin/pantheon --restart --no-attach" in args[2]
        assert "--all" not in args[2]
        assert _status(app) == restart_mod.STARTED_DECK


@drives_the_screen
async def test_restart_everything_asks_twice_before_all(tmp_path, fake):
    app = _app(tmp_path)
    async with app.run_test(size=(160, 50)) as pilot:
        await pilot.pause()
        await pilot.press("R")
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
        assert isinstance(app.screen, Confirm) and app.screen.danger
        assert app.screen.question == restart_mod.CONFIRM_ALL_1
        await pilot.press("y")
        await pilot.pause()
        assert isinstance(app.screen, Confirm)          # the second ask
        assert app.screen.question == restart_mod.CONFIRM_ALL_2
        assert fake.calls == []
        await pilot.press("y")
        await pilot.pause()
        assert len(fake.calls) == 1
        assert fake.calls[0][:2] == ("run-shell", "-b")
        assert "--restart --all --no-attach" in fake.calls[0][2]
        assert _status(app) == restart_mod.STARTED_ALL


@drives_the_screen
@pytest.mark.parametrize("keys", [["escape"], ["r", "n"], ["r", "escape"], ["a", "n"], ["a", "y", "n"],
                                  ["a", "y", "escape"]])
async def test_every_way_out_runs_nothing(tmp_path, fake, keys):
    app = _app(tmp_path)
    async with app.run_test(size=(160, 50)) as pilot:
        await pilot.pause()
        await pilot.press("R")
        await pilot.pause()
        for key in keys:
            await pilot.press(key)
            await pilot.pause()
        assert not isinstance(app.screen, (Pick, Confirm))
        assert fake.calls == []
        assert _status(app) == restart_mod.CANCELLED


@drives_the_screen
async def test_R_works_at_phone_width(tmp_path, fake):
    app = _app(tmp_path)
    async with app.run_test(size=(65, 26)) as pilot:
        await pilot.pause()
        await pilot.press("R")
        await pilot.pause()
        assert isinstance(app.screen, Pick)
        await pilot.press("r")
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()
        assert len(fake.calls) == 1 and fake.calls[0][:2] == ("run-shell", "-b")


@drives_the_screen
async def test_tmux_refusal_says_so(tmp_path, monkeypatch):
    refused = FakeTmux(returncode=1)
    monkeypatch.setattr(tmuxctl, "run", refused)
    from pantheon import assistant as assistant_mod   # its start-of-deck check is not the restart's call
    monkeypatch.setattr(assistant_mod, "ensure", lambda cfg, **kw: {"ok": True, "action": "none", "message": ""})
    app = _app(tmp_path)
    async with app.run_test(size=(160, 50)) as pilot:
        await pilot.pause()
        for key in ("R", "r", "y"):
            await pilot.press(key)
            await pilot.pause()
        assert len(refused.calls) == 1
        assert "did not take the restart" in _status(app)


def test_quit_says_what_it_quits(tmp_path):
    from pantheon import keys as keys_mod
    from pantheon.deck.app import DeckApp
    assert "this window only" in restart_mod.QUIT_NOTICE
    assert "agents keep running" in restart_mod.QUIT_NOTICE
    q = [b for b in DeckApp.BINDINGS if b.key == "q"][0]
    assert q.description == "quit window"
    assert "quit this deck window only; your agents keep running" in keys_mod.DECK
    assert "R    restart" in keys_mod.DECK


def test_deck_main_prints_the_quit_notice(tmp_path, monkeypatch, capsys):
    from pantheon.deck import app as deck_app
    from tests.test_deck_app import _cfg
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(deck_app.config_mod, "load", lambda: cfg)
    monkeypatch.setattr(deck_app.DeckApp, "run", lambda self: None)
    assert deck_app.main() == 0
    assert restart_mod.QUIT_NOTICE in capsys.readouterr().out


def _standalone_apps(tmp_path):
    """The three standalone windows, each with test-safe sources."""
    from pantheon import config as config_mod
    from pantheon.hud.app import HudApp
    from pantheon.supervisor.app import SupervisorApp
    from tests.test_queue_app import _app as queue_app
    return {
        "queue": queue_app(tmp_path),
        "hud": HudApp(config_mod.Config(state_dir=str(tmp_path / "hud-state")), start_refresher=False),
        "supervisor": SupervisorApp(cfg=config_mod.Config(state_dir=str(tmp_path / "sup-state")),
                                    window_source=lambda: []),
    }


@pytest.mark.parametrize("which", ["queue", "hud", "supervisor"])
def test_standalone_windows_restart_the_same_way(tmp_path, fake, which):
    import asyncio

    app = _standalone_apps(tmp_path)[which]

    async def drive():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("R")
            await pilot.pause()
            assert isinstance(app.screen, Pick)
            await pilot.press("r")
            await pilot.pause()
            assert isinstance(app.screen, Confirm)
            assert app.screen.question == restart_mod.CONFIRM_DECK
            await pilot.press("y")
            await pilot.pause()

    asyncio.run(drive())
    assert len(fake.calls) == 1
    assert fake.calls[0][:2] == ("run-shell", "-b")
    assert "--restart --no-attach" in fake.calls[0][2] and "--all" not in fake.calls[0][2]


def test_standalone_key_lists_name_R_and_say_what_q_quits():
    from pantheon import keys as keys_mod
    for section in (keys_mod.QUEUE, keys_mod.HUD):
        assert "R    restart the deck, queue and budget windows, or everything" in section
        assert "window only; your agents keep running" in section


def test_a_staged_session_is_put_back_before_the_restart(tmp_path, monkeypatch):
    """`respawn-window -k` on window 0 would kill a session staged beside the deck."""
    from pantheon import config as config_mod
    from pantheon import restart as restart_mod

    cfg = config_mod.Config(state_dir=str(tmp_path / "state"))
    order = []
    monkeypatch.setattr(restart_mod.stage_mod, "release",
                        lambda cfg, tmux=None: order.append("release") or {"ok": True, "action": "released"})
    fake_run = lambda *a, tmux=None: order.append(a[0]) or type("CP", (), {"returncode": 0})()
    assert restart_mod.run_restart(cfg, run=fake_run) is True
    assert order == ["release", "run-shell"]


def test_no_restart_when_the_staged_session_will_not_move(tmp_path, monkeypatch):
    from pantheon import config as config_mod
    from pantheon import restart as restart_mod

    cfg = config_mod.Config(state_dir=str(tmp_path / "state"))
    ran = []
    monkeypatch.setattr(restart_mod.stage_mod, "release",
                        lambda cfg, tmux=None: {"ok": False, "action": "none", "message": "stuck"})
    assert restart_mod.run_restart(cfg, run=lambda *a, tmux=None: ran.append(a)) is False
    assert ran == []
