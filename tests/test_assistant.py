"""The pinned Assistant: ensure opens it once and resumes the
saved conversation after, a missing window comes back, the sidebar line stages it, the pit skips
it, and one setting turns it all off. No real tmux and no real `claude`: a small fake tmux server
answers the handful of calls `pantheon/assistant.py` makes, and a fake provider stands in for
`providers/claude.py` and records what it was asked to launch."""
from __future__ import annotations

import asyncio
import dataclasses
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

from textual.app import App, ComposeResult
from textual.widgets import ListView

from pantheon import assistant as assistant_mod
from pantheon import config as config_mod
from pantheon import tmuxctl
from pantheon.models import AgentState, AgentStatus, LaunchResult, TmuxWindow, utcnow_iso
from pantheon.pit import tmux_pit
from pantheon.session_view import models as session_models
from pantheon.session_view.models import SessionEntry
from pantheon.supervisor.rail import ASSISTANT_LABEL, SessionSidebar

ROOT = "C:/Home/x/Documents/Projects"


class FakeTmux:
    """One `pantheon` session. `panes` maps pane id -> [window_index, window_name, command]."""

    def __init__(self, alive: bool = True):
        self.alive = alive
        self.panes: dict[str, list] = {"%0": [0, "DECK", "python"]}
        self.killed: list[int] = []
        self._next = 10

    def add(self, name: str, command: str = "node") -> tuple[str, int]:
        pane = f"%{self._next}"
        index = max(p[0] for p in self.panes.values()) + 1
        self._next += 1
        self.panes[pane] = [index, name, command]
        return pane, index

    def __call__(self, *args, tmux=None, check=False):
        ok = SimpleNamespace(returncode=0, stdout="", stderr="")
        bad = SimpleNamespace(returncode=1, stdout="", stderr="no server")
        if not self.alive:
            return bad
        cmd = args[0]
        if cmd == "has-session":
            return ok
        if cmd == "list-panes" and "-s" in args:
            lines = [f"{p}|{i}|{n}|{c}" for p, (i, n, c) in self.panes.items()]
            return SimpleNamespace(returncode=0, stdout="\n".join(lines), stderr="")
        if cmd == "kill-window":
            target = args[args.index("-t") + 1]
            index = int(target.rsplit(":", 1)[1])
            self.killed.append(index)
            self.panes = {p: v for p, v in self.panes.items() if v[0] != index}
            return ok
        return bad


class FakeProvider:
    """Records each launch; a launch opens an `ASSISTANT` pane in the fake tmux running `node`
    (or, with `fail_resume`, a resume falls back to a bare shell and fails, as a gone id does)."""

    name = "claude"

    def __init__(self, fake: FakeTmux, fail_resume: bool = False):
        self.fake = fake
        self.fail_resume = fail_resume
        self.calls: list[dict] = []

    def capabilities(self):
        return {"interactive_tmux"}

    def launch(self, project_dir, briefing_path, interactive=True, options=None):
        options = dict(options or {})
        self.calls.append({"folder": project_dir, **options})
        if self.fail_resume and options.get("resume"):
            pane, index = self.fake.add(options.get("window_name", "x"), "bash")
            return LaunchResult(False, "tmux", index, "ASSISTANT", message="claude did not start")
        pane, index = self.fake.add(options.get("window_name", "x"), "node")
        return LaunchResult(True, "tmux", index, options.get("window_name", "x"), tmux_pane=pane,
                            message="claude is open")


def make_cfg(tmp_path, **assistant) -> config_mod.Config:
    cfg = config_mod.Config(state_dir=str(tmp_path / "state"), projects_root=ROOT,
                            claude_home=str(tmp_path / "claude_home"))
    if assistant:
        cfg = dataclasses.replace(cfg, assistant=assistant)
    cfg.events_file.parent.mkdir(parents=True, exist_ok=True)
    return cfg


