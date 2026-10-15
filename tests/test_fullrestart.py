"""the full restart that brings every session back (pantheon/fullrestart.py). The snapshot,
the wait-until-idle logic, the per-session resume command, and the detached part's tmux calls --
all against fakes; no test reaches a real tmux server (tests/conftest.py)."""
from __future__ import annotations

import json
import subprocess

import pytest

from pantheon import config as config_mod
from pantheon import fullrestart as full
from pantheon.models import AgentState, AgentStatus, LaunchResult


def _cfg(tmp_path, **kw):
    return config_mod.Config(state_dir=str(tmp_path / "state"), **kw)


def _row(sid="s-1", status=AgentStatus.WAITING_INPUT, window=3, name="garden_notes",
         cwd="C:/Users/x/Documents/Claude/garden_notes", provider="claude", session="pantheon",
         last_action="turn done", pane="%3", job_id=None):
    return AgentState(session_id=sid, provider=provider, status=status, cwd=cwd,
                      last_action=last_action, window_index=window, window_name=name,
                      tmux_pane=pane, tmux_session=session, in_pantheon=session == "pantheon",
                      job_id=job_id)


# --------------------------------------------------------------------------- the wait logic


@pytest.mark.parametrize("status,action,busy", [
    (AgentStatus.WORKING, "Bash", True),
    (AgentStatus.WORKING, full.JUST_STARTED, False),   # at its prompt, no turn taken yet
    (AgentStatus.WINDING_DOWN, "", True),
    (AgentStatus.WAITING_INPUT, "turn done", False),
    (AgentStatus.BLOCKED_PERMISSION, "permission: Bash", False),   # waiting on the user
    (AgentStatus.IDLE, "", False),
    (AgentStatus.QUIET, "", False),
])
def test_busy_means_mid_turn(status, action, busy):
    assert full.is_busy(_row(status=status, last_action=action)) is busy


def test_waiting_on_counts_only_claude_windows_in_this_pantheon():
    rows = [
        _row("a", AgentStatus.WORKING, last_action="Edit"),
        _row("b", AgentStatus.WORKING, last_action="Edit", session="pantheon-4756"),  # grouped client
        _row("c", AgentStatus.WORKING, last_action="Edit", session="main"),           # another tmux
        _row("d", AgentStatus.WORKING, last_action="Edit", window=None, session=None),  # desktop
        _row("e", AgentStatus.RUNNING, provider="codex", name="codex:x"),              # parked, not waited
        _row("f", AgentStatus.WAITING_INPUT),
    ]
    assert [r.session_id for r in full.waiting_on(rows, "pantheon")] == ["a", "b"]


def test_wait_until_settled_says_who_and_returns_once_idle(tmp_path):
    cfg = _cfg(tmp_path)
    ticks = iter([
        [_row("a", AgentStatus.WORKING, last_action="Bash", name="one"), _row("b", AgentStatus.WORKING,
                                                                            last_action="Bash", window=4, name="two")],
        [_row("a", AgentStatus.WAITING_INPUT, name="one"), _row("b", AgentStatus.WORKING, last_action="Bash",
                                                                  window=4, name="two")],
        [_row("a", AgentStatus.WAITING_INPUT, name="one"), _row("b", AgentStatus.IDLE, window=4, name="two")],
    ])
    said, slept = [], []
    rows = full.wait_until_settled(cfg, lambda: next(ticks), say=said.append, sleep=slept.append)
    assert all(not full.is_busy(r) for r in rows)
    assert len(said) == 2      # once per change of who it waits for
    assert "window 3 one (working)" in said[0] and "window 4 two (working)" in said[0]
    assert "one" not in said[1] and "window 4 two" in said[1]
    assert "Ctrl+C cancels" in said[0]
    assert slept == [full.POLL_SECONDS, full.POLL_SECONDS]


def test_wait_can_time_out_naming_who(tmp_path):
    clock = iter([0.0, 5.0, 11.0]).__next__
    with pytest.raises(TimeoutError, match="garden_notes"):
        full.wait_until_settled(_cfg(tmp_path), lambda: [_row(status=AgentStatus.WORKING, last_action="Bash")],
                                say=lambda m: None, sleep=lambda s: None, timeout=10, clock=clock)


# --------------------------------------------------------------------------- the snapshot


