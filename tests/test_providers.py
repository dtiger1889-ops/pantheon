"""Provider adapters: one interface, two files; a fake tmux so no server is touched."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from pantheon import config as config_mod
from pantheon import providers as registry
from pantheon import tmuxctl
from pantheon.providers import base, claude as claude_mod, codex as codex_mod, ollama as ollama_mod


def cfg_for(tmp_path, **kw):
    kw.setdefault("state_dir", str(tmp_path / "state"))
    kw.setdefault("providers", {"claude": True, "codex": True, "ollama": True})
    return config_mod.Config(**kw)


def test_registry_builds_every_enabled_adapter_and_rejects_unknown_names(tmp_path):
    cfg = cfg_for(tmp_path)
    got = registry.get_providers(cfg)
    assert set(got) == {"claude", "codex", "ollama"}
    for p in got.values():
        assert isinstance(p, base.Provider)
    with pytest.raises(ValueError) as err:
        registry.get_providers(cfg_for(tmp_path, providers={"gemini": True}))
    assert "allowed: claude, codex, ollama" in str(err.value)


def test_capability_sets_match_the_spec(tmp_path):
    cfg = cfg_for(tmp_path, codex_home=str(tmp_path / "no-codex"))
    assert claude_mod.ClaudeProvider(cfg).capabilities() == {"interactive_tmux"}
    assert codex_mod.CodexProvider(cfg).capabilities() == set()  # never interactive_tmux
    assert ollama_mod.OllamaProvider(cfg).capabilities() == set()  # never interactive_tmux either


FIXTURE = Path(__file__).parent / "fixtures"


def test_ollama_list_models_never_errors_when_nothing_is_listening():
    models, note = ollama_mod.list_models("http://127.0.0.1:9/api/tags", timeout=0.2)
    assert models == [] and note == "ollama not running"


def test_ollama_readiness_reports_each_gate_in_plain_words(monkeypatch):
    # gate 1: nothing answers on the tags URL
    ready, message = ollama_mod.readiness("http://127.0.0.1:9/api/tags", which=lambda n: None)
    assert ready is False and "ollama not running" in message and "ollama pull" in message

    # gate 2: ollama answers, but no models are pulled
    monkeypatch.setattr(ollama_mod, "list_models", lambda url, timeout=1.5: ([], ""))
    ready, message = ollama_mod.readiness(which=lambda n: None)
    assert ready is False and "no local model pulled yet" in message and "ollama pull" in message

    # gate 3: a model exists, but neither qwen nor goose is on PATH
    monkeypatch.setattr(ollama_mod, "list_models", lambda url, timeout=1.5: (["qwen2.5-coder:7b"], ""))
    ready, message = ollama_mod.readiness(which=lambda n: None)
    assert ready is False and "no local runner found" in message

    # all three gates pass
    ready, message = ollama_mod.readiness(which=lambda n: "/usr/bin/qwen" if n == "qwen" else None)
    assert ready is True and message == ""


def test_ollama_launch_reports_not_ready_instead_of_raising(tmp_path):
    result = ollama_mod.OllamaProvider(cfg_for(tmp_path), which=lambda n: None).launch("C:/x", "C:/y.md", False)
    assert result.ok is False and result.where == "headless"
    assert "ollama not running" in result.message  # never a traceback


def test_ollama_launch_opens_a_headless_tmux_window_once_ready(tmp_path, monkeypatch):
    cfg = cfg_for(tmp_path)
    fake = FakeTmux()
    monkeypatch.setattr(tmuxctl, "run", fake.run)
    monkeypatch.setattr(ollama_mod, "list_models", lambda url, timeout=1.5: (["qwen2.5-coder:7b"], ""))
    provider = ollama_mod.OllamaProvider(cfg, which=lambda n: "/usr/bin/qwen" if n == "qwen" else None)
    result = provider.launch("C:/Home/x/Documents/Projects/hiking_log_v2", "C:/b.md", False)
    assert result.ok and result.where == "headless" and result.job_id.startswith("ollama-")
    new_window = next(c for c in fake.calls if c[0] == "new-window")
    assert "ollama:hiking_log" in new_window
    command = new_window[-1]
    assert "-m pantheon.dispatch.ollama_job" in command and '--runner "qwen"' in command
    assert result.job_id in command


def test_ollama_job_argv_picks_the_runner_and_rejects_unknown_ones():
    from pantheon.dispatch import ollama_job

    assert ollama_job.runner_argv("qwen") == ["qwen", "--input-format", "text", "--yolo"]
    assert ollama_job.runner_argv("goose") == ["goose", "run", "-t", "-"]
    with pytest.raises(ValueError):
        ollama_job.runner_argv("gemini")


def test_ollama_job_records_queued_running_done_and_keeps_a_log(tmp_path, monkeypatch):
    """A stand-in for the runner (the venv python) proves the wrapper's bookkeeping end to end."""
    import sys

    from pantheon.dispatch import ollama_job

    cfg = cfg_for(tmp_path)
    monkeypatch.setattr(ollama_job, "runner_argv",
                        lambda runner: [sys.executable, "-c", "import sys; print('echo:' + sys.stdin.read().strip())"])
    brief = tmp_path / "b.md"
    brief.write_text("do the local thing", encoding="utf-8")
    code = ollama_job.run(cfg, str(brief), str(tmp_path), "ollama-test", "qwen", "hiking_log_v2-x",
                          wait_for_enter=False)
    assert code == 0
    assert (cfg.dispatch_dir / "ollama-test.log").read_text(encoding="utf-8").strip() == "echo:do the local thing"
    names = [json.loads(l)["event"] for l in open(cfg.events_file, encoding="utf-8")]
    assert names == ["queued", "running", "done"]
    last = json.loads(open(cfg.events_file, encoding="utf-8").read().splitlines()[-1])
    assert last["source"] == "ollama" and last["job_id"] == "ollama-test" and last["tracker_id"] == "hiking_log_v2-x"
    assert last["detail"] == "exit code 0"


