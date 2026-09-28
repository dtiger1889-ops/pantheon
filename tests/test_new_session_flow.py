""" acceptance: one pilot-driven walk per path, on the deck itself,
with a fake provider and a fake `session_ctl`, so what gets launched and what gets typed is
asserted rather than eyeballed."""
from __future__ import annotations

import asyncio
import dataclasses
import functools
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from textual.screen import ModalScreen
from textual.widgets import Input, OptionList

from pantheon import config as config_mod
from pantheon.deck.app import DeckApp
from pantheon.dispatch import sessions as sessions_mod
from pantheon.models import LaunchResult, TmuxWindow
from pantheon.providers.claude import claude_command
from pantheon.supervisor import pane as pane_mod
from pantheon.tasks import obsidian_base as ob
from pantheon.widgets.modal import Pick, ProjectPicker, TextPrompt

FIXTURES = Path(__file__).parent / "fixtures"


def drives_the_screen(test):
    @functools.wraps(test)
    def wrapper(*args, **kwargs):
        return asyncio.run(test(*args, **kwargs))
    return wrapper


class FakeProvider:
    def __init__(self, name: str, interactive_tmux: bool) -> None:
        self.name = name
        self._caps = {"interactive_tmux"} if interactive_tmux else set()
        self.calls: list[dict] = []

    def capabilities(self):
        return set(self._caps)

    def launch(self, project_dir, briefing_path, interactive, options=None):
        self.calls.append({"project_dir": project_dir, "briefing": briefing_path,
                           "interactive": interactive, "options": dict(options or {})})
        return LaunchResult(True, "tmux", 5, Path(project_dir).name, tmux_pane="%5",
                            message=f"{self.name} is open in window 5")


def make_cfg(tmp_path: Path):
    root = tmp_path / "Claude"
    for name in ("habit_notes", "almanac", "loom-os"):
        (root / name).mkdir(parents=True)
        (root / name / "CLAUDE.md").write_text("x", encoding="utf-8")
    vault = tmp_path / "vault" / "Projects"
    import shutil
    shutil.copytree(FIXTURES / "sprints", vault / "Sprints")
    shutil.copy(FIXTURES / "Sprints.base", vault / "Sprints.base")
    cfg = config_mod.Config(vault=str(tmp_path / "vault"), state_dir=str(tmp_path / "state"))
    cfg = dataclasses.replace(cfg, projects_root=str(root))
    config_mod.ensure_state_dirs(cfg)
    return cfg, root