def session_start(cfg, session_id: str, pane: str, transcript: str = "", detail: str = "startup") -> None:
    from pantheon import eventcache
    line = {"ts": utcnow_iso(), "source": "claude", "event": "SessionStart", "session_id": session_id,
            "cwd": ROOT, "detail": detail, "tmux_pane": pane, "transcript_path": transcript}
    with open(cfg.events_file, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(line) + "\n")
    eventcache._cache = None


def install(monkeypatch, fake: FakeTmux) -> None:
    monkeypatch.setattr(tmuxctl, "run", fake)


def no_sleep(_seconds):
    pass


# --------------------------------------------------------------------------- ensure


def test_ensure_opens_the_window_once_then_learns_and_keeps_the_conversation_id(tmp_path, monkeypatch):
    fake = FakeTmux()
    install(monkeypatch, fake)
    cfg = make_cfg(tmp_path)
    provider = FakeProvider(fake)

    first = assistant_mod.ensure(cfg, provider=provider, sleep=no_sleep, learn_wait=0)
    assert first["action"] == "started"
    # No --resume / --continue; model + effort from [assistant];
    # a NEW conversation gets the configured name.
    assert provider.calls == [{"folder": ROOT, "window_name": "ASSISTANT", "start_model": "claude-opus-5-5",
                               "start_effort": "medium", "name": "Pantheon Assistant"}]
    pane = first["pane_id"]
    assert assistant_mod.read_state(cfg)["pane_id"] == pane

    session_start(cfg, "conv-1", pane)
    assert assistant_mod.learn(cfg) == "conv-1"
    assert assistant_mod.read_state(cfg)["session_id"] == "conv-1"

    second = assistant_mod.ensure(cfg, provider=provider, sleep=no_sleep, learn_wait=0)
    assert second["action"] == "running"
    assert len(provider.calls) == 1                      # nothing opened a second time


def test_a_stale_pane_id_from_an_old_server_never_closes_a_deck_window(tmp_path, monkeypatch):
    """2026-09-27 13:03:40: a fresh tmux server reused pane ids from %0, the saved Assistant pane
    id matched the new BUDGET pane (a bash wrapper), and ensure closed BUDGET as a "bare shell"."""
    fake = FakeTmux()
    fake.panes = {"%0": [0, "DECK", "python"], "%1": [1, "QUEUE", "bash"], "%2": [2, "BUDGET", "bash"]}
    install(monkeypatch, fake)
    cfg = make_cfg(tmp_path)
    assistant_mod.write_state(cfg, {"session_id": "", "pane_id": "%2", "started_at": "2026-09-27T05:34:08Z",
                                    "folder": ROOT})
    assert assistant_mod.locate(cfg) is None
    result = assistant_mod.ensure(cfg, provider=FakeProvider(fake), sleep=no_sleep, learn_wait=0)
    assert result["action"] == "started"
    assert fake.killed == [] and fake.panes["%2"][1] == "BUDGET"


def test_clear_shell_window_refuses_any_window_not_named_assistant(tmp_path, monkeypatch):
    fake = FakeTmux()
    fake.panes["%2"] = [2, "BUDGET", "bash"]
    install(monkeypatch, fake)
    cfg = make_cfg(tmp_path)
    place = assistant_mod.Place("%2", 2, "BUDGET", "bash")
    assert assistant_mod._clear_shell_window(cfg, place, None) is False
    assert fake.killed == []


def test_a_running_assistant_window_with_no_record_is_adopted_and_its_id_learned(tmp_path, monkeypatch):
    fake = FakeTmux()
    install(monkeypatch, fake)
    cfg = make_cfg(tmp_path)
    pane, index = fake.add("ASSISTANT", "node")            # opened before the record existed
    session_start(cfg, "conv-early", pane)
    provider = FakeProvider(fake)
    result = assistant_mod.ensure(cfg, provider=provider, sleep=no_sleep, learn_wait=0)
    assert result["action"] == "running" and provider.calls == []
    state = assistant_mod.read_state(cfg)
    assert state["pane_id"] == pane and state["session_id"] == "conv-early"