# ---------------------------------------------------------------- claude launch, against a fake tmux


class FakeTmux:
    """Records every tmux call and plays back a scripted pane."""

    def __init__(self, screens=None, commands=None):
        self.calls = []
        self.screens = list(screens or [])
        self.commands = list(commands or [])

    def run(self, *args, tmux=None, check=False):
        import subprocess

        self.calls.append(args)
        out = ""
        if args[0] == "new-window":
            out = "4\n"
        elif args[0] == "display-message":
            fmt = args[-1]
            if "pane_current_command" in fmt:
                out = (self.commands.pop(0) if len(self.commands) > 1 else self.commands[0]) + "\n"
            elif "pane_id" in fmt:
                out = "%9\n"
        elif args[0] == "capture-pane":
            out = self.screens.pop(0) if len(self.screens) > 1 else (self.screens[0] if self.screens else "")
        return subprocess.CompletedProcess(args, 0, out, "")


def _typed(fake: FakeTmux) -> list[str]:
    """Typed lines (`send-keys -l`) and pasted briefings (`set-buffer ... -- text`), in order."""
    return [c[4] for c in fake.calls if (c[0] == "send-keys" and "-l" in c) or c[0] == "set-buffer"]


def test_claude_launch_pastes_a_long_briefing_whole(tmp_path, monkeypatch):
    """A 4k briefing goes in as one bracketed paste, not typed key by key (2026-10-29/30: long
    briefs were never submitted or arrived as their tails)."""
    cfg = cfg_for(tmp_path)
    fake = FakeTmux(screens=["starting...", "│ > \n"], commands=["bash", "bash", "node"])
    monkeypatch.setattr(tmuxctl, "run", fake.run)
    brief = tmp_path / "b.md"
    brief.write_text("word " * 800, encoding="utf-8")
    result = claude_mod.ClaudeProvider(cfg, sleep=lambda s: None).launch("C:/p", str(brief), True)
    assert result.ok
    sets = [c for c in fake.calls if c[0] == "set-buffer"]
    pastes = [c for c in fake.calls if c[0] == "paste-buffer"]
    assert len(sets) == 1 and sets[0][4] == ("word " * 800).strip()
    assert len(pastes) == 1 and "-p" in pastes[0] and sets[0][2] == pastes[0][pastes[0].index("-b") + 1]
    assert not any(c[4] == sets[0][4] for c in fake.calls if c[0] == "send-keys" and "-l" in c)


# ------------------------------------------------- briefing delivery: ready, verify, retry

PROJECT = "C:/Users/x/Documents/Claude/brief_probe"
BOX = "───\n> Try \"fix typecheck errors\"\n───\n  5h 2% · wk 78%\n"
SESSION_START = '{"type":"attachment","attachment":{"type":"hook_success"}}\n'
USER_LINE = '{"type":"user","message":{"role":"user","content":"the brief"}}\n'


