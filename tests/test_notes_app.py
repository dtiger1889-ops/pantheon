"""The scratchpad window, driven headlessly by Textual's own test pilot.

No tmux server is touched (`window_source` is injected); the store lives under `tmp_path`, never
the repo's real `state/`.
"""
from __future__ import annotations

import asyncio

from textual.widgets import TextArea

from pantheon import config as config_mod
from pantheon.notes import app as notes_app
from pantheon.notes import store


def _cfg(tmp_path):
    # `claude_home` points at an empty tmp folder -- never the user's real `~/.claude` (project
    # rule: tests touch only a temp state dir), so slash-command completion sees just the
    # built-ins unless a test injects its own commands/skills fixture.
    return config_mod.Config(state_dir=str(tmp_path / "state"), claude_home=str(tmp_path / "claude_home"))


def _editor(app: notes_app.NotesApp) -> TextArea:
    return app.query_one("#editor", TextArea)


# ---------------------------------------------------------------- acceptance 1: restore


def test_reopening_restores_the_same_tab_and_content_a_hard_kill_left(tmp_path):
    """Simulates `tmux kill-window` then `pantheon --notes` again: write the fixture the store
    would have left, mount a FRESH app instance against the same folder, and check it restores
    with no "recover?" prompt (there is none to dismiss -- the content is just there)."""
    cfg = _cfg(tmp_path)
    d = tmp_path / "state" / "notes"
    store.write(d, "handoff", "a prompt I was mid-typing")
    store.write(d, "scratch", "an older draft")
    store.write_session(d, ["scratch", "handoff"], "handoff")

    async def drive():
        app = notes_app.NotesApp(cfg, window_source=lambda: [])
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            assert app.pane.focused == "handoff"
            assert app.pane.tabs == ["scratch", "handoff"]
            assert _editor(app).text == "a prompt I was mid-typing"

    asyncio.run(drive())


def test_first_run_with_no_folder_creates_one_empty_untitled_tab(tmp_path):
    cfg = _cfg(tmp_path)

    async def drive():
        app = notes_app.NotesApp(cfg, window_source=lambda: [])
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            assert app.pane.tabs == ["untitled"]
            assert app.pane.focused == "untitled"
            assert _editor(app).text == ""

    asyncio.run(drive())


# ---------------------------------------------------------------- acceptance 2: debounced autosave


def test_typing_is_never_dropped_and_the_debounced_write_lands_on_disk(tmp_path, monkeypatch):
    # A long debounce so 200 keystrokes -- each one re-arming it -- provably never lets it fire
    # mid-typing; the disk content is asserted to still be BEHIND the editor right after typing,
    # then to catch up once the debounce (shrunk here) actually elapses.
    monkeypatch.setattr(notes_app, "DEBOUNCE_SECONDS", 2.0)
    cfg = _cfg(tmp_path)

    async def drive():
        app = notes_app.NotesApp(cfg, window_source=lambda: [])
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            text = "x" * 200
            for ch in text:
                await pilot.press(ch)
            assert _editor(app).text == text, "a keystroke was dropped while typing fast"
            # Still inside the idle window -- nothing flushed yet.
            assert store.read(app.pane.dir, app.pane.focused) != text
            notes_app.DEBOUNCE_SECONDS = 0.0  # let the NEXT arm (there is none more -- fire now)
            app.pane._on_debounce_fire()      # simulate the idle timer elapsing
            assert store.read(app.pane.dir, app.pane.focused) == text

    asyncio.run(drive())


def test_switching_tabs_flushes_the_old_one_immediately_not_after_the_debounce(tmp_path, monkeypatch):
    monkeypatch.setattr(notes_app, "DEBOUNCE_SECONDS", 5.0)   # long enough that only the explicit
                                                              # flush-on-switch could satisfy this
    cfg = _cfg(tmp_path)
    d = tmp_path / "state" / "notes"
    store.write(d, "other", "")
    store.write(d, "untitled", "")
    store.write_session(d, ["other", "untitled"], "untitled")

    async def drive():
        app = notes_app.NotesApp(cfg, window_source=lambda: [])
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            for ch in "hello":
                await pilot.press(ch)
            app.pane._tab_clicked("other")
            await pilot.pause()
            assert store.read(app.pane.dir, "untitled") == "hello"

    asyncio.run(drive())


def test_blur_flushes_immediately(tmp_path, monkeypatch):
    monkeypatch.setattr(notes_app, "DEBOUNCE_SECONDS", 5.0)
    cfg = _cfg(tmp_path)

    async def drive():
        app = notes_app.NotesApp(cfg, window_source=lambda: [])
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            for ch in "blur me":
                await pilot.press(ch)
            app.pane._on_editor_blur()
            assert store.read(app.pane.dir, app.pane.focused) == "blur me"

    asyncio.run(drive())


def test_quit_flushes_and_writes_the_session(tmp_path, monkeypatch):
    monkeypatch.setattr(notes_app, "DEBOUNCE_SECONDS", 5.0)
    cfg = _cfg(tmp_path)

    async def drive():
        app = notes_app.NotesApp(cfg, window_source=lambda: [])
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            for ch in "quit me":
                await pilot.press(ch)
            app.action_quit()
            assert store.read(app.pane.dir, "untitled") == "quit me"
            session = store.read_session(app.pane.dir)
            assert session["focused"] == "untitled"

    asyncio.run(drive())


# ---------------------------------------------------------------- acceptance 3 (widget wiring): the
# Insert button says so when nothing is selectable, and does not crash with no agent.


def test_insert_with_no_selectable_agent_says_so_and_sends_nothing(tmp_path, monkeypatch):
    from pantheon import tmuxctl

    calls = []
    monkeypatch.setattr(tmuxctl, "run", lambda *a, **k: calls.append(a) or __import__("subprocess").CompletedProcess(a, 0, "", ""))
    cfg = _cfg(tmp_path)

    async def drive():
        app = notes_app.NotesApp(cfg, window_source=lambda: [])
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            for ch in "a draft with no agent to go to":
                await pilot.press(ch)
            app.pane.action_insert()
            assert app.pane._message_role == "warning"
            assert not calls, "nothing should be sent to tmux with no selectable agent"

    asyncio.run(drive())


# ---------------------------------------------------------------- acceptance 4 (widget wiring): the
# slash-command picker opens and inserts without sending.


def test_slash_completion_inserts_the_chosen_command_and_sends_nothing(tmp_path, monkeypatch):
    from pantheon.notes import complete as complete_mod

    monkeypatch.setattr(
        complete_mod, "list_commands",
        lambda claude_home=None, extra_skills_dirs=None: [
            complete_mod.Command("/checkpoint", "save progress", "command"),
        ],
    )
    cfg = _cfg(tmp_path)

    async def drive():
        app = notes_app.NotesApp(cfg, window_source=lambda: [])
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            for ch in "/che":
                await pilot.press(ch)
            app.pane.action_complete()
            await pilot.pause()
            await pilot.press("enter")   # Pick's OptionList: Enter accepts the highlighted option
            await pilot.pause()
            assert _editor(app).text == "/checkpoint "

    asyncio.run(drive())