def test_snapshot_records_ids_folders_windows_and_the_assistant(tmp_path):
    cfg = _cfg(tmp_path)
    rows = [
        _row("s-1", window=3, name="garden_notes", cwd="C:/p/garden_notes", pane="%3"),
        _row("s-2", window=5, name="pantheon", cwd="C:/p/project_pantheon", pane="%5"),
        _row("s-asst", window=6, name="ASSISTANT", cwd="C:/p", pane="%6"),
        _row("codex-1", AgentStatus.RUNNING, provider="codex", window=7, name="codex:life",
             cwd="C:/p/life", pane="%7", job_id="codex-1"),
        _row("codex-2", AgentStatus.DONE, provider="codex", window=8, name="codex:old", pane="%8"),
    ]
    snap, problems = full.build_snapshot(cfg, rows, assistant_state={"session_id": "s-asst", "folder": "C:/p"})
    assert problems == []
    assert [(e["session_id"], e["folder"], e["window_name"], e["role"]) for e in snap["claude"]] == [
        ("s-1", "C:/p/garden_notes", "garden_notes", "session"),
        ("s-2", "C:/p/project_pantheon", "pantheon", "session"),
        ("s-asst", "C:/p", "ASSISTANT", "assistant"),
    ]
    assert [(c["job_id"] or c["session_id"], c["action"]) for c in snap["codex"]] == [
        ("codex-1", "keep-running"), ("codex-2", "finished")]
    assert snap["assistant"]["session_id"] == "s-asst"
    path = full.write_snapshot(cfg, snap)
    assert path.name == "restart_snapshot.json"
    assert json.loads(path.read_text(encoding="utf-8"))["claude"][0]["session_id"] == "s-1"
    assert full.read_snapshot(cfg)["tmux_session"] == "pantheon"


def test_a_window_with_no_conversation_id_is_a_problem_not_a_guess(tmp_path):
    snap, problems = full.build_snapshot(_cfg(tmp_path), [
        _row("pane:%9", AgentStatus.UNKNOWN, window=9, name="mystery"), _row("s-1")], assistant_state={})
    assert len(problems) == 1 and "window 9 mystery" in problems[0] and "no known conversation id" in problems[0]
    assert [e["session_id"] for e in snap["claude"]] == ["s-1"]


def test_prepare_fails_closed_and_writes_nothing_on_a_missing_id(tmp_path):
    cfg = _cfg(tmp_path)
    said = []
    code = full.prepare(cfg, rows_source=lambda: [_row("pane:%9", AgentStatus.UNKNOWN, name="mystery")],
                        say=said.append, sleep=lambda s: None)
    assert code == 2
    assert not full.snapshot_path(cfg).exists()
    assert "nothing was ended" in said[-1] and "mystery" in said[-1]


def test_prepare_no_wait_stops_when_a_turn_started(tmp_path):
    cfg = _cfg(tmp_path)
    said = []
    code = full.prepare(cfg, wait=False, say=said.append,
                        rows_source=lambda: [_row(status=AgentStatus.WORKING, last_action="Bash")])
    assert code == 3 and not full.snapshot_path(cfg).exists()
    assert "started a turn" in said[-1]


def test_prepare_waits_then_writes_the_snapshot(tmp_path):
    cfg = _cfg(tmp_path)
    ticks = iter([[_row(status=AgentStatus.WORKING, last_action="Bash")],   # the id check
                  [_row(status=AgentStatus.WORKING, last_action="Bash")],   # first wait poll
                  [_row(status=AgentStatus.WAITING_INPUT)]])
    code = full.prepare(cfg, rows_source=lambda: next(ticks), say=lambda m: None, sleep=lambda s: None)
    assert code == 0
    assert full.read_snapshot(cfg)["claude"][0]["session_id"] == "s-1"


def test_prepare_ctrl_c_cancels_and_ends_nothing(tmp_path):
    cfg = _cfg(tmp_path)

    def interrupt(_s):
        raise KeyboardInterrupt
    said = []
    code = full.prepare(cfg, rows_source=lambda: [_row(status=AgentStatus.WORKING, last_action="Bash")],
                        say=said.append, sleep=interrupt)
    assert code == 130 and "nothing was ended" in said[-1]
    assert not full.snapshot_path(cfg).exists()


# --------------------------------------------------------------------------- the resume command


def test_resume_command_is_claude_resume_with_the_saved_id(tmp_path):
    cfg = _cfg(tmp_path)
    entry = {"session_id": "0b3c-1234", "folder": "C:/p/garden_notes", "window_name": "garden_notes"}
    assert full.resume_options(entry) == {"resume": "0b3c-1234", "window_name": "garden_notes"}
    cmd = full.resume_command(cfg, entry)
    assert cmd == f"{cfg.tools.claude} --resume 0b3c-1234"
    assert "--continue" not in cmd and "--name" not in cmd