def test_a_session_start_from_another_pane_or_before_the_start_is_not_learned(tmp_path, monkeypatch):
    fake = FakeTmux()
    install(monkeypatch, fake)
    cfg = make_cfg(tmp_path)
    session_start(cfg, "older-desktop", "%10")            # before the Assistant was opened
    result = assistant_mod.ensure(cfg, provider=FakeProvider(fake), sleep=no_sleep, learn_wait=0)
    session_start(cfg, "someone-else", "%99")
    assert assistant_mod.learn(cfg) is None
    session_start(cfg, "mine", result["pane_id"])
    assert assistant_mod.learn(cfg) == "mine"


def test_a_clear_inside_the_assistant_moves_the_saved_id_to_the_new_conversation(tmp_path, monkeypatch):
    fake = FakeTmux()
    install(monkeypatch, fake)
    cfg = make_cfg(tmp_path)
    pane = assistant_mod.ensure(cfg, provider=FakeProvider(fake), sleep=no_sleep, learn_wait=0)["pane_id"]
    session_start(cfg, "conv-1", pane)
    session_start(cfg, "conv-2", pane, detail="clear")
    assert assistant_mod.learn(cfg) == "conv-2"


def test_a_missing_window_comes_back_resuming_the_saved_conversation(tmp_path, monkeypatch):
    fake = FakeTmux()
    install(monkeypatch, fake)
    cfg = make_cfg(tmp_path)
    transcript = tmp_path / "claude_home" / "conv-1.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("{}\n", encoding="utf-8")
    provider = FakeProvider(fake)
    pane = assistant_mod.ensure(cfg, provider=provider, sleep=no_sleep, learn_wait=0)["pane_id"]
    session_start(cfg, "conv-1", pane, transcript=str(transcript))
    assistant_mod.learn(cfg)

    fake.panes = {"%0": [0, "DECK", "python"]}            # a restart / a dead server: the window is gone
    back = assistant_mod.ensure(cfg, provider=provider, sleep=no_sleep, learn_wait=0)
    assert back["action"] == "resumed"
    # A resume carries model + effort again but never a name: an existing conversation keeps its own.
    assert provider.calls[-1] == {"folder": ROOT, "window_name": "ASSISTANT", "resume": "conv-1",
                                  "start_model": "claude-opus-5-5", "start_effort": "medium"}
    assert "same conversation" in back["message"]
    state = assistant_mod.read_state(cfg)
    assert state["session_id"] == "conv-1" and state["pane_id"] == back["pane_id"] != pane


def test_a_saved_conversation_whose_file_is_gone_starts_fresh_and_says_so(tmp_path, monkeypatch):
    fake = FakeTmux()
    install(monkeypatch, fake)
    cfg = make_cfg(tmp_path)
    assistant_mod.write_state(cfg, {"session_id": "gone-1", "pane_id": "%77",
                                    "started_at": utcnow_iso(), "transcript_path": str(tmp_path / "nope.jsonl")})
    provider = FakeProvider(fake)
    result = assistant_mod.ensure(cfg, provider=provider, sleep=no_sleep, learn_wait=0)
    assert result["action"] == "fresh"
    assert result["message"] == assistant_mod.MSG_FRESH_MISSING
    assert "resume" not in provider.calls[-1]
    assert assistant_mod.read_state(cfg)["session_id"] is None      # the new id is learned next
    session_start(cfg, "new-1", result["pane_id"])
    assert assistant_mod.learn(cfg) == "new-1"