class DeliveryTmux(FakeTmux):
    """A Claude pane whose startup and delivery are scripted against a real transcript folder:
    the transcript appears after `start_after` captures; `deliver_on` names which brief-Enter
    (1 = the first after a paste) makes Claude write the user message, `working_on` which one
    makes the pane show Claude working instead; None = never."""

    def __init__(self, folder: Path, start_after=0, deliver_on=1, working_on=None, box=BOX):
        super().__init__(screens=[box], commands=["node"])
        self.folder, self.start_after = folder, start_after
        self.deliver_on, self.working_on = deliver_on, working_on
        self.captures = 0
        self.pasted = False
        self.brief_enters = 0
        self.transcript_at_paste = []
        self.file = folder / "11111111-new.jsonl"
        self.kills = []

    def run(self, *args, tmux=None, check=False):
        if args[0] == "capture-pane":
            self.captures += 1
            if self.captures > self.start_after and not self.file.exists():
                self.folder.mkdir(parents=True, exist_ok=True)
                self.file.write_text(SESSION_START, encoding="utf-8")
        elif args[0] == "kill-window":
            # the dead session's window is closed: the relaunch writes a transcript of its own
            self.kills.append(args)
            self.pasted = False
            self.file = self.folder / f"{len(self.kills) + 1:08d}-relaunch.jsonl"
        elif args[0] == "paste-buffer":
            self.pasted = True
            self.transcript_at_paste.append(self.file.exists())
        elif args[0] == "send-keys" and args[-1] == "Enter" and "-l" not in args and self.pasted:
            self.brief_enters += 1
            if self.brief_enters == self.deliver_on:
                with open(self.file, "a", encoding="utf-8") as fh:
                    fh.write(USER_LINE)
            if self.brief_enters == self.working_on:
                self.screens = ["✻ Thinking… (esc to interrupt)\n" + BOX]
        return super().run(*args, tmux=tmux, check=check)


def _delivery_setup(tmp_path, monkeypatch, **kw):
    monkeypatch.setattr(claude_mod, "VERIFY_DELIVERY", True)
    home = tmp_path / "claude_home"
    folder = home / "projects" / "C--Users-x-Documents-Claude-brief-probe"
    fake = DeliveryTmux(folder, **kw)
    monkeypatch.setattr(tmuxctl, "run", fake.run)
    brief = tmp_path / "b.md"
    brief.write_text("the brief", encoding="utf-8")
    cfg = cfg_for(tmp_path, claude_home=str(home))
    return cfg, fake, brief


def _pastes(fake):
    return [c for c in fake.calls if c[0] == "paste-buffer"]


def test_transcript_folder_maps_the_path_like_claude_code(tmp_path):
    home = tmp_path / "h"
    assert claude_mod.transcript_folder(str(home), "C:/Users/x/Documents/Projects/notes_app").name \
        == "C--Users-x-Documents-Projects-notes-app"
    assert claude_mod.transcript_folder(str(home), "/c/Users/x/Documents/Projects/hiking_log").name \
        == "C--Users-x-Documents-Projects-hiking-log"
    assert claude_mod.transcript_folder(str(home), "C:\\Users\\x\\Apps\\Smart home").name \
        == "C--Users-x-Apps-Smart-home"


def test_claude_launch_pastes_only_after_the_session_has_started_and_the_box_settled(tmp_path, monkeypatch):
    """The box draws before startup is over (2026-11-09: a brief typed 0.5 s before the
    SessionStart records was lost whole); the paste waits for the transcript and a still screen."""
    cfg, fake, brief = _delivery_setup(tmp_path, monkeypatch, start_after=6)
    result = claude_mod.ClaudeProvider(cfg, sleep=lambda s: None).launch(PROJECT, str(brief), True)
    assert result.ok and result.message == "claude is working in window 4 (brief_probe)"
    assert fake.transcript_at_paste == [True]
    assert fake.captures > 6


def test_claude_launch_counts_the_pane_working_as_delivered(tmp_path, monkeypatch):
    cfg, fake, brief = _delivery_setup(tmp_path, monkeypatch, deliver_on=None, working_on=1)
    result = claude_mod.ClaudeProvider(cfg, sleep=lambda s: None).launch(PROJECT, str(brief), True)
    assert result.ok and "claude is working in window 4" in result.message
    assert len(_pastes(fake)) == 1