def test_reopen_goes_through_the_decks_start_session_and_skips_the_assistant(tmp_path):
    cfg = _cfg(tmp_path)
    snap = {"claude": [
        {"session_id": "s-1", "folder": "C:/p/a", "window_name": "a", "role": "session"},
        {"session_id": "s-asst", "folder": "C:/p", "window_name": "ASSISTANT", "role": "assistant"},
        {"session_id": "s-2", "folder": "C:/p/gone", "window_name": "b", "role": "session"},
    ]}
    calls = []

    def start(folder, cfg_, provider, interactive, message, options):
        calls.append((folder, interactive, message, options))
        if folder.endswith("gone"):
            return LaunchResult(False, message="that folder is not there any more: C:/p/gone")
        return LaunchResult(True, "tmux", 7, "a")

    resumed, failed = full.reopen(cfg, snap, provider=object(), start=start)
    assert calls == [("C:/p/a", True, "", {"resume": "s-1", "window_name": "a"}),
                     ("C:/p/gone", True, "", {"resume": "s-2", "window_name": "b"})]
    assert [r["session_id"] for r in resumed] == ["s-1"] and resumed[0]["window_index"] == 7
    assert failed[0]["session_id"] == "s-2" and "not there" in failed[0]["message"]


def test_the_claude_provider_types_the_resume_into_the_named_window(tmp_path, monkeypatch):
    """The reopen path is the deck's own: ClaudeProvider.launch opens a window with the saved
    name and types `claude --resume <id>` after `cd` into the saved folder."""
    from pantheon import tmuxctl
    from pantheon.providers.claude import ClaudeProvider, exit_suffix

    cfg = _cfg(tmp_path)
    typed, opened = [], []
    monkeypatch.setattr(tmuxctl, "new_window", lambda s, name, cwd, **kw: opened.append((name, cwd)) or 9)
    monkeypatch.setattr(tmuxctl, "send_text", lambda t, text, **kw: typed.append(text) or True)
    monkeypatch.setattr(tmuxctl, "wait_for_command", lambda *a, **kw: True)
    monkeypatch.setattr(tmuxctl, "capture", lambda *a, **kw: "> ")
    monkeypatch.setattr(tmuxctl, "pane_id", lambda *a, **kw: "%9")
    result = ClaudeProvider(cfg, sleep=lambda s: None).launch(
        "C:/p/a", "", True, options=full.resume_options({"session_id": "s-1", "window_name": "a-win"}))
    assert result.ok and opened == [("a-win", "C:/p/a")]
    assert typed == ['cd "C:/p/a"', f"{cfg.tools.claude} --resume s-1" + exit_suffix()]


# --------------------------------------------------------------------------- the detached part


class FakeTmux:
    def __init__(self, sessions=(), hold_windows="", fail=()):
        self.calls = []
        self.sessions = list(sessions)
        self.hold_windows = hold_windows
        self.fail = set(fail)

    def __call__(self, *args, tmux=None, check=False):
        self.calls.append(args)
        code = 1 if args[0] in self.fail else 0
        out = ""
        if args[0] == "list-sessions":
            out = "\n".join(self.sessions)
        elif args[0] == "list-windows":
            out = self.hold_windows
        elif args[0] == "has-session" and "has-session" not in self.fail:
            code = 0
        return subprocess.CompletedProcess(["tmux", *args], code, out, "")


def test_end_sessions_ends_base_and_grouped_but_not_others_or_the_hold(tmp_path):
    fake = FakeTmux(sessions=["pantheon", "pantheon-4756", "main", "pantheon_restart_hold", "pantheonx"])
    ended = full.end_sessions(_cfg(tmp_path), run=fake)
    assert ended == ["pantheon", "pantheon-4756"]
    assert ("kill-session", "-t", "=pantheon") in fake.calls
    assert not any(c[0] == "kill-session" and "main" in c[2] for c in fake.calls)


def test_running_codex_jobs_are_parked_and_come_back(tmp_path):
    cfg = _cfg(tmp_path)
    snap = {"codex": [
        {"session_id": "c1", "window_name": "codex:life", "window_index": 7, "tmux_pane": "%7",
         "action": "keep-running"},
        {"session_id": "c2", "window_name": "codex:old", "window_index": 8, "tmux_pane": "%8",
         "action": "finished"},
    ]}
    fake = FakeTmux(fail={"has-session"})
    moved, failed = full.park_codex(cfg, snap, run=fake)
    assert moved == ["codex:life"] and failed == []
    assert ("move-window", "-d", "-s", "%7", "-t", "pantheon_restart_hold:") in fake.calls
    assert not any("%8" in c for c in fake.calls)
    back_fake = FakeTmux(hold_windows="0|hold\n1|codex:life")
    back, stuck = full.unpark_codex(cfg, run=back_fake)
    assert back == ["codex:life"] and stuck == []
    assert ("move-window", "-d", "-s", "pantheon_restart_hold:1", "-t", "pantheon:") in back_fake.calls
    assert ("kill-session", "-t", "=pantheon_restart_hold") in back_fake.calls