def test_a_resume_that_does_not_come_up_falls_back_to_a_fresh_conversation(tmp_path, monkeypatch):
    fake = FakeTmux()
    install(monkeypatch, fake)
    cfg = make_cfg(tmp_path)
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    assistant_mod.write_state(cfg, {"session_id": "conv-1", "pane_id": "%77", "started_at": utcnow_iso(),
                                    "transcript_path": str(transcript)})
    provider = FakeProvider(fake, fail_resume=True)
    result = assistant_mod.ensure(cfg, provider=provider, sleep=no_sleep, learn_wait=0)
    assert result["action"] == "fresh" and result["message"] == assistant_mod.MSG_FRESH_FAILED
    assert [("resume" in c) for c in provider.calls] == [True, False]
    assert len(fake.killed) == 1                           # the shell-only window it left was closed
    names = [v[1] for v in fake.panes.values()]
    assert names.count("ASSISTANT") == 1


def test_an_assistant_window_left_at_a_shell_is_replaced_not_duplicated(tmp_path, monkeypatch):
    fake = FakeTmux()
    install(monkeypatch, fake)
    cfg = make_cfg(tmp_path)
    pane, index = fake.add("ASSISTANT", "bash")            # Claude was exited; the shell stayed
    provider = FakeProvider(fake)
    result = assistant_mod.ensure(cfg, provider=provider, sleep=no_sleep, learn_wait=0)
    assert result["action"] == "started"
    assert fake.killed == [index]
    assert [v[1] for v in fake.panes.values()].count("ASSISTANT") == 1


def test_an_assistant_window_running_something_else_is_left_alone(tmp_path, monkeypatch):
    fake = FakeTmux()
    install(monkeypatch, fake)
    cfg = make_cfg(tmp_path)
    fake.add("ASSISTANT", "vim")
    provider = FakeProvider(fake)
    result = assistant_mod.ensure(cfg, provider=provider, sleep=no_sleep, learn_wait=0)
    assert result["action"] == "busy" and not result["ok"]
    assert provider.calls == [] and fake.killed == []


def test_no_tmux_means_nothing_is_tried_and_nothing_is_said(tmp_path, monkeypatch):
    fake = FakeTmux(alive=False)
    install(monkeypatch, fake)
    cfg = make_cfg(tmp_path)
    provider = FakeProvider(fake)
    result = assistant_mod.ensure(cfg, provider=provider, sleep=no_sleep, learn_wait=0)
    assert result == {"ok": True, "action": "none", "message": ""}
    assert provider.calls == []


def test_a_start_already_in_progress_is_not_started_twice(tmp_path, monkeypatch):
    fake = FakeTmux()
    install(monkeypatch, fake)
    cfg = make_cfg(tmp_path)
    assert assistant_mod._lock(cfg)
    provider = FakeProvider(fake)
    result = assistant_mod.ensure(cfg, provider=provider, sleep=no_sleep, learn_wait=0)
    assert result["action"] == "starting" and provider.calls == []
    assert assistant_mod.starting(cfg)


def test_the_off_switch_turns_everything_off(tmp_path, monkeypatch):
    fake = FakeTmux()
    install(monkeypatch, fake)
    cfg = make_cfg(tmp_path, enabled=False)
    provider = FakeProvider(fake)
    assert assistant_mod.ensure(cfg, provider=provider)["action"] == "disabled"
    assert provider.calls == []
    assert assistant_mod.view(cfg, [], []) is None


def test_the_folder_setting_defaults_to_the_workspace_root_and_can_be_changed(tmp_path):
    assert make_cfg(tmp_path).assistant_settings().folder == ROOT
    assert make_cfg(tmp_path, folder="C:/x/loom-os").assistant_settings().folder == "C:/x/loom-os"
    toml = tmp_path / "pantheon.toml"
    toml.write_text('[assistant]\nenabled = "off"\n', encoding="utf-8")
    assert config_mod.load(toml).assistant_settings().enabled is False


# --------------------------------------------------------------------------- the pit