def test_claude_launch_waits_for_a_slow_submit_before_any_retry(tmp_path, monkeypatch):
    """Slow is not dead (a paste once took ~40 s): no extra Enter or paste inside the wait."""
    cfg, fake, brief = _delivery_setup(tmp_path, monkeypatch, deliver_on=None)
    slept = []

    def sleep(s):
        slept.append(s)
        if sum(slept) > 40 and not fake.file.read_text(encoding="utf-8").count('"user"'):
            with open(fake.file, "a", encoding="utf-8") as fh:
                fh.write(USER_LINE)

    result = claude_mod.ClaudeProvider(cfg, sleep=sleep).launch(PROJECT, str(brief), True)
    assert result.ok
    assert fake.brief_enters == 1 and len(_pastes(fake)) == 1


def test_claude_launch_presses_enter_once_more_when_nothing_arrived(tmp_path, monkeypatch):
    cfg, fake, brief = _delivery_setup(tmp_path, monkeypatch, deliver_on=2,
                                       box="───\n> the brief\n───\n")
    result = claude_mod.ClaudeProvider(cfg, sleep=lambda s: None).launch(PROJECT, str(brief), True)
    assert result.ok
    assert fake.brief_enters == 2 and len(_pastes(fake)) == 1


def test_claude_launch_pastes_once_more_into_the_same_window_when_the_box_is_empty(tmp_path, monkeypatch):
    cfg, fake, brief = _delivery_setup(tmp_path, monkeypatch, deliver_on=3)
    result = claude_mod.ClaudeProvider(cfg, sleep=lambda s: None).launch(PROJECT, str(brief), True)
    assert result.ok
    assert len(_pastes(fake)) == 2
    assert len([c for c in fake.calls if c[0] == "new-window"]) == 1   # never a second session


def test_claude_launch_does_not_paste_again_over_text_still_in_the_box(tmp_path, monkeypatch):
    cfg, fake, brief = _delivery_setup(tmp_path, monkeypatch, deliver_on=None,
                                       box="───\n> the brief\n───\n")
    result = claude_mod.ClaudeProvider(cfg, sleep=lambda s: None).launch(PROJECT, str(brief), True)
    assert result.ok is False
    assert len(_pastes(fake)) == 2              # one per attempt, never a re-paste over text


def _events(cfg) -> list[dict]:
    path = Path(cfg.events_file)
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def test_claude_launch_says_plainly_when_the_brief_never_arrived_twice(tmp_path, monkeypatch):
    """Both sessions take no keys: the first (dead, its own, empty) window is closed, the second
    is left open, and the message says it was tried twice."""
    cfg, fake, brief = _delivery_setup(tmp_path, monkeypatch, deliver_on=None)
    result = claude_mod.ClaudeProvider(cfg, sleep=lambda s: None).launch(PROJECT, str(brief), True)
    assert result.ok is False
    assert result.message == "window 4 opened but the brief was not delivered (tried twice)"
    assert result.window_index == 4 and result.tmux_pane == "%9"
    assert len(_pastes(fake)) == 4 and fake.brief_enters == 6
    assert len([c for c in fake.calls if c[0] == "new-window"]) == 2
    assert fake.kills == [("kill-window", "-t", "%9")]          # exactly one window closed
    names = [e["event"] for e in _events(cfg)]
    assert names == ["brief_not_delivered", "dead_window_closed", "brief_not_delivered"]
    assert _events(cfg)[2]["dead_pane"] == "%9" and _events(cfg)[2]["attempt"] == 2


def test_a_dead_first_session_is_closed_and_the_relaunch_delivers(tmp_path, monkeypatch):
    # attempt 1 spends three Enters (submit, retry, re-paste); the relaunch's first Enter is #4
    cfg, fake, brief = _delivery_setup(tmp_path, monkeypatch, deliver_on=4)
    result = claude_mod.ClaudeProvider(cfg, sleep=lambda s: None).launch(PROJECT, str(brief), True)
    assert result.ok, result.message
    assert result.message == "claude is working in window 4 (brief_probe)"
    assert fake.kills == [("kill-window", "-t", "%9")]
    assert len([c for c in fake.calls if c[0] == "new-window"]) == 2
    events = _events(cfg)
    assert [e["event"] for e in events] == ["brief_not_delivered", "dead_window_closed",
                                            "brief_delivered_on_retry"]
    assert events[0]["tmux_pane"] == "%9" and events[2]["dead_pane"] == "%9"


class MovedPaneTmux(DeliveryTmux):
    """After the launch opened its window, a different pane sits at that index (the window was
    closed and the index reused): the launch must not close it."""

    def __init__(self, folder):
        super().__init__(folder, deliver_on=None)
        self.pane_reads = 0

    def run(self, *args, tmux=None, check=False):
        if args[0] == "display-message" and "pane_id" in args[-1]:
            import subprocess
            self.calls.append(args)
            self.pane_reads += 1
            return subprocess.CompletedProcess(args, 0, "%9\n" if self.pane_reads == 1 else "%12\n", "")
        return super().run(*args, tmux=tmux, check=check)


