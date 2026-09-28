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
    return [c[4] for c in fake.calls if c[0] == "send-keys" and "-l" in c]


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
    assert typed[1] == 'claude --name "hiking_log_v2: Tracker id: x"'   # a new session is named
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


def test_a_new_session_is_named_after_its_project_and_task(tmp_path):
    from pantheon.providers.claude import session_name
    brief = tmp_path / "brief.md"
    brief.write_text("# Fix the sprint base view so done rows vanish at once, every time\nmore\n",
                     encoding="utf-8")
    name = session_name("C:/Home/x/Documents/Projects/obsidian", str(brief))
    assert name.startswith("obsidian: Fix the sprint base view") and name.endswith("…")
    assert len(name) <= 60 and '"' not in name
    assert session_name("C:/Home/x/Documents/Projects/loom-os", "") == "loom-os"