def test_the_pit_never_pulls_the_assistant_in(monkeypatch):
    windows = [TmuxWindow(0, "DECK", "python", ROOT, "%0", "pantheon"),
               TmuxWindow(3, "loom-os", "node", ROOT, "%3", "pantheon"),
               TmuxWindow(4, "ASSISTANT", "node", ROOT, "%4", "pantheon")]
    monkeypatch.setattr(tmuxctl, "list_windows", lambda session=None, tmux=None: windows)
    assert "ASSISTANT" in tmux_pit.RESERVED_WINDOWS
    assert [w.name for w in tmux_pit.agent_windows("pantheon")] == ["loom-os"]


# --------------------------------------------------------------------------- the sidebar line


def _row(session_id, pane, status, index=4):
    return AgentState(session_id=session_id, provider="claude", status=status, cwd=ROOT,
                      window_index=index, tmux_pane=pane, in_pantheon=True)


def test_the_line_says_what_the_assistant_is_doing(tmp_path):
    cfg = make_cfg(tmp_path)
    assistant_mod.write_state(cfg, {"session_id": "conv-1", "pane_id": "%4", "started_at": utcnow_iso()})
    window = [TmuxWindow(4, "ASSISTANT", "node", ROOT, "%4", "pantheon")]
    assert assistant_mod.view(cfg, [], []).status == "not running"
    assert assistant_mod.view(cfg, [], window).status == "idle"
    assert assistant_mod.view(cfg, [_row("conv-1", "%4", AgentStatus.WORKING)], window).status == "working"
    needs = assistant_mod.view(cfg, [_row("conv-1", "%4", AgentStatus.BLOCKED_PERMISSION)], window)
    assert needs.status == "needs you" and needs.window_index == 4 and needs.running
    assert assistant_mod.view(cfg, [], window, is_starting=True).status == "starting"


ENTRIES = [
    SessionEntry(session_id="conv-1", group=session_models.LIVE, project="workspace", cwd=ROOT,
                 title="the assistant itself", status_text="idle", window_index=4, tmux_session="pantheon"),
    SessionEntry(session_id="other-1", group=session_models.LIVE, project="workspace", cwd=ROOT,
                 title="another root session", status_text="working", window_index=5, tmux_session="pantheon"),
]


def test_the_sidebar_line_sits_under_the_top_lines_is_not_listed_twice_and_posts_on_enter(tmp_path):
    cfg = make_cfg(tmp_path)
    view = assistant_mod.View("idle", "dim", "conv-1", "%4", 4, frozenset({"conv-1"}))
    got = []

    class _App(App):
        def __init__(self):
            super().__init__()
            self.sidebar = SessionSidebar(cfg, rows_source=lambda: [], entries_source=lambda _r: ENTRIES,
                                          assistant_source=lambda: view, id="rail")

        def compose(self) -> ComposeResult:
            yield self.sidebar

        def on_session_sidebar_assistant_chosen(self, event) -> None:
            got.append("assistant")

    async def drive():
        app = _App()
        async with app.run_test(size=(40, 20)) as pilot:
            await pilot.pause()
            items = list(app.sidebar._items)
            got.append(items)
            got.append(app.sidebar._row_text(items[2]).plain)
            list_view = app.query_one("#sidebar-list", ListView)
            list_view.focus()
            list_view.index = 2
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()

    asyncio.run(drive())
    items = got[0]
    assert [s[0] for s in items[:3]] == ["new", "edit", "assistant"]
    listed = [s[1].session_id for s in items if s[0] == "session"]
    assert listed == ["other-1"]                            # the Assistant is not a second row
    assert got[1] == f"{ASSISTANT_LABEL}  idle"
    assert got[2] == "assistant"


def test_the_line_says_on_screen_while_the_assistant_is_staged(tmp_path):
    cfg = make_cfg(tmp_path)
    view = assistant_mod.View("idle", "dim", "conv-1", "%4", 0, frozenset({"conv-1"}))
    seen = []

    class _App(App):
        def __init__(self):
            super().__init__()
            self.sidebar = SessionSidebar(cfg, rows_source=lambda: [], entries_source=lambda _r: [],
                                          assistant_source=lambda: view, id="rail")

        def compose(self) -> ComposeResult:
            yield self.sidebar

    async def drive():
        app = _App()
        async with app.run_test(size=(40, 20)) as pilot:
            await pilot.pause()
            app.sidebar.set_stage(True, "conv-1", "%4")
            await pilot.pause()
            spec = next(s for s in app.sidebar._items if s[0] == "assistant")
            seen.append(app.sidebar._row_text(spec).plain)

    asyncio.run(drive())
    assert seen == [f"{ASSISTANT_LABEL}  on screen"]