def test_the_launcher_never_closes_a_window_it_did_not_create(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_mod, "VERIFY_DELIVERY", True)
    home = tmp_path / "claude_home"
    fake = MovedPaneTmux(home / "projects" / "C--Users-x-Documents-Claude-brief-probe")
    monkeypatch.setattr(tmuxctl, "run", fake.run)
    brief = tmp_path / "b.md"
    brief.write_text("the brief", encoding="utf-8")
    cfg = cfg_for(tmp_path, claude_home=str(home))
    result = claude_mod.ClaudeProvider(cfg, sleep=lambda s: None).launch(PROJECT, str(brief), True)
    assert result.ok is False and result.message == "window 4 opened but the brief was not delivered"
    assert fake.kills == [] and not any(c[0] == "kill-window" for c in fake.calls)
    assert len([c for c in fake.calls if c[0] == "new-window"]) == 1


def test_a_plain_session_with_no_brief_is_never_checked_or_closed(tmp_path, monkeypatch):
    cfg, fake, _brief = _delivery_setup(tmp_path, monkeypatch, deliver_on=None)
    result = claude_mod.ClaudeProvider(cfg, sleep=lambda s: None).launch(PROJECT, "", True)
    assert result.ok and "claude is open in window 4" in result.message
    assert fake.kills == [] and _events(cfg) == []


class NewFolderTmux(DeliveryTmux):
    """A never-trusted folder: the trust question shows first and drops the first `swallow` keys
    (Claude not taking input yet); Enter on yes starts the session (transcript written) and shows
    the prompt box; an Escape would exit Claude."""

    def __init__(self, folder: Path, swallow: int = 3):
        super().__init__(folder, start_after=10_000)
        self.screens = [TRUST_SCREEN]
        self.swallow = swallow
        self.trust_keys = []
        self.exited = False
        self.shown = False          # keys count once the question has been read off the screen

    def run(self, *args, tmux=None, check=False):
        if args[0] == "capture-pane":
            self.shown = True
        if self.shown and args[0] == "send-keys" and "-l" not in args and not self.pasted and \
                args[-1] in ("Down", "Up", "Enter", "Escape"):
            screen = self.screens[0]
            if "trust this folder" in screen:
                self.trust_keys.append(args[-1])
                if self.swallow:
                    self.swallow -= 1
                elif args[-1] == "Escape":
                    self.exited = True
                    self.screens = ["bash-5.3$ "]
                elif args[-1] == "Down":
                    self.screens = [screen.replace(" > No, exit", "   No, exit").replace(
                        "   Yes, I trust this folder", " > Yes, I trust this folder")]
                elif args[-1] == "Enter" and " > Yes, I trust this folder" in screen:
                    self.screens = [BOX]
                    self.folder.mkdir(parents=True, exist_ok=True)
                    self.file.write_text(SESSION_START, encoding="utf-8")
        return super().run(*args, tmux=tmux, check=check)


def test_a_new_folder_launch_answers_trust_after_dropped_keys_then_delivers_the_brief(tmp_path, monkeypatch):
    """2026-11-09, a never-trusted folder: two Downs were dropped and the launch gave up; now the answer
    retries until the cursor moves, and the same launch pastes and delivers the brief."""
    monkeypatch.setattr(claude_mod, "VERIFY_DELIVERY", True)
    home = tmp_path / "claude_home"
    folder = home / "projects" / "C--Users-x-Documents-Claude-brief-probe"
    fake = NewFolderTmux(folder, swallow=3)
    monkeypatch.setattr(tmuxctl, "run", fake.run)
    brief = tmp_path / "b.md"
    brief.write_text("the brief", encoding="utf-8")
    cfg = cfg_for(tmp_path, claude_home=str(home))
    result = claude_mod.ClaudeProvider(cfg, sleep=lambda s: None).launch(PROJECT, str(brief), True)
    assert result.ok, result.message
    assert result.message == "claude is working in window 4 (brief_probe)"
    assert fake.trust_keys == ["Down"] * 4 + ["Enter"]
    assert "Escape" not in fake.trust_keys and not fake.exited
    assert fake.transcript_at_paste == [True] and len(_pastes(fake)) == 1