class Typed:
    """Stands in for `session_ctl.set_model/set_effort/set_mode`: records, never touches tmux."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def set_model(self, target, model, tmux=None):
        self.calls.append(("model", target, model)); return None

    def set_effort(self, target, effort, tmux=None):
        self.calls.append(("effort", target, effort)); return None

    def set_mode(self, target, current, mode, tmux=None):
        self.calls.append(("mode", target, mode)); return None


async def start_deck(cfg, monkeypatch, sessions=None):
    typed = Typed()
    monkeypatch.setattr(pane_mod.session_ctl, "set_model", typed.set_model)
    monkeypatch.setattr(pane_mod.session_ctl, "set_effort", typed.set_effort)
    monkeypatch.setattr(pane_mod.session_ctl, "set_mode", typed.set_mode)
    monkeypatch.setattr(pane_mod.sessions_mod, "list_sessions", lambda project_dir, *a, **k: list(sessions or []))
    app = DeckApp(cfg, window_source=lambda: [], source=ob.make(cfg))
    claude = FakeProvider("claude", True)
    codex = FakeProvider("codex", False)
    return app, claude, codex, typed


def _screen_title(app) -> str:
    screen = app.screen
    if isinstance(screen, Pick):
        return screen.title_text
    if isinstance(screen, TextPrompt):
        return screen.title_text
    if isinstance(screen, ProjectPicker):
        return "project"
    return type(screen).__name__


async def pick_project(pilot, app, name: str):
    assert isinstance(app.screen, ProjectPicker)
    for ch in name[:4]:
        await pilot.press(ch)
    await pilot.pause()
    await pilot.press("enter")
    await pilot.pause()


async def wait_launch(app, provider, pilot, tries=40):
    for _ in range(tries):
        if provider.calls:
            break
        await pilot.pause(0.05)
    await pilot.pause()


@drives_the_screen
async def test_path_a_new_claude_with_choices_starts_with_them_as_flags(tmp_path, monkeypatch):
    cfg, root = make_cfg(tmp_path)
    app, claude, codex, typed = await start_deck(cfg, monkeypatch)
    async with app.run_test(size=(200, 50)) as pilot:
        await pilot.pause()
        app.sup.providers = {"claude": claude, "codex": codex}
        await pilot.press("n"); await pilot.pause()
        await pick_project(pilot, app, "habit")
        assert _screen_title(app).startswith("who works in habit_notes")
        await pilot.press("c"); await pilot.pause()
        assert _screen_title(app).startswith("model for habit_notes")
        await pilot.press("f"); await pilot.pause()                       # Fable family
        assert "Fable version" in _screen_title(app)
        await pilot.press("1"); await pilot.pause()                       # 5.1
        assert _screen_title(app).startswith("effort for habit_notes")
        await pilot.press("h"); await pilot.pause()                       # high
        assert _screen_title(app).startswith("permission mode for habit_notes")
        await pilot.press("a"); await pilot.pause()                       # auto
        assert _screen_title(app).startswith("first message (optional)")
        for ch in "read CHECKPOINT.md":
            await pilot.press(ch if ch != " " else "space")
        await pilot.press("enter")
        await wait_launch(app, claude, pilot)
    assert claude.calls and claude.calls[0]["options"] == {"start_model": "claude-fable-5-1", "start_effort": "high", "start_mode": "auto"}
    assert claude.calls[0]["interactive"] is True
    assert Path(claude.calls[0]["briefing"]).read_text(encoding="utf-8").strip() == "read CHECKPOINT.md"
    assert typed.calls == []   # nothing typed after launch


@drives_the_screen
async def test_path_b_stock_config_picks_up_the_new_session_effort_default(tmp_path, monkeypatch):
    """`make_cfg` builds a plain `config_mod.Config`, whose `[new_session]` defaults are model
    "default" (untouched) and effort "high" -- pressing
    straight Enter through model/effort/mode now starts with only `--effort high`, never "default -> Sonnet
    5 / low" silently."""
    cfg, root = make_cfg(tmp_path)
    app, claude, codex, typed = await start_deck(cfg, monkeypatch)
    async with app.run_test(size=(200, 50)) as pilot:
        await pilot.pause()
        app.sup.providers = {"claude": claude, "codex": codex}
        await pilot.press("n"); await pilot.pause()
        await pick_project(pilot, app, "loom")
        await pilot.press("c"); await pilot.pause()
        assert app.screen.current == "default"                             # model default untouched
        await pilot.press("enter"); await pilot.pause()   # model default
        assert app.screen.current == "high"                                # effort preselected from config
        await pilot.press("enter"); await pilot.pause()   # effort default (= "high")
        await pilot.press("enter"); await pilot.pause()   # mode default
        assert isinstance(app.screen, TextPrompt)
        await pilot.press("enter")
        await wait_launch(app, claude, pilot)
    assert claude.calls[0]["options"] == {"start_effort": "high"} and claude.calls[0]["briefing"] == ""
    assert typed.calls == []


@drives_the_screen
async def test_path_b2_default_new_session_config_types_nothing(tmp_path, monkeypatch):
    """Same walk, but with `[new_session]` explicitly set back to "default"/"default" -- the old
    all-defaults-types-nothing behaviour still exists for whoever configures it that way."""
    cfg, root = make_cfg(tmp_path)
    cfg = dataclasses.replace(cfg, new_session={"model": "default", "effort": "default"})
    app, claude, codex, typed = await start_deck(cfg, monkeypatch)
    async with app.run_test(size=(200, 50)) as pilot:
        await pilot.pause()
        app.sup.providers = {"claude": claude, "codex": codex}
        await pilot.press("n"); await pilot.pause()
        await pick_project(pilot, app, "loom")
        await pilot.press("c"); await pilot.pause()
        await pilot.press("enter"); await pilot.pause()   # model default
        await pilot.press("enter"); await pilot.pause()   # effort default
        await pilot.press("enter"); await pilot.pause()   # mode default
        assert isinstance(app.screen, TextPrompt)
        await pilot.press("enter")
        await wait_launch(app, claude, pilot)
    assert claude.calls[0]["options"] == {} and claude.calls[0]["briefing"] == ""
    assert typed.calls == []