def test_a_powershell_left_in_the_window_counts_as_a_bare_shell(tmp_path, monkeypatch):
    fake = FakeTmux()
    install(monkeypatch, fake)
    cfg = make_cfg(tmp_path)
    _pane, index = fake.add("ASSISTANT", "C:/Program Files/PowerShell/7/pwsh.exe")
    result = assistant_mod.ensure(cfg, provider=FakeProvider(fake), sleep=no_sleep, learn_wait=0)
    assert result["action"] == "started" and fake.killed == [index]


def test_no_assistant_source_means_no_line(tmp_path):
    cfg = make_cfg(tmp_path)

    class _App(App):
        def __init__(self):
            super().__init__()
            self.sidebar = SessionSidebar(cfg, rows_source=lambda: [], entries_source=lambda _r: ENTRIES, id="rail")

        def compose(self) -> ComposeResult:
            yield self.sidebar

    seen = []

    async def drive():
        app = _App()
        async with app.run_test(size=(40, 20)) as pilot:
            await pilot.pause()
            seen.extend(s[0] for s in app.sidebar._items)

    asyncio.run(drive())
    assert "assistant" not in seen


# --------------------------------------------------------------------------- the deck


FIXTURES = Path(__file__).parent / "fixtures"


def _deck(tmp_path, windows, **assistant):
    from pantheon.deck.app import DeckApp
    from pantheon.tasks import obsidian_base as ob

    projects = tmp_path / "Projects"
    shutil.copytree(FIXTURES / "sprints", projects / "Sprints")
    shutil.copy(FIXTURES / "Sprints.base", projects / "Sprints.base")
    tools_page = {k: str(tmp_path / "tools" / k) for k in
                  ("claude_skills_dir", "agents_skills_dir", "claude_settings", "codex_config", "codex_hooks")}
    cfg = config_mod.Config(vault=str(tmp_path), state_dir=str(tmp_path / "state"), projects_root=ROOT,
                            tools_page=tools_page, assistant=assistant)
    cfg.events_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.events_file.write_text("", encoding="utf-8")
    return cfg, DeckApp(cfg, window_source=lambda: windows, source=ob.make(cfg))


def test_A_on_the_deck_puts_a_running_assistant_beside_the_list(tmp_path, monkeypatch):
    windows = [TmuxWindow(6, "ASSISTANT", "node", ROOT, "%6", "pantheon")]
    cfg, app = _deck(tmp_path, windows)
    assistant_mod.write_state(cfg, {"session_id": "conv-1", "pane_id": "%6", "started_at": utcnow_iso()})
    shown = []
    monkeypatch.setattr(type(app), "_show_session", lambda self, index, sid="": shown.append((index, sid)))

    async def drive():
        async with app.run_test(size=(200, 50)) as pilot:
            await pilot.pause()
            app.sup.refresh_rows()
            await pilot.pause()
            await pilot.press("A")
            await pilot.pause()

    asyncio.run(drive())
    assert shown == [(6, "conv-1")]


def test_A_starts_the_assistant_when_it_is_not_running_then_puts_it_on_screen(tmp_path, monkeypatch):
    cfg, app = _deck(tmp_path, [])
    calls = []

    def fake_ensure(cfg_, provider=None, on_starting=None, **kw):
        calls.append("ensure")
        return {"ok": True, "action": "started", "message": "the Assistant is open in window 7",
                "window_index": 7, "session_id": ""}

    monkeypatch.setattr(assistant_mod, "ensure", fake_ensure)
    shown = []
    monkeypatch.setattr(type(app), "_show_session", lambda self, index, sid="": shown.append(index))

    async def drive():
        async with app.run_test(size=(200, 50)) as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            calls.clear()                                  # the start-of-deck ensure
            await pilot.press("A")
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()

    asyncio.run(drive())
    assert calls == ["ensure"] and shown == [7]