def test_pantheon_open_exits_nonzero_when_the_brief_never_arrived(tmp_path, monkeypatch, capsys):
    from pantheon.dispatch import projects as projects_mod

    cfg, fake, _brief = _delivery_setup(tmp_path, monkeypatch, deliver_on=None)
    project = tmp_path / "brief_probe"
    project.mkdir()
    cfg = cfg_for(tmp_path, claude_home=cfg.claude_home, projects_root=str(tmp_path))
    monkeypatch.setattr(projects_mod, "get_providers",
                        lambda c: {"claude": claude_mod.ClaudeProvider(c, sleep=lambda s: None)})
    monkeypatch.setattr(config_mod, "load", lambda *a, **k: cfg)
    brief = tmp_path / "msg.md"
    brief.write_text("reply only: ok", encoding="utf-8")
    rc = projects_mod.main(["open", "brief_probe", "--message-file", str(brief)])
    out = capsys.readouterr().out
    assert rc != 0
    assert out.strip() == "window 4 opened but the brief was not delivered (tried twice)"


def test_claude_launch_opens_cds_starts_waits_then_types_the_briefing(tmp_path, monkeypatch):
    cfg = cfg_for(tmp_path)
    fake = FakeTmux(screens=["starting...", "│ > \n"], commands=["bash", "bash", "node"])
    monkeypatch.setattr(tmuxctl, "run", fake.run)
    brief = tmp_path / "b.md"
    brief.write_text("Tracker id: x\nline two\n", encoding="utf-8")
    provider = claude_mod.ClaudeProvider(cfg, sleep=lambda s: None)
    result = provider.launch("C:/Home/x/Documents/Projects/hiking_log_v2", str(brief), True)
    assert result.ok and result.window_index == 4 and result.tmux_pane == "%9"
    typed = _typed(fake)
    assert typed[0] == 'cd "C:/Home/x/Documents/Projects/hiking_log_v2"'
    # A new session is named, and the line ends by recording the pane's exit code.
    assert typed[1] == 'claude --name "hiking_log_v2: Tracker id: x"' + claude_mod.exit_suffix()
    script = (config_mod.PROJECT_ROOT / "bin" / "pane_exit").as_posix()
    assert typed[1].endswith(f'; "{script}" $?')
    assert typed[2] == "Tracker id: x line two"                 # one line, no raw newline
    # Enter is always a separate call, never `-l ... Enter`.
    enters = [c for c in fake.calls if c[0] == "send-keys" and c[-1] == "Enter" and "-l" not in c]
    assert len(enters) == 3


def test_claude_launch_stops_at_a_trust_dialog_it_cannot_read_and_says_so(tmp_path, monkeypatch):
    """No cursor on screen -> no safe way to pick yes -> stop and hand it to the user."""
    cfg = cfg_for(tmp_path)
    fake = FakeTmux(screens=["Is this a project you trust?\n  Yes, I trust this folder\n"], commands=["node"])
    monkeypatch.setattr(tmuxctl, "run", fake.run)
    brief = tmp_path / "b.md"
    brief.write_text("hello", encoding="utf-8")
    result = claude_mod.ClaudeProvider(cfg, sleep=lambda s: None).launch("C:/p", str(brief), True)
    assert result.ok is False and "asking whether to trust p" in result.message
    assert "hello" not in _typed(fake)                           # the briefing was NOT typed into the dialog


DIALOGS = FIXTURE / "dialogs"
TRUST_SCREEN = (DIALOGS / "trust_folder_2.1.283.txt").read_text(encoding="utf-8")
MCP_SCREEN = (DIALOGS / "mcp_server_2.1.283.txt").read_text(encoding="utf-8")


class DialogTmux(FakeTmux):
    """A pane that shows a real startup-dialog screen and reacts to Down/Enter like Claude Code:
    Enter on "Yes, I trust this folder" -> the prompt; `stuck` = the cursor never moves."""

    def __init__(self, screen, stuck=False):
        super().__init__(screens=[screen], commands=["node"])
        self.stuck = stuck
        self.keys = []
        self.shown = False          # keys count once the dialog has been on screen (read once)

    def run(self, *args, tmux=None, check=False):
        if args[0] == "capture-pane":
            self.shown = True
        if (self.shown and args[0] == "send-keys" and "-l" not in args
                and args[-1] in ("Down", "Up", "Enter", "Escape")):
            self.keys.append(args[-1])
            screen = self.screens[0]
            if args[-1] == "Down" and not self.stuck:
                screen = screen.replace(" > No, exit", "   No, exit").replace(
                    "   Yes, I trust this folder", " > Yes, I trust this folder")
            elif args[-1] == "Enter" and " > Yes, I trust this folder" in screen:
                screen = "────\n│ > \n────\n"
            self.screens = [screen]
        return super().run(*args, tmux=tmux, check=check)