def test_run_stops_before_ending_anything_when_a_codex_job_cannot_be_kept(tmp_path):
    cfg = _cfg(tmp_path)
    full.write_snapshot(cfg, {"claude": [], "codex": [
        {"session_id": "c1", "window_name": "codex:life", "tmux_pane": "%7", "action": "keep-running"}]})
    fake = FakeTmux(sessions=["pantheon"], fail={"move-window", "has-session"})
    spawned = []
    assert full.run_restart(cfg, run=fake, spawn=spawned.append) == 2
    assert not any(c[0] == "kill-session" and c[2] == "=pantheon" for c in fake.calls)
    assert spawned == []
    assert "nothing was ended" in full.read_result(cfg)["error"]


def test_run_without_a_snapshot_ends_nothing(tmp_path):
    fake = FakeTmux(sessions=["pantheon"])
    assert full.run_restart(_cfg(tmp_path), run=fake, spawn=lambda a: 0) == 2
    assert fake.calls == []


def test_run_ends_the_sessions_then_starts_over_ssh(tmp_path, monkeypatch):
    monkeypatch.delenv("TMUX_TMPDIR", raising=False)
    monkeypatch.delenv("PANTHEON_RESTART_LOCAL", raising=False)
    monkeypatch.delenv("PANTHEON_STATE_DIR", raising=False)
    cfg = _cfg(tmp_path)
    full.write_snapshot(cfg, {"claude": [{"session_id": "s-1", "folder": "C:/p/a", "role": "session"}],
                              "codex": []})
    fake = FakeTmux(sessions=["pantheon", "pantheon-99"])
    spawned = []
    assert full.run_restart(cfg, run=fake, spawn=lambda argv: spawned.append(argv) or 0,
                            root=tmp_path / "checkout") == 0
    assert [c[2] for c in fake.calls if c[0] == "kill-session"] == ["=pantheon", "=pantheon-99"]
    argv = spawned[0]
    assert argv[1:6] == ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "localhost"]
    remote = argv[-1]
    assert remote.startswith("export PATH=/usr/bin:$PATH")
    assert f"{(tmp_path / 'checkout' / 'bin' / 'pantheon').as_posix()} --no-attach" in remote
    assert "-m pantheon.fullrestart reopen" in remote and "PANTHEON_SESSION=pantheon" in remote


def test_a_throwaway_tmux_server_is_restarted_locally_not_over_ssh(monkeypatch):
    monkeypatch.setenv("TMUX_TMPDIR", "/tmp/lane")
    assert full.use_ssh() is False
    monkeypatch.delenv("TMUX_TMPDIR")
    monkeypatch.delenv("PANTHEON_RESTART_LOCAL", raising=False)
    assert full.use_ssh() is True


def test_reopen_step_writes_the_result_naming_failures_by_id(tmp_path):
    cfg = _cfg(tmp_path)
    full.write_snapshot(cfg, {"claude": [
        {"session_id": "s-1", "folder": "C:/p/a", "window_name": "a", "role": "session"},
        {"session_id": "s-2", "folder": "C:/p/b", "window_name": "b", "role": "session"}], "codex": []})
    fake = FakeTmux(hold_windows="0|hold\n1|codex:life")

    def start(folder, *a):
        return LaunchResult(folder.endswith("a"), "tmux", 4, message="claude did not start in window 5")
    result = full.reopen_step(cfg, run=fake, start=start, provider=object(), sleep=lambda s: None)
    assert [r["session_id"] for r in result["resumed"]] == ["s-1"]
    assert full.read_result(cfg)["failed"][0]["session_id"] == "s-2"
    message, warn = full.result_message(full.read_result(cfg))
    assert warn and "did NOT reopen: b (s-2)" in message
    assert "1 session(s) back" in message and "Codex jobs kept running, not resumed: codex:life" in message


def test_reopen_step_gives_up_when_pantheon_never_came_back(tmp_path):
    cfg = _cfg(tmp_path)
    full.write_snapshot(cfg, {"claude": [{"session_id": "s-1", "folder": "C:/p/a", "role": "session"}]})
    fake = FakeTmux(fail={"has-session"})
    result = full.reopen_step(cfg, run=fake, start=lambda *a: pytest.fail("must not start"),
                              sleep=lambda s: None, wait_seconds=2)
    assert "did not come back" in result["error"] and result["failed"][0]["session_id"] == "s-1"
    assert full.result_message(result)[1] is True