def test_the_deck_calls_ensure_when_it_starts_inside_tmux_unless_switched_off(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(assistant_mod, "ensure",
                        lambda cfg_, **kw: calls.append("ensure") or {"ok": True, "action": "none", "message": ""})
    for enabled, inside, expected in ((True, True, ["ensure"]), (False, True, []), (True, False, [])):
        # Outside tmux (a render, a script, a Desktop-app session) the deck never starts it: a
        # render once opened a real ASSISTANT window in the live server that way.
        monkeypatch.setattr(tmuxctl, "in_tmux", lambda inside=inside: inside)
        calls.clear()
        sub = tmp_path / f"{enabled}-{inside}"
        sub.mkdir()
        _cfg, app = _deck(sub, [], enabled=enabled)

        async def drive():
            async with app.run_test(size=(200, 50)) as pilot:
                await pilot.pause()
                await app.workers.wait_for_complete()

        asyncio.run(drive())
        assert calls == expected


def test_the_assistant_starts_with_the_shared_dispatch_memory(tmp_path, monkeypatch):
    """: the Assistant shares Dispatch's memory, read and write."""
    memory = tmp_path / "dispatch" / "agent" / "memory"
    memory.mkdir(parents=True)
    (memory / "MEMORY.md").write_text("- [Style](style.md) -- tight lists\n", encoding="utf-8")
    fake = FakeTmux()
    install(monkeypatch, fake)
    cfg = make_cfg(tmp_path, memory=str(memory))
    provider = FakeProvider(fake)
    assistant_mod.ensure(cfg, provider=provider, sleep=no_sleep, learn_wait=0)
    call = provider.calls[0]
    assert call["add_dirs"] == [str(memory)]
    prompt = Path(call["append_system_prompt_file"])
    assert prompt.parent == Path(cfg.state_dir)
    assert memory.as_posix() in prompt.read_text(encoding="utf-8")
    from pantheon.providers.claude import claude_command
    cmd = claude_command("claude", call)
    assert f'--add-dir "{memory}"' in cmd and "--append-system-prompt-file" in cmd
    assert "autoMemoryDirectory" in cmd and memory.as_posix() in cmd   # one store, not two


def test_memory_off_means_no_shared_memory(tmp_path, monkeypatch):
    fake = FakeTmux()
    install(monkeypatch, fake)
    cfg = make_cfg(tmp_path, memory="off")
    provider = FakeProvider(fake)
    assistant_mod.ensure(cfg, provider=provider, sleep=no_sleep, learn_wait=0)
    assert "add_dirs" not in provider.calls[0] and "append_system_prompt_file" not in provider.calls[0]


def test_memory_is_found_in_the_store_apps_redirected_folder_first(tmp_path, monkeypatch):
    """Claude Desktop is a Store app: its real memory folder is under LocalAppData\Packages."""
    packaged = tmp_path / "Packages" / "Claude_x" / "LocalCache" / "Roaming" / "Claude" / "local-agent-mode-sessions"
    (packaged / "org" / "acct" / "agent" / "memory").mkdir(parents=True)
    (packaged / "org" / "acct" / "agent" / "memory" / "MEMORY.md").write_text("- x\n", encoding="utf-8")
    monkeypatch.setattr(assistant_mod, "DISPATCH_SESSION_ROOTS", [packaged, tmp_path / "plain-appdata"])
    settings = config_mod.Config().assistant_settings()
    assert assistant_mod.memory_dir(settings) == packaged / "org" / "acct" / "agent" / "memory"