def test_claude_launch_answers_the_folder_trust_question_it_caused_then_types_the_briefing(tmp_path, monkeypatch):
    """The user picked the folder in the deck: that is consent to trust it. The answer is Down onto "Yes, I trust this folder", then Enter -- never a bare
    Enter, which would land on "No, exit" and close Claude."""
    cfg = cfg_for(tmp_path)
    fake = DialogTmux(TRUST_SCREEN)
    monkeypatch.setattr(tmuxctl, "run", fake.run)
    brief = tmp_path / "b.md"
    brief.write_text("hello", encoding="utf-8")
    result = claude_mod.ClaudeProvider(cfg, sleep=lambda s: None).launch("C:/p", str(brief), True)
    assert result.ok, result.message
    assert fake.keys[:2] == ["Down", "Enter"]
    assert "hello" in _typed(fake)


def test_claude_launch_stops_at_a_new_mcp_server_question_and_leaves_it_to_the_user(tmp_path, monkeypatch):
    cfg = cfg_for(tmp_path)
    fake = DialogTmux(MCP_SCREEN)
    monkeypatch.setattr(tmuxctl, "run", fake.run)
    brief = tmp_path / "b.md"
    brief.write_text("hello", encoding="utf-8")
    result = claude_mod.ClaudeProvider(cfg, sleep=lambda s: None).launch("C:/p", str(brief), True)
    assert result.ok is False and "asking whether to use a new MCP server in p" in result.message
    assert fake.keys == []                                       # nothing typed at the question
    assert "hello" not in _typed(fake)


def test_claude_launch_says_so_when_the_trust_answer_does_not_take(tmp_path, monkeypatch):
    cfg = cfg_for(tmp_path)
    fake = DialogTmux(TRUST_SCREEN, stuck=True)
    monkeypatch.setattr(tmuxctl, "run", fake.run)
    brief = tmp_path / "b.md"
    brief.write_text("hello", encoding="utf-8")
    result = claude_mod.ClaudeProvider(cfg, sleep=lambda s: None).launch("C:/p", str(brief), True)
    assert result.ok is False and "asking whether to trust trustprobe_a1" in result.message
    assert "Enter" not in fake.keys                              # never Enter on "No, exit"
    assert "hello" not in _typed(fake)


def test_claude_launch_reports_a_window_that_never_started(tmp_path, monkeypatch):
    cfg = cfg_for(tmp_path)
    fake = FakeTmux(commands=["bash"])
    monkeypatch.setattr(tmuxctl, "run", fake.run)
    brief = tmp_path / "b.md"
    brief.write_text("hello", encoding="utf-8")
    result = claude_mod.ClaudeProvider(cfg, sleep=lambda s: None).launch("C:/p", str(brief), True)
    assert result.ok is False and "did not start in window 4" in result.message


def test_codex_headless_launch_runs_the_job_wrapper_in_a_named_window(tmp_path, monkeypatch):
    cfg = cfg_for(tmp_path)
    fake = FakeTmux()
    monkeypatch.setattr(tmuxctl, "run", fake.run)
    result = codex_mod.CodexProvider(cfg).launch("C:/Home/x/Documents/Projects/hiking_log_v2", "C:/b.md", False)
    assert result.ok and result.where == "headless" and result.job_id.startswith("codex-")
    new_window = next(c for c in fake.calls if c[0] == "new-window")
    assert "codex:hiking_log" in new_window
    command = new_window[-1]
    assert "-m pantheon.dispatch.codex_job" in command and '--project-dir "C:/Home/x/Documents/Projects/hiking_log_v2"' in command
    assert result.job_id in command


def test_codex_job_argv_carries_the_flags_that_stop_exec_hanging(tmp_path):
    from pantheon.dispatch import codex_job

    argv = codex_job.codex_argv(cfg_for(tmp_path), "C:/p")
    assert argv[1:] == ["exec", "--skip-git-repo-check", "-s", "workspace-write", "-C", "C:/p", "-"]