@drives_the_screen
async def test_path_c_resume_lists_sessions_newest_first_and_types_nothing(tmp_path, monkeypatch):
    cfg, root = make_cfg(tmp_path)
    now = datetime.now(timezone.utc)
    found = [sessions_mod.SessionInfo("f8bd748c-aaaa", "Workspace checkpoints backfill and gating logic update", now),
             sessions_mod.SessionInfo("6f401d1a-bbbb", "Lapse audit", now)]
    app, claude, codex, typed = await start_deck(cfg, monkeypatch, sessions=found)
    async with app.run_test(size=(200, 50)) as pilot:
        await pilot.pause()
        app.sup.providers = {"claude": claude, "codex": codex}
        await pilot.press("n"); await pilot.pause()
        await pick_project(pilot, app, "habit")
        await pilot.press("r"); await pilot.pause()
        assert _screen_title(app).startswith("resume which session in habit_notes")
        options = app.screen.query_one(OptionList)
        assert "Workspace checkpoints backfill" in str(options.get_option_at_index(0).prompt)
        await pilot.press("1"); await pilot.pause()
        assert isinstance(app.screen, TextPrompt)                          # model/effort/mode skipped
        await pilot.press("enter")
        await wait_launch(app, claude, pilot)
    assert claude.calls[0]["options"] == {"resume": "f8bd748c-aaaa"}
    assert claude_command("claude", claude.calls[0]["options"]) == "claude --resume f8bd748c-aaaa"
    assert typed.calls == []


@drives_the_screen
async def test_path_d_codex_headless_needs_a_message_and_gets_the_flags(tmp_path, monkeypatch):
    cfg, root = make_cfg(tmp_path)
    app, claude, codex, typed = await start_deck(cfg, monkeypatch)
    async with app.run_test(size=(200, 50)) as pilot:
        await pilot.pause()
        app.sup.providers = {"claude": claude, "codex": codex}
        await pilot.press("n"); await pilot.pause()
        await pick_project(pilot, app, "alma")
        await pilot.press("x"); await pilot.pause()                        # headless
        assert _screen_title(app).startswith("model for almanac")
        await pilot.press("5"); await pilot.pause()                        # GPT-5.6 family
        await pilot.press("1"); await pilot.pause()                        # gpt-5.6-sol, its newest
        await pilot.press("h"); await pilot.pause()                        # high
        assert _screen_title(app).startswith("sandbox for almanac")
        await pilot.press("r"); await pilot.pause()                        # read-only
        assert _screen_title(app).startswith("first message (required")
        await pilot.press("enter"); await pilot.pause()
        for ch in "fix it":
            await pilot.press(ch if ch != " " else "space")
        await pilot.press("enter")
        await wait_launch(app, codex, pilot)
    assert codex.calls[0]["interactive"] is False
    assert codex.calls[0]["options"] == {"model": "gpt-5.6-sol", "effort": "high", "mode": "read-only"}
    assert Path(codex.calls[0]["briefing"]).read_text(encoding="utf-8").strip() == "fix it"


@drives_the_screen
async def test_path_f_escape_goes_back_one_step_and_keeps_answers(tmp_path, monkeypatch):
    cfg, root = make_cfg(tmp_path)
    app, claude, codex, typed = await start_deck(cfg, monkeypatch)
    async with app.run_test(size=(200, 50)) as pilot:
        await pilot.pause()
        app.sup.providers = {"claude": claude, "codex": codex}
        await pilot.press("n"); await pilot.pause()
        await pick_project(pilot, app, "habit")
        await pilot.press("c"); await pilot.pause()
        await pilot.press("o"); await pilot.pause()                        # Opus family
        await pilot.press("1"); await pilot.pause()                        # 5.5
        assert _screen_title(app).startswith("effort for")
        await pilot.press("escape"); await pilot.pause()
        assert _screen_title(app).startswith("model for")
        assert app.screen.current == "family:Opus"                         # the earlier pick is kept
        await pilot.press("escape"); await pilot.pause()
        assert _screen_title(app).startswith("who works in")
        await pilot.press("escape"); await pilot.pause()
        assert isinstance(app.screen, ProjectPicker)
        await pilot.press("escape"); await pilot.pause()
        assert not isinstance(app.screen, ModalScreen)
    assert claude.calls == [] and codex.calls == []


