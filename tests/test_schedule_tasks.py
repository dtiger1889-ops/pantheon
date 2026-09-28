"""build B, the Task Scheduler half and `bin/schedule` (acceptance 3 and 4). Every `schtasks`
call goes to a fake; no test here ever creates a real scheduled task."""
from __future__ import annotations

import dataclasses
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from pantheon import config as config_mod
from pantheon.schedule import cli as cli_mod
from pantheon.schedule import plan
from pantheon.schedule import schedule as schedule_mod

AT = datetime(2026, 9, 27, 6, 0, tzinfo=timezone(timedelta(hours=-4)))


class FakeSchtasks:
    def __init__(self, returncode: int = 0, stderr: str = "") -> None:
        self.calls: list[list[str]] = []
        self.returncode = returncode
        self.stderr = stderr

    def __call__(self, argv):
        self.calls.append(list(argv))
        return subprocess.CompletedProcess(argv, self.returncode, "SUCCESS", self.stderr)


def _cfg(tmp_path):
    root = tmp_path / "Claude"
    (root / "hiking_log_v2").mkdir(parents=True)
    (root / "hiking_log_v2" / "CLAUDE.md").write_text("x", encoding="utf-8")
    cfg = config_mod.Config(vault=str(tmp_path / "vault"), state_dir=str(tmp_path / "state"))
    return dataclasses.replace(cfg, projects_root=str(root))


def _project(cfg) -> str:
    return str(Path(cfg.projects_root) / "hiking_log_v2").replace("\\", "/")


# ---------------------------------------------------------------- acceptance 3


def test_create_records_the_exact_command_and_calls_schtasks_once(tmp_path):
    cfg = _cfg(tmp_path)
    fake = FakeSchtasks()
    result = schedule_mod.create(cfg, _project(cfg), AT, "claude", {"model": "sonnet", "effort": "high"},
                                 "carry on from CHECKPOINT.md", run=fake, now=1790000000)
    assert result.ok
    assert result.id == "pantheon-start-hiking-log-v2-1790000000"
    assert len(fake.calls) == 1
    argv = fake.calls[0]
    assert argv[1:5] == ["/create", "/sc", "once", "/st"]
    assert argv[argv.index("/st") + 1] == "06:00"
    assert argv[argv.index("/sd") + 1] == "09/27/2026"
    assert argv[argv.index("/tn") + 1] == result.id
    assert argv[-1] == "/f"
    task_run = argv[argv.index("/tr") + 1]
    assert "ssh.exe -o BatchMode=yes localhost" in task_run and f"bin/schedule --fire {result.id}" in task_run
    manifest = json.loads((Path(cfg.state_dir) / "scheduled" / f"{result.id}.json").read_text(encoding="utf-8"))
    assert manifest["command"].startswith(f"pantheon open {_project(cfg)} --who claude --model sonnet --effort high")
    assert manifest["open_args"][-2:] == ["--message", "carry on from CHECKPOINT.md"]
    assert manifest["status"] == "waiting" and manifest["task_run"] == task_run


def test_list_then_cancel_removes_the_task_and_the_note(tmp_path):
    cfg = _cfg(tmp_path)
    fake = FakeSchtasks()
    made = schedule_mod.create(cfg, _project(cfg), AT, "claude", {"model": "sonnet", "effort": "high"}, run=fake)
    listed = schedule_mod.list_staged(cfg)
    assert [m["id"] for m in listed] == [made.id]
    assert "--model sonnet --effort high" in listed[0]["command"]

    gone = schedule_mod.cancel(cfg, made.id, run=fake)
    assert gone.ok
    assert fake.calls[-1][1:] == ["/delete", "/tn", made.id, "/f"]
    assert schedule_mod.list_staged(cfg) == []


def test_a_refused_create_writes_no_note(tmp_path):
    cfg = _cfg(tmp_path)
    result = schedule_mod.create(cfg, _project(cfg), AT, run=FakeSchtasks(1, "ERROR: Access is denied."))
    assert not result.ok and "Access is denied" in result.message
    assert schedule_mod.list_staged(cfg) == []


def test_a_waiting_start_whose_task_will_not_delete_keeps_its_note(tmp_path):
    cfg = _cfg(tmp_path)
    made = schedule_mod.create(cfg, _project(cfg), AT, run=FakeSchtasks())
    result = schedule_mod.cancel(cfg, made.id, run=FakeSchtasks(1, "ERROR: Access is denied."))
    assert not result.ok
    assert [m["id"] for m in schedule_mod.list_staged(cfg)] == [made.id]