def test_codex_job_records_queued_running_done_and_keeps_a_log(tmp_path, monkeypatch):
    """A stand-in for codex (the venv python) proves the wrapper's bookkeeping end to end."""
    import sys

    from pantheon.dispatch import codex_job

    cfg = cfg_for(tmp_path, tools=config_mod.Tools(codex=sys.executable))
    monkeypatch.setattr(codex_job, "codex_argv",
                        lambda cfg, d: [sys.executable, "-c", "import sys; print('echo:' + sys.stdin.read().strip())"])
    brief = tmp_path / "b.md"
    brief.write_text("do the thing", encoding="utf-8")
    code = codex_job.run(cfg, str(brief), str(tmp_path), "codex-test", "hiking_log_v2-x", wait_for_enter=False)
    assert code == 0
    assert (cfg.dispatch_dir / "codex-test.log").read_text(encoding="utf-8").strip() == "echo:do the thing"
    names = [json.loads(l)["event"] for l in open(cfg.events_file, encoding="utf-8")]
    assert names == ["queued", "running", "done"]
    last = json.loads(open(cfg.events_file, encoding="utf-8").read().splitlines()[-1])
    assert last["source"] == "codex" and last["job_id"] == "codex-test"
    # done now carries the job's final output line so an apology-run is visible at a glance
    assert last["detail"] == "exit code 0 · echo:do the thing"
    assert last["tracker_id"] == "hiking_log_v2-x"


def test_codex_job_stops_a_child_that_outlives_its_deadline_and_checks_it_exited(tmp_path, monkeypatch):
    """A stand-in codex that never finishes is terminated at the deadline; `done` says so."""
    import subprocess
    import sys
    import time

    from pantheon.dispatch import codex_job

    started = []
    real_popen = subprocess.Popen

    class RecordingPopen(real_popen):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            started.append(self)

    cfg = cfg_for(tmp_path, tools=config_mod.Tools(codex=sys.executable))
    monkeypatch.setattr(codex_job, "codex_argv",
                        lambda cfg, d: [sys.executable, "-c", "import time; time.sleep(600)"])
    monkeypatch.setattr(codex_job.subprocess, "Popen", RecordingPopen)
    brief = tmp_path / "b.md"
    brief.write_text("hang", encoding="utf-8")
    t0 = time.monotonic()
    code = codex_job.run(cfg, str(brief), str(tmp_path), "codex-hang", wait_for_enter=False,
                         deadline_seconds=1.0, stop_grace=5.0)
    assert time.monotonic() - t0 < 30
    assert code == codex_job.DEADLINE_EXIT_CODE
    assert len(started) == 1 and started[0].poll() is not None  # the child really exited
    last = json.loads(open(cfg.events_file, encoding="utf-8").read().splitlines()[-1])
    assert last["event"] == "done"
    assert last["detail"] == "exit code 124 · stopped at the 1-second deadline"


def test_codex_job_window_waits_for_enter_only_a_bounded_time(tmp_path, monkeypatch):
    """The finished window no longer blocks on input(): it waits at most the linger time, then exits."""
    import os
    import sys
    import time

    from pantheon.dispatch import codex_job

    r, w = os.pipe()
    with os.fdopen(r, encoding="utf-8") as line_in:
        t0 = time.monotonic()
        assert codex_job.wait_for_line(0.3, line_in) is False  # nothing typed: returns at the deadline
        assert time.monotonic() - t0 < 5
        os.write(w, b"\n")
        assert codex_job.wait_for_line(5, line_in) is True     # Enter closes it at once
    os.close(w)

    # run() hands the window's wait to that bounded helper with the linger it was given
    waits = []

    class FakeTTY:
        def isatty(self):
            return True

    cfg = cfg_for(tmp_path, tools=config_mod.Tools(codex=sys.executable))
    monkeypatch.setattr(codex_job, "codex_argv", lambda cfg, d: [sys.executable, "-c", "print('ok')"])
    monkeypatch.setattr(codex_job, "wait_for_line", lambda seconds, stream=None: waits.append(seconds) or False)
    monkeypatch.setattr(sys, "stdin", FakeTTY())
    brief = tmp_path / "b.md"
    brief.write_text("x", encoding="utf-8")
    assert codex_job.run(cfg, str(brief), str(tmp_path), "codex-linger", linger_seconds=42) == 0
    assert waits == [42]


def test_a_new_session_is_named_after_its_project_and_task(tmp_path):
    from pantheon.providers.claude import session_name
    brief = tmp_path / "brief.md"
    brief.write_text("# Fix the sprint base view so done rows vanish at once, every time\nmore\n",
                     encoding="utf-8")
    name = session_name("C:/Home/x/Documents/Projects/obsidian", str(brief))
    assert name.startswith("obsidian: Fix the sprint base view") and name.endswith("…")
    assert len(name) <= 60 and '"' not in name
    assert session_name("C:/Home/x/Documents/Projects/loom-os", "") == "loom-os"