def test_sessions_are_listed_newest_first_with_their_titles(tmp_path):
    home = tmp_path / "claude-home"
    project = "C:\\Home\\x\\Documents\\Projects\\habit_notes"
    folder = home / "projects" / sessions_mod.slug_for(project)
    folder.mkdir(parents=True)
    assert folder.name == "C--Home-x-Documents-Projects-habit-notes"
    old = folder / "aaaa.jsonl"
    old.write_text(json.dumps({"type": "user", "message": {"content": "<local-command-caveat>x"}}) + "\n"
                   + json.dumps({"type": "user", "message": {"content": [{"type": "text", "text": "fix the hooks please"}]}}) + "\n",
                   encoding="utf-8")
    new = folder / "bbbb.jsonl"
    new.write_text(json.dumps({"type": "ai-title", "aiTitle": "first title"}) + "\n"
                   + json.dumps({"type": "ai-title", "aiTitle": "Workspace checkpoints backfill"}) + "\n",
                   encoding="utf-8")
    past = time.time() - 3600
    os.utime(old, (past, past))
    (folder / "notes.txt").write_text("ignored", encoding="utf-8")
    found = sessions_mod.list_sessions(project, claude_home=home)
    assert [s.session_id for s in found] == ["bbbb", "aaaa"]
    assert found[0].title == "Workspace checkpoints backfill" and found[1].title == "fix the hooks please"
    assert found[1].when_text.endswith("h ago") or found[1].when_text.endswith("m ago")
    assert sessions_mod.list_sessions("C:/nowhere", claude_home=home) == []


def test_open_cli_resume_flag_reaches_the_launch(tmp_path, monkeypatch):
    from pantheon.dispatch import projects as projects_mod
    cfg, root = make_cfg(tmp_path)
    claude = FakeProvider("claude", True)
    monkeypatch.setattr(projects_mod, "get_providers", lambda cfg_: {"claude": claude})
    monkeypatch.setattr(projects_mod.config_mod if hasattr(projects_mod, "config_mod") else config_mod, "load", lambda *a, **k: cfg)
    rc = projects_mod.main(["open", "habit_notes", "--resume", "f8bd748c-aaaa"])
    assert rc == 0 and claude.calls[0]["options"] == {"resume": "f8bd748c-aaaa"}
    rc = projects_mod.main(["open", "habit_notes", "--resume"])
    assert rc == 0 and claude.calls[1]["options"] == {"resume": True}
    assert claude_command("claude", {"resume": True}) == "claude --continue"


@drives_the_screen
async def test_who_picker_labels_fit_and_read_on_the_phone(tmp_path, monkeypatch):
    """: at 65 columns the who labels clipped into fragments ("codex -on a phone
    needs ..."). Below DESK_AT the step swaps in short labels; every rendered line must fit the
    phone modal's inner width."""
    cfg, root = make_cfg(tmp_path)
    app, claude, codex, typed = await start_deck(cfg, monkeypatch)
    async with app.run_test(size=(65, 26)) as pilot:
        await pilot.pause()
        app.sup.providers = {"claude": claude, "codex": codex}
        await pilot.press("n"); await pilot.pause()
        await pick_project(pilot, app, "habit")
        assert _screen_title(app).startswith("who works in habit_notes")
        from pantheon.widgets.modal import PHONE_WIDTH
        ol = app.screen.query_one(OptionList)
        lines = [str(ol.get_option_at_index(i).prompt) for i in range(ol.option_count)]
        assert any("Codex headless" in l for l in lines)
        assert any("desk only" in l for l in lines)
        for l in lines:
            assert len(l) <= PHONE_WIDTH - 6, l