def test_fire_starts_the_session_records_how_it_went_and_removes_the_task(tmp_path):
    cfg = _cfg(tmp_path)
    fake = FakeSchtasks()
    made = schedule_mod.create(cfg, _project(cfg), AT, "claude", {"effort": "high"}, run=fake)
    opened: list[list[str]] = []

    def opener(args):
        opened.append(args)
        return 0, "claude is open in window 9 (hiking_log_v2); effort set to high"

    result = schedule_mod.fire(cfg, made.id, run=fake, opener=opener)
    assert result.ok
    assert opened == [["open", _project(cfg), "--who", "claude", "--effort", "high"]]
    assert fake.calls[-1][1:] == ["/delete", "/tn", made.id, "/f"]
    note = schedule_mod.read(cfg, made.id)
    assert note["status"] == "started" and "window 9" in note["result"]
    # A second fire (Task Scheduler retrying, a hand run) starts nothing more.
    again = schedule_mod.fire(cfg, made.id, run=fake, opener=opener)
    assert not again.ok and len(opened) == 1


def test_fire_writes_down_a_start_that_failed(tmp_path):
    cfg = _cfg(tmp_path)
    made = schedule_mod.create(cfg, _project(cfg), AT, run=FakeSchtasks())
    result = schedule_mod.fire(cfg, made.id, run=FakeSchtasks(),
                               opener=lambda args: (1, "tmux would not open a window; is pantheon running?"))
    assert not result.ok
    assert schedule_mod.read(cfg, made.id)["status"] == "did not start"


def test_the_fire_command_uses_an_msys_path():
    line = schedule_mod.fire_command("pantheon-start-x-1", root="C:/Home/x/Documents/Projects/project_lanterns")
    assert "/c/Home/x/Documents/Projects/project_lanterns/bin/schedule --fire pantheon-start-x-1" in line
    assert len(line) < 261   # Task Scheduler's limit on the command it runs


# ---------------------------------------------------------------- acceptance 4 (`bin/schedule`)


def _wire(monkeypatch, cfg, warning):
    created: list[tuple] = []
    monkeypatch.setattr(config_mod, "load", lambda *a, **k: cfg)
    monkeypatch.setattr(plan, "warn_for", lambda *a, **k: warning)

    def fake_create(cfg_, project_dir, at, who, options, message, warning=None):
        created.append((project_dir, at, who, options, message, warning))
        return schedule_mod.ScheduleResult(True, "hiking_log_v2 starts Sun 27 Sep 06:00", "pantheon-start-x-1",
                                           {"command": "pantheon open x"})

    monkeypatch.setattr(schedule_mod, "create", fake_create)
    return created


def test_a_warning_without_force_exits_non_zero_and_schedules_nothing(tmp_path, monkeypatch, capsys):
    cfg = _cfg(tmp_path)
    created = _wire(monkeypatch, cfg, "06:00 lands inside the five-hour window, already 92% used (resets 06:10); it may wind down fast.")
    rc = cli_mod.main(["hiking_log_v2", "--at", "06:00", "--model", "sonnet"])
    out = capsys.readouterr().out
    assert rc != 0 and created == []
    assert "92%" in out and "--force" in out


def test_a_warning_with_force_schedules(tmp_path, monkeypatch, capsys):
    cfg = _cfg(tmp_path)
    created = _wire(monkeypatch, cfg, "06:00 lands inside the five-hour window, already 92% used (resets 06:10); it may wind down fast.")
    rc = cli_mod.main(["hiking_log_v2", "--at", "06:00", "--model", "sonnet", "--effort", "high", "--force"])
    assert rc == 0 and len(created) == 1
    project_dir, _at, who, options, _message, warning = created[0]
    assert project_dir.endswith("hiking_log_v2") and who == "claude"
    assert options == {"model": "sonnet", "effort": "high"} and "92%" in warning


def test_no_warning_schedules_without_force(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    created = _wire(monkeypatch, cfg, None)
    assert cli_mod.main(["hiking_log_v2", "--at", "06:00"]) == 0 and len(created) == 1


@pytest.mark.parametrize("argv, words", [
    (["hiking_log_v2"], "--at"),
    (["--at", "06:00"], "which project"),
    (["no-such-folder", "--at", "06:00"], "not there"),
    (["hiking_log_v2", "--at", "6pm"], "HH:MM"),
    (["hiking_log_v2", "--at", "06:00", "--who", "codex-headless"], "first message"),
])
def test_plain_refusals_schedule_nothing(tmp_path, monkeypatch, capsys, argv, words):
    cfg = _cfg(tmp_path)
    created = _wire(monkeypatch, cfg, None)
    assert cli_mod.main(argv) != 0 and created == []
    assert words in capsys.readouterr().out
