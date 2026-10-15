"""The freeze watcher (pantheon/health/watch.py). Every outside effect is a fake:
tmux, the sampler, PowerShell, taskkill, the toast and the phone push, the restart, and the
clock. No test reaches a real tmux server (tests/conftest.py) or waits."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from pantheon import config as config_mod
from pantheon import fullrestart
from pantheon.health import watch
from pantheon.models import AgentState, AgentStatus


def _cfg(tmp_path, **health):
    return config_mod.Config(state_dir=str(tmp_path / "state"), health=health)


def _cp(rc=0, out="", err=""):
    return subprocess.CompletedProcess(["tmux"], rc, out, err)


class FakeTmux:
    """Answers each tmux command from `outcomes` (for list-sessions) and fixed replies otherwise."""

    def __init__(self, outcomes=None, clients="", pid="77"):
        self.outcomes = list(outcomes or [])
        self.calls = []
        self.clients = clients
        self.pid = pid

    def __call__(self, *args, tmux=None, check=False):
        self.calls.append(args)
        if args[0] == "list-sessions":
            outcome = self.outcomes.pop(0) if self.outcomes else "ok"
            if isinstance(outcome, BaseException):
                raise outcome
            return {"ok": _cp(0, "pantheon: 3 windows\n"),
                    "timeout": _cp(124, "", "tmux timed out"),
                    "gone": _cp(1, "", "no server running on /tmp/tmux-1000/default")}[outcome]
        if args[0] == "display-message":
            return _cp(0, self.pid + "\n")
        if args[0] == "list-clients":
            return _cp(0, self.clients)
        return _cp(1, "", "not faked")


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


def _sample(handles=300, commit=50.0, top=None):
    return {"ok": True, "error": "", "server": {"pid": 9001, "name": "tmux", "handles": handles},
            "claude": [], "commit_percent": commit,
            "top": top or [{"name": "chrome", "private_mb": 900.0}, {"name": "claude", "private_mb": 400.0},
                           {"name": "node", "private_mb": 300.0}, {"name": "x", "private_mb": 1.0}]}


def _row(sid="s-1", status=AgentStatus.WAITING_INPUT, window=3, name="garden_notes"):
    return AgentState(session_id=sid, provider="claude", status=status, cwd="C:/p/garden_notes",
                      last_action="turn done", window_index=window, window_name=name,
                      tmux_pane="%3", tmux_session="pantheon", in_pantheon=True)


class Rig:
    """A watcher wired to fakes, with every call recorded."""

    def __init__(self, tmp_path, outcomes=None, samples=None, rows=None, clients="", spawn_rc=None,
                 restart_result=None, proc_winpid="9001", **health):
        self.cfg = _cfg(tmp_path, **health)
        self.clock = Clock()
        self.tmux = FakeTmux(outcomes, clients=clients)
        self.alerts, self.toasts, self.pushes, self.spawned, self.restarts = [], [], [], [], []
        self.samples = list(samples or [])
        self.rows = rows if rows is not None else [_row()]
        self.spawn_rc = spawn_rc or {}
        self.restart_result = restart_result or {"resumed": [{"window_name": "garden_notes"}],
                                                 "failed": [], "expected": 1}
        proc = tmp_path / "proc" / "77"
        proc.mkdir(parents=True, exist_ok=True)
        (proc / "winpid").write_text(proc_winpid)
        self.w = watch.Watcher(
            self.cfg, run=self.tmux, spawn=self.spawn, sampler=self.sampler, toast=self.toast,
            ntfy=self.ntfy, after_freeze=self.after_freeze, windows_source=lambda: [],
            rows_source=lambda windows: list(self.rows), clock=self.clock, sleep=self.clock.sleep,
            alive=lambda pid: False, proc_root=str(tmp_path / "proc"), root=tmp_path / "root")

    def sampler(self, winpid, **kw):
        s = self.samples.pop(0) if self.samples else _sample()
        return dict(s, server_winpid=winpid, handle_types={"Mutant": 2} if kw.get("with_types") else {})

    def toast(self, title, body, pwsh, **kw):
        self.toasts.append((title, body))
        self.alerts.append(body)
        return True, "toast started"

    def ntfy(self, url, topic, title, body, **kw):
        self.pushes.append((url, topic, title, body))
        return False, "ntfy: no server url configured"

    def spawn(self, argv, timeout, env=None):
        self.spawned.append(list(argv))
        assert timeout and timeout > 0
        name = Path(argv[0]).name
        rc = self.spawn_rc.get(name, 0)
        if isinstance(rc, BaseException):
            raise rc
        return subprocess.CompletedProcess(argv, rc, "[]", "")

    def after_freeze(self, cfg):
        self.restarts.append(self.clock())
        return dict(self.restart_result)


# --------------------------------------------------------------------------- probe outcomes


def test_answered_probe_is_healthy_and_writes_snapshot_sample_and_server_pid(tmp_path):
    rig = Rig(tmp_path)
    assert rig.w.cycle() == watch.ANSWERED
    snap = json.loads(watch.last_snapshot_path(rig.cfg).read_text())
    assert [e["session_id"] for e in snap["claude"]] == ["s-1"]
    health = json.loads(watch.health_path(rig.cfg).read_text())
    assert health["level"] == "ok" and health["message"] == "" and health["server_handles"] == 300
    server = json.loads((watch.health_dir(rig.cfg) / "server.json").read_text())
    assert server == {**server, "msys_pid": 77, "winpid": 9001}
    assert rig.alerts == []


def test_snapshot_and_sample_keep_their_own_timers(tmp_path):
    rig = Rig(tmp_path)
    sampled = []
    real = rig.sampler
    snapped = []
    real_snap = rig.w.save_snapshot
    rig.w.sampler = lambda winpid, **kw: (sampled.append(rig.clock()), real(winpid, **kw))[1]
    rig.w.save_snapshot = lambda: (snapped.append(rig.clock()), real_snap())
    start = rig.clock()
    rig.w.loop(max_cycles=11)                   # 0..300 s at 30 s
    assert [t - start for t in sampled] == [0, 60, 120, 180, 240, 300]
    assert [t - start for t in snapped] == [0, 300]


def test_one_timeout_is_one_miss_and_an_answer_clears_it(tmp_path):
    rig = Rig(tmp_path, outcomes=["timeout", "timeout", "ok", "timeout"])
    for _ in range(4):
        rig.w.cycle()
    assert rig.w.misses == 1 and rig.restarts == []


def test_spawn_failures_are_not_misses(tmp_path):
    rig = Rig(tmp_path, outcomes=[BlockingIOError(11, "Resource temporarily unavailable"),
                                  OSError(12, "Cannot allocate memory")] * 3)
    outcomes = [rig.w.cycle() for _ in range(6)]
    assert outcomes == [watch.SPAWN_FAILED] * 6
    assert rig.w.misses == 0 and rig.restarts == []
    assert not any("taskkill" in a[0] for a in rig.spawned)


def test_no_server_is_one_log_line_and_the_watcher_leaves(tmp_path):
    rig = Rig(tmp_path, outcomes=["gone"])
    assert rig.w.loop(max_cycles=5) == 1
    assert rig.w.leaving and rig.alerts == [] and rig.restarts == [] and rig.spawned == []


def test_single_instance_pid_file(tmp_path):
    cfg = _cfg(tmp_path)
    watch.pid_path(cfg).parent.mkdir(parents=True)
    watch.pid_path(cfg).write_text("4242\n")
    assert watch.other_watcher(cfg, alive=lambda pid: True) == 4242
    assert watch.other_watcher(cfg, alive=lambda pid: False) is None


# --------------------------------------------------------------------------- the freeze path


def _freeze(rig, n=3):
    for _ in range(n):
        rig.w.cycle()


def _kills(rig):
    return [a for a in rig.spawned if "taskkill" in Path(a[0]).name]


def test_three_timeouts_capture_alert_kill_and_restart(tmp_path):
    rig = Rig(tmp_path, outcomes=["ok"] + ["timeout"] * 4)
    events = Path(rig.cfg.events_file)
    events.parent.mkdir(parents=True, exist_ok=True)
    events.write_text("".join(json.dumps({"n": i}) + "\n" for i in range(150)))
    cmdlog = Path(rig.cfg.state_dir) / "tmux-commands.log"
    cmdlog.write_text("1 start 7-1 a:b t list-sessions\n1 end 7-1 rc=0 0.1\n2 start 7-2 a:b t list-panes\n")
    rig.w.cycle()                                # healthy: learns the server pid
    _freeze(rig)
    folders = list(watch.health_dir(rig.cfg).glob("freeze-*"))
    assert len(folders) == 1
    manifest = json.loads((folders[0] / "manifest.json").read_text())
    taken = {t["item"] for t in manifest["taken"]}
    assert {"sample with handle types", "server threads", "open tmux commands", "command log tail",
            "last 100 events", "low-memory events", "ps -W"} <= taken
    assert (folders[0] / "tmux-commands-open.txt").read_text().strip() == "2 start 7-2 a:b t list-panes"
    assert len((folders[0] / "events-tail.jsonl").read_text().splitlines()) == 100
    assert json.loads((folders[0] / "sample.json").read_text())["handle_types"] == {"Mutant": 2}
    kills = _kills(rig)
    assert kills == [[kills[0][0], "//F", "//T", "//PID", "9001"]]
    assert rig.spawned[-1] == kills[0]           # the capture ran first, the kill last
    assert len(rig.restarts) == 1
    froze, restarted = rig.toasts
    assert froze[1].startswith("Pantheon froze at ") and froze[1].endswith("Saving details, then restarting.")
    assert restarted[1] == "Pantheon restarted after a freeze: 1 of 1 sessions back"
    assert len(rig.pushes) == 2                  # the phone too, with [notify] url and topic


def test_a_server_that_answers_after_the_capture_stalled_and_is_not_ended(tmp_path):
    """2026-10-04: no answer for about 100 s, an answer while the capture ran, and a kill anyway
    that ended four running background agents. Now the last probe decides."""
    rig = Rig(tmp_path, outcomes=["ok"] + ["timeout"] * 3 + ["ok"])
    rig.w.cycle()
    for _ in range(3):
        rig.w.cycle()
        rig.clock.t += 35
    assert _kills(rig) == [] and rig.restarts == []
    hdir = watch.health_dir(rig.cfg)
    assert list(hdir.glob("freeze-*")) == [] and len(list(hdir.glob("stall-*"))) == 1
    assert len(rig.toasts) == 1 and rig.toasts[0][0] == "Pantheon stalled"
    assert "about 70 seconds" in rig.toasts[0][1] and "Nothing was restarted" in rig.toasts[0][1]
    assert "last_freeze" not in rig.w.state and "last_auto_restart" not in rig.w.state
    assert rig.w.misses == 0 and not rig.w.left_frozen


def test_the_restart_goes_ahead_when_the_capture_failed_and_the_alert_says_so(tmp_path, monkeypatch):
    rig = Rig(tmp_path, outcomes=["ok"] + ["timeout"] * 4)

    def broken(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(watch.capture_mod, "capture", broken)
    rig.w.cycle()
    _freeze(rig)
    assert len(rig.restarts) == 1 and len(_kills(rig)) == 1
    assert rig.toasts[0][1].endswith("The capture failed.")


def test_each_capture_item_is_bounded_and_the_total_is_capped(tmp_path):
    rig = Rig(tmp_path, outcomes=["ok"] + ["timeout"] * 4, capture_seconds=24)
    timeouts = []

    def slow_spawn(argv, timeout, env=None):
        timeouts.append(timeout)
        if "taskkill" in Path(argv[0]).name:
            return subprocess.CompletedProcess(argv, 0, "", "")
        rig.clock.t += 12                        # every outside program uses 12 s, then times out
        raise subprocess.TimeoutExpired(argv, timeout)

    rig.w.spawn = slow_spawn
    rig.w.cycle()
    _freeze(rig)
    manifest = json.loads(next(watch.health_dir(rig.cfg).glob("freeze-*/manifest.json")).read_text())
    reasons = {s["item"]: s["reason"] for s in manifest["skipped"]}
    assert reasons["server threads"] == "timed out"
    assert reasons["ps -W"] == "the capture ran out of time"
    assert all(t <= 20 for t in timeouts[:-1])   # each item has its own cap
    assert len(rig.restarts) == 1
    assert rig.toasts[0][1].endswith("Some details could not be saved.")


def test_storm_guard_second_freeze_inside_the_window_gets_capture_and_alert_only(tmp_path):
    rig = Rig(tmp_path, outcomes=["ok"] + ["timeout"] * 4 + ["ok"] + ["timeout"] * 7)
    rig.w.cycle()
    _freeze(rig)
    assert len(rig.restarts) == 1
    rig.w.cycle()                                # back, healthy
    _freeze(rig)                                 # moments later: inside 60 minutes
    assert len(rig.restarts) == 1 and len(_kills(rig)) == 1
    assert rig.toasts[-1][1] == "Pantheon froze again; not restarting; details saved"
    assert len(list(watch.health_dir(rig.cfg).glob("freeze-*"))) == 2
    _freeze(rig)                                 # the frozen server is left for the user: no repeat
    assert len(list(watch.health_dir(rig.cfg).glob("freeze-*"))) == 2
    assert rig.w.left_frozen


def test_a_freeze_after_the_storm_window_restarts_again(tmp_path):
    rig = Rig(tmp_path, outcomes=["ok"] + ["timeout"] * 4 + ["ok"] + ["timeout"] * 4)
    rig.w.cycle()
    _freeze(rig)
    rig.clock.t += 61 * 60
    rig.w.cycle()
    _freeze(rig)
    assert len(rig.restarts) == 2


def test_auto_restart_off_captures_and_alerts_only(tmp_path):
    rig = Rig(tmp_path, outcomes=["ok"] + ["timeout"] * 4, auto_restart=False)
    rig.w.cycle()
    _freeze(rig)
    assert rig.restarts == [] and _kills(rig) == []
    assert "automatic restart is off" in rig.toasts[0][1]


def test_alerts_follow_the_health_alerts_setting(tmp_path):
    rig = Rig(tmp_path, outcomes=["ok"] + ["timeout"] * 4, alerts=["ntfy"])
    rig.w.cycle()
    _freeze(rig)
    assert rig.toasts == [] and len(rig.pushes) == 2
    assert len((watch.health_dir(rig.cfg) / "alerts.jsonl").read_text().splitlines()) == 2


def test_restart_summary_names_windows_and_lost_codex_never_ids():
    text = watch.restart_summary("Pantheon restarted after a freeze", {
        "resumed": [{"window_name": "a", "session_id": "uuid-1"}],
        "failed": [{"window_name": "b", "session_id": "uuid-2"}], "expected": 2, "codex_lost": ["job7"]})
    assert text == ("Pantheon restarted after a freeze: 1 of 2 sessions back; did not come back: b; "
                    "Codex jobs lost: job7")


# --------------------------------------------------------------------------- run_after_freeze


def _snapshot(cfg, claude, codex=()):
    path = fullrestart.last_health_snapshot_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"tmux_session": "pantheon", "claude": list(claude), "codex": list(codex)}))


def test_run_after_freeze_starts_locally_reopens_and_names_lost_codex(tmp_path):
    from pantheon.models import LaunchResult

    cfg = _cfg(tmp_path)
    _snapshot(cfg, [{"session_id": "a1", "folder": "C:/p/a", "window_name": "a", "role": "session"},
                    {"session_id": "b1", "folder": "C:/p/b", "window_name": "b", "role": "session"},
                    {"session_id": "z", "folder": "C:/p", "window_name": "asst", "role": "assistant"}],
              [{"window_name": "job7", "action": "keep-running"}, {"window_name": "old", "action": "finished"}])
    spawned, tmux_calls = [], []

    def spawn(argv, env):
        spawned.append((argv, env["PANTHEON_SESSION"]))
        return 0

    def run(*args, tmux=None, check=False):
        tmux_calls.append(args)
        return _cp(0 if args[0] == "has-session" else 1)

    def start(folder, cfg, provider, *rest):
        return LaunchResult(folder.endswith("a"), "tmux", 4, message="did not start")

    result = fullrestart.run_after_freeze(cfg, run=run, spawn=spawn, root=tmp_path / "root",
                                          provider=object(), start=start, sleep=lambda s: None)
    assert spawned == [([(tmp_path / "root" / "bin" / "pantheon").as_posix(), "--no-attach"], "pantheon")]
    assert not any(a[0] == "kill-session" and a[-1] == "=pantheon" for a in tmux_calls)
    assert [r["session_id"] for r in result["resumed"]] == ["a1"]
    assert [f["session_id"] for f in result["failed"]] == ["b1"]
    assert result["codex_lost"] == ["job7"] and result["expected"] == 2
    assert fullrestart.read_result(cfg)["after_freeze"] is True
    message, warn = fullrestart.result_message(fullrestart.read_result(cfg))
    assert warn and "Codex jobs lost in the freeze: job7" in message


def test_run_after_freeze_writes_an_error_when_pantheon_does_not_start(tmp_path):
    cfg = _cfg(tmp_path)
    _snapshot(cfg, [{"session_id": "a1", "folder": "C:/p/a", "window_name": "a", "role": "session"}])
    result = fullrestart.run_after_freeze(cfg, run=lambda *a, **k: _cp(0), spawn=lambda argv, env: 3,
                                          root=tmp_path)
    assert "exit 3" in result["error"] and [f["session_id"] for f in result["failed"]] == ["a1"]
    assert watch.restart_summary("Pantheon restarted after a freeze", result).startswith(
        "Pantheon restarted after a freeze, but: Pantheon did not start again")


def test_run_after_freeze_without_a_snapshot_still_restarts(tmp_path):
    cfg = _cfg(tmp_path)
    calls = []
    result = fullrestart.run_after_freeze(cfg, run=lambda *a, **k: _cp(0),
                                          spawn=lambda argv, env: calls.append(argv) or 0, root=tmp_path,
                                          start=lambda *a: None, provider=object())
    assert len(calls) == 1 and result["resumed"] == [] and result["expected"] == 0


# --------------------------------------------------------------------------- warnings


def _sample_passes(rig, n):
    """n sample passes: each healthy cycle plus a jump past the sample timer."""
    for _ in range(n):
        rig.w.cycle()
        rig.clock.t += rig.w.h.sample_seconds


def test_handle_warning_once_per_crossing(tmp_path):
    rig = Rig(tmp_path, samples=[_sample(9000), _sample(10000), _sample(12000), _sample(9999),
                                 _sample(15000)], planned_restart=False)
    _sample_passes(rig, 5)
    handle_alerts = [b for b in rig.alerts if "handles" in b]
    assert handle_alerts == ["The tmux server holds 10,000 handles (warning at 10,000).",
                             "The tmux server holds 15,000 handles (warning at 10,000)."]


def test_memory_warning_names_top_three_and_fires_once(tmp_path):
    rig = Rig(tmp_path, samples=[_sample(commit=95.0), _sample(commit=96.0), _sample(commit=50.0),
                                 _sample(commit=91.0)])
    _sample_passes(rig, 4)
    mem = [b for b in rig.alerts if b.startswith("Memory is low")]
    assert len(mem) == 2
    assert mem[0] == ("Memory is low: commit 95% of the limit. "
                      "Top users: chrome 900 MB, claude 400 MB, node 300 MB.")


def test_spawn_failure_and_new_log_lines_are_memory_signals(tmp_path):
    rig = Rig(tmp_path, outcomes=["ok", BlockingIOError(11, "x"), "ok", "ok"])
    log = Path(rig.cfg.log_file)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("old: memory allocation of 4096 bytes failed\n")   # history is not news
    _sample_passes(rig, 1)
    assert rig.alerts == []
    rig.w.cycle()                                # the probe could not start tmux
    assert rig.alerts[-1].startswith("Memory is low: a process could not start.")
    _sample_passes(rig, 1)                       # back under: cleared
    assert rig.w.memory_warned is False
    with open(log, "a") as fh:
        fh.write("2026 ERROR fork: Resource temporarily unavailable\n")
    _sample_passes(rig, 1)
    assert rig.alerts[-1].startswith("Memory is low: a memory allocation failed.")
    assert len([b for b in rig.alerts if b.startswith("Memory is low")]) == 2


def test_deck_status_only_for_warn_or_bad(tmp_path):
    rig = Rig(tmp_path, samples=[_sample(300), _sample(11000), _sample(25000)], planned_restart=False)
    levels = []
    for _ in range(3):
        _sample_passes(rig, 1)
        h = json.loads(watch.health_path(rig.cfg).read_text())
        levels.append((h["level"], h["message"]))
    assert levels[0] == ("ok", "")
    assert levels[1] == ("warn", "health: tmux server holds 11,000 handles")
    assert levels[2][0] == "bad"


def test_nonzero_pane_exit_reads_the_application_log_once(tmp_path):
    rig = Rig(tmp_path)
    events = Path(rig.cfg.events_file)
    events.parent.mkdir(parents=True, exist_ok=True)
    events.write_text(json.dumps({"event": "PaneExit", "code": 9, "tmux_pane": "%1"}) + "\n")  # history
    rig.w.cycle()
    assert not (watch.health_dir(rig.cfg) / "pane-exits.jsonl").exists()
    at = watch._iso(rig.clock())
    crash = {"time": at, "id": 1000, "message": "Faulting application name: claude.exe"}
    old = {"time": watch._iso(rig.clock() - 900), "id": 1000, "message": "claude.exe long ago"}
    rig.w.spawn = lambda argv, timeout, env=None: subprocess.CompletedProcess(
        argv, 0, json.dumps([crash, old]), "")
    with open(events, "a") as fh:
        fh.write(json.dumps({"ts": at, "event": "PaneExit", "code": 3, "tmux_pane": "%4"}) + "\n")
        fh.write(json.dumps({"ts": at, "event": "PaneExit", "code": 0, "tmux_pane": "%5"}) + "\n")
    rig.w.cycle()
    lines = (watch.health_dir(rig.cfg) / "pane-exits.jsonl").read_text().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["code"] == 3 and rec["tmux_pane"] == "%4" and rec["application_events"] == [crash]


# --------------------------------------------------------------------------- planned restart


def _planned_rig(tmp_path, handles=25000, rows=None, typed_ago=3600, **kw):
    rig = Rig(tmp_path, rows=rows, **kw)
    rig.w.sampler = lambda winpid, **k: _sample(rig.handles)
    rig.handles = handles
    rig.tmux.clients = f"{int(rig.clock() - typed_ago)}\n"
    real_spawn = rig.spawn

    def spawn(argv, timeout, env=None):
        out = real_spawn(argv, timeout, env)
        if "--restart" in argv:                  # the detached part writes its result
            assert env["PANTHEON_SESSION"] == "pantheon"
            fullrestart.write_result(rig.cfg, {"resumed": [{"window_name": "garden_notes"}], "failed": []})
            os.utime(fullrestart.result_path(rig.cfg), (rig.clock() + 5, rig.clock() + 5))
        return out

    rig.w.spawn = spawn
    return rig


def _restarts(rig):
    return [a for a in rig.spawned if "--restart" in a]


def test_planned_restart_when_handles_high_agents_idle_and_nobody_typing(tmp_path):
    rig = _planned_rig(tmp_path)
    rig.w.cycle()
    assert _restarts(rig) == [[(tmp_path / "root" / "bin" / "pantheon").as_posix(),
                              "--restart", "--all", "--ready"]]
    assert "Planned restart (tmux handles 25,000)" in rig.alerts
    assert rig.alerts[-1] == "Pantheon restarted as planned: 1 of 1 sessions back"


def test_no_planned_restart_while_an_agent_is_mid_turn(tmp_path):
    rig = _planned_rig(tmp_path, rows=[_row(), _row(sid="s-2", status=AgentStatus.WORKING, window=4)])
    rig.w.cycle()
    assert _restarts(rig) == []


def test_no_planned_restart_while_someone_typed_recently(tmp_path):
    rig = _planned_rig(tmp_path, typed_ago=120)
    rig.w.cycle()
    assert _restarts(rig) == []


def test_no_planned_restart_below_the_threshold_or_when_off(tmp_path):
    rig = _planned_rig(tmp_path, handles=19999)
    rig.w.cycle()
    assert _restarts(rig) == []
    rig = _planned_rig(tmp_path / "off", planned_restart=False)
    rig.w.cycle()
    assert _restarts(rig) == []


def test_planned_restart_at_most_once_per_six_hours(tmp_path):
    rig = _planned_rig(tmp_path)
    rig.w.cycle()
    rig.clock.t += 2 * 3600
    rig.w.cycle()
    assert len(_restarts(rig)) == 1
    rig.clock.t += 4 * 3600 + 60
    rig.w.cycle()
    assert len(_restarts(rig)) == 2


def test_handles_back_above_half_within_an_hour_alerts_once_and_pauses_planning(tmp_path):
    rig = _planned_rig(tmp_path)
    rig.w.cycle()
    rig.handles = 12000
    rig.clock.t += 30 * 60
    rig.w.cycle()
    climbing = [b for b in rig.alerts if "within an hour of a planned restart" in b]
    assert len(climbing) == 1
    paused_until = rig.w.state["planned_paused_until"]
    assert paused_until > rig.clock()
    rig.clock.t += 60
    rig.w.cycle()
    assert len([b for b in rig.alerts if "within an hour of a planned restart" in b]) == 1
    rig.handles = 30000
    rig.clock.t = paused_until - 60              # still the same day: no planning
    rig.w.cycle()
    assert len(_restarts(rig)) == 1
    rig.clock.t = paused_until + 60              # the next day: planning is back
    rig.w.cycle()
    assert len(_restarts(rig)) == 2


def test_a_planned_restart_that_did_not_go_ahead_retries_later_not_every_pass(tmp_path):
    rig = _planned_rig(tmp_path, spawn_rc={"pantheon": 1})
    rig.w.cycle()
    rig.clock.t += 60
    rig.w.cycle()
    assert len(_restarts(rig)) == 1
    assert "The planned restart did not go ahead; nothing was ended." in rig.alerts
    rig.clock.t += 10 * 60
    rig.w.cycle()
    assert len(_restarts(rig)) == 2


# --------------------------------------------------------------------------- hourly handle log (step 7)


def _hourly(rig):
    path = watch.hourly_path(rig.cfg)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _claude_sample(handles=300, claude=((11, 136), (12, 407))):
    s = _sample(handles)
    s.update(claude=[{"pid": pid, "name": "claude", "handles": h, "threads": 9, "private_mb": 100.0}
                     for pid, h in claude],
             commit_used_mb=29715.9, commit_limit_mb=40884.2)
    return s


def test_hourly_log_one_line_per_utc_hour_and_latest_sample_still_written(tmp_path):
    rig = Rig(tmp_path, samples=[_claude_sample(600 + i) for i in range(200)], planned_restart=False)
    start = rig.clock()                          # 1_800_000_000 is the top of a UTC hour
    rig.w.loop(max_cycles=2 * 120 + 1)           # two hours and one more pass, at 30 s
    lines = _hourly(rig)
    assert [line["ts"] for line in lines] == [watch._iso(start), watch._iso(start + 3600),
                                              watch._iso(start + 7200)]
    first = lines[0]
    assert first["server_handles"] == 600 and first["server_winpid"] == 9001
    assert first["server_msys_pid"] == 77
    assert first["commit_percent"] == 50.0 and first["commit_used_mb"] == 29715.9
    assert first["claude"] == [{"pid": 11, "handles": 136, "private_mb": 100.0},
                               {"pid": 12, "handles": 407, "private_mb": 100.0}]
    assert first["claude_count"] == 2 and first["claude_handles_total"] == 543
    # the latest-sample file the deck reads is still rewritten on every sample, not hourly
    health = json.loads(watch.health_path(rig.cfg).read_text())
    assert health["server_handles"] == 600 + 120 and health["written_at"] == watch._iso(start + 7200)


def test_hourly_log_skips_a_failed_sample_and_takes_the_next_good_one(tmp_path):
    bad = {"ok": False, "error": "powershell timed out", "server": None, "claude": []}
    rig = Rig(tmp_path, samples=[bad, _claude_sample(700)], planned_restart=False)
    _sample_passes(rig, 1)
    assert _hourly(rig) == []
    _sample_passes(rig, 1)
    lines = _hourly(rig)
    assert len(lines) == 1 and lines[0]["server_handles"] == 700


def test_hourly_log_a_restarted_watcher_does_not_repeat_the_hour(tmp_path):
    rig = Rig(tmp_path, planned_restart=False)
    _sample_passes(rig, 1)
    assert len(_hourly(rig)) == 1
    again = Rig(tmp_path, planned_restart=False)  # same state folder, same clock start
    again.clock.t = rig.clock.t
    _sample_passes(again, 2)
    assert len(_hourly(again)) == 1
    again.clock.t += 3600
    _sample_passes(again, 1)
    assert len(_hourly(again)) == 2


def test_last_hour_written_reads_the_newest_line_and_survives_junk(tmp_path):
    path = tmp_path / "h.jsonl"
    assert watch.last_hour_written(path) == ""
    path.write_text('{"ts": "2026-10-07T08:00:05Z"}\n{"ts": "2026-10-07T09:00:31Z"}\nnot json\n',
                    encoding="utf-8")
    assert watch.last_hour_written(path) == "2026-10-07T09"


# --------------------------------------------------------------------------- empty panes


class PaneTmux(FakeTmux):
    """FakeTmux that also answers `list-panes` and records `kill-pane`."""

    def __init__(self, panes):
        super().__init__()
        self.panes = panes
        self.killed = []

    def __call__(self, *args, tmux=None, check=False):
        if args[0] == "list-panes":
            self.calls.append(args)
            return _cp(0, "".join(self.panes))
        if args[0] == "kill-pane":
            self.calls.append(args)
            self.killed.append(args[2])
            return _cp(0)
        return super().__call__(*args, tmux=tmux, check=check)


def _pane(pane, pid, activity, name, command="bash", dead="0"):
    return f"{pane}|{pid}|{dead}|{activity}|{command}|{name}\n"


def test_only_empty_shells_untouched_for_five_minutes_are_closed(tmp_path):
    rig = Rig(tmp_path)
    now = int(rig.clock())
    old, fresh = now - 6 * 60, now - 60
    proc = tmp_path / "proc"
    for child, parent in (("77", "1"), ("500", "100"), ("501", "200"), ("502", "300")):
        (proc / child).mkdir(exist_ok=True)
        (proc / child / "ppid").write_text(parent + "\n")
    rig.w.run = rig.tmux = PaneTmux([
        _pane("%1", "100", old, "DECK"),                      # deck window: never
        _pane("%2", "200", old, "garden_notes"),               # bash with python under it
        _pane("%3", "300", old, "deal_crawler", "C:\\Users\\x\\.local\\bin\\claude.exe"),
        _pane("%4", "400", old, "work_stuff"),                # empty bash, 6 min: closed
        _pane("%5", "401", fresh, "obsidian"),                # empty bash, 1 min: kept
        _pane("%6", "402", old, "C:\\x\\pwsh.exe", "C:\\x\\pwsh.exe"),   # native shell: kept
        _pane("%7", "403", old, "gone", "claude", dead="1"),  # dead pane: closed
        _pane("%4", "400", old, "work_stuff"),                # grouped session's copy
        _pane("%8", "404", old, "notes|draft"),               # "|" in the name: still closed
    ])
    assert rig.w.close_empty_panes() == ["%4", "%7", "%8"]
    assert rig.tmux.killed == ["%4", "%7", "%8"]


def test_a_live_process_whose_parent_cannot_be_read_closes_nothing(tmp_path):
    rig = Rig(tmp_path)
    (tmp_path / "proc" / "77" / "ppid").write_text("1\n")
    (tmp_path / "proc" / "600").mkdir()                       # alive, but no readable ppid
    rig.w.run = rig.tmux = PaneTmux([_pane("%4", "400", 0, "work_stuff")])
    assert rig.w.close_empty_panes() == [] and rig.tmux.killed == []


def test_empty_panes_off_and_unreadable_proc_close_nothing(tmp_path):
    rig = Rig(tmp_path, close_empty_minutes=0)
    rig.w.run = rig.tmux = PaneTmux([_pane("%4", "400", 0, "work_stuff")])
    assert rig.w.close_empty_panes() == [] and rig.tmux.killed == []
    rig = Rig(tmp_path)
    rig.w.proc_root = str(tmp_path / "no-such-proc")
    rig.w.run = rig.tmux = PaneTmux([_pane("%4", "400", 0, "work_stuff")])
    assert rig.w.close_empty_panes() == [] and rig.tmux.killed == []


def test_the_empty_pane_check_runs_once_a_minute_while_healthy(tmp_path):
    rig = Rig(tmp_path, planned_restart=False)
    rig.w.run = rig.tmux = PaneTmux([])
    rig.w.cycle()
    rig.w.cycle()
    rig.clock.t += watch.EMPTY_CHECK_SECONDS
    rig.w.cycle()
    assert sum(1 for c in rig.tmux.calls if c[0] == "list-panes") == 2
