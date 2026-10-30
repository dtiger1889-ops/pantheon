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


def test_a_fire_running_at_the_same_moment_starts_nothing(tmp_path):
    cfg = _cfg(tmp_path)
    fake = FakeSchtasks()
    made = schedule_mod.create(cfg, _project(cfg), AT, "claude", run=fake)
    opened: list = []

    def opener(args):
        # While the first fire is inside its start, a second one reads the same "waiting" note.
        if not opened:
            opened.append(args)
            assert "already being fired" in schedule_mod.fire(cfg, made.id, run=fake, opener=opener).message
        return 0, "ok"

    assert schedule_mod.fire(cfg, made.id, run=fake, opener=opener).ok
    assert len(opened) == 1
    assert not list(schedule_mod.scheduled_dir(cfg).glob("*.firing"))


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


# ---------------------------------------------------------------- `--at reset` through the CLI


def test_at_reset_schedules_a_new_session_at_the_reset_plus_margin(tmp_path, monkeypatch, capsys):
    cfg = _cfg(tmp_path)
    created = _wire(monkeypatch, cfg, None)
    reset = datetime.now().astimezone() + timedelta(hours=2)
    monkeypatch.setattr(plan, "read_hud", lambda cfg_: {
        "fetched_at": _utc(datetime.now(timezone.utc)),
        "claude": {"fetched_at": _utc(datetime.now(timezone.utc)), "five_hour_resets_at": _utc(reset)}})
    assert cli_mod.main(["hiking_log_v2", "--at", "reset", "--model", "sonnet"]) == 0
    at = created[0][1]
    assert 119 * 60 <= (at - datetime.now().astimezone()).total_seconds() <= 124 * 60 + 60 and at.second == 0


def test_at_reset_without_a_usage_file_schedules_nothing(tmp_path, monkeypatch, capsys):
    cfg = _cfg(tmp_path)
    created = _wire(monkeypatch, cfg, None)
    monkeypatch.setattr(plan, "read_hud", lambda cfg_: None)
    assert cli_mod.main(["hiking_log_v2", "--at", "reset"]) != 0 and created == []
    out = capsys.readouterr().out
    assert "no usage file" in out and "Nothing was scheduled" in out


def _utc(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


# ---------------------------------------------------------------- a prompt into an EXISTING session

from pantheon import session_ctl                      # noqa: E402
from pantheon import tmuxctl                          # noqa: E402
from pantheon.models import TmuxWindow                # noqa: E402
from pantheon.schedule import deliver as deliver_mod  # noqa: E402

IDLE = "● Done.\n" + "─" * 80 + "\n> \n" + "─" * 80 + "\n  5h 29% · wk 54% · Sonnet 5\n"
BUSY = "● Working\n" + "─" * 80 + "\n> half a sentence\n" + "─" * 80 + "\n"
QUESTION = "Do you want to proceed?\n  1. Yes\n  2. No\n  Esc to cancel\n"


class FakeDeck:
    """tmux, the screen and the notifier, all fake. `screens` is consumed one per look."""

    def __init__(self, monkeypatch, windows, screens=(), claude=True):
        self.windows = list(windows)
        self.screens = list(screens)
        self.ran: list[tuple] = []
        self.slept: list[float] = []
        self.notices: list[tuple] = []
        monkeypatch.setattr(tmuxctl, "list_windows", lambda session=None, tmux=None: list(self.windows))
        monkeypatch.setattr(session_ctl, "is_claude_window", lambda target, tmux=None: claude)

        def run(*args, tmux=None, check=False):
            self.ran.append(args)
            return subprocess.CompletedProcess(args, 0, "", "")

        monkeypatch.setattr(tmuxctl, "run", run)

    def capture(self, target, tmux=None):
        return self.screens.pop(0) if len(self.screens) > 1 else self.screens[0]

    def notifier(self, title, body):
        self.notices.append((title, body))

    def go(self, cfg, manifest, **kw):
        kw.setdefault("retry_seconds", 60)
        return deliver_mod.run(cfg, manifest, tmux="tmux", capture=self.capture, sleep=self.slept.append,
                               notifier=self.notifier, **kw)

    def typed(self):
        return [a for a in self.ran if a[0] == "set-buffer"]


def _win(name="research", index=4):
    return TmuxWindow(index, name, "node", "C:/x", f"%{index}", "pantheon")


def _note(**over):
    note = {"kind": "prompt", "target": "research", "prompt": "carry on with step 3", "or_new": False}
    note.update(over)
    return note


def test_prompt_is_pasted_and_entered_when_the_session_is_idle(tmp_path, monkeypatch):
    deck = FakeDeck(monkeypatch, [_win()], [IDLE])
    code, said = deck.go(_cfg(tmp_path), _note())
    assert code == 0 and "typed into 'research'" in said
    [(verb, flag, buffer, dashes, text)] = deck.typed()
    assert buffer.startswith("pantheon-schedule-") and text == "carry on with step 3"
    assert ("send-keys", "-t", "%4", "Enter") in deck.ran
    assert deck.notices == [] and deck.slept == [0.5, 1.0]


def test_a_busy_session_is_retried_each_minute_then_typed_into(tmp_path, monkeypatch):
    deck = FakeDeck(monkeypatch, [_win()], [BUSY, QUESTION, IDLE])
    code, said = deck.go(_cfg(tmp_path), _note())
    assert code == 0 and "look 3" in said
    assert deck.slept[:2] == [60, 60] and len(deck.typed()) == 1          # nothing typed on the busy looks


def test_a_session_busy_to_the_cap_is_given_up_on_and_announced(tmp_path, monkeypatch):
    deck = FakeDeck(monkeypatch, [_win()], [BUSY])
    code, said = deck.go(_cfg(tmp_path), _note(), max_tries=4)
    assert code == 1 and "stayed busy" in said and "4 looks" in said
    assert deck.typed() == [] and deck.slept == [60, 60, 60]
    assert deck.notices and "research" in deck.notices[0][0] and said == deck.notices[0][1]


def test_a_gone_session_is_logged_and_no_new_one_is_started(tmp_path, monkeypatch):
    deck = FakeDeck(monkeypatch, [_win("other")], [IDLE])
    opened: list = []
    code, said = deck.go(_cfg(tmp_path), _note(), opener=lambda a: opened.append(a) or (0, "x"))
    assert code == 1 and "is gone" in said and "--or-new" in said
    assert opened == [] and deck.typed() == [] and len(deck.notices) == 1


def test_a_newer_session_that_took_the_name_is_not_typed_into(tmp_path, monkeypatch):
    # Scheduled while 'research' was pane %3; that conversation closed and a new 'research' is %4.
    deck = FakeDeck(monkeypatch, [_win("research", 4)], [IDLE])
    cfg = _cfg(tmp_path)
    code, said = deck.go(cfg, _note(pane_id="%3"))
    assert code == 1 and "is gone" in said and deck.typed() == []
    code, said = deck.go(cfg, _note(pane_id="%4"))
    assert code == 0


def test_scheduling_records_the_pane_that_has_the_name(tmp_path, monkeypatch):
    FakeDeck(monkeypatch, [_win("research", 4), _win("other", 5)])
    cfg = _cfg(tmp_path)
    assert deliver_mod.pane_now(cfg, "Research") == "%4"
    assert deliver_mod.pane_now(cfg, "missing") is None


def test_or_new_starts_a_new_session_when_the_old_one_is_gone(tmp_path, monkeypatch):
    deck = FakeDeck(monkeypatch, [], [IDLE])
    opened: list = []
    note = _note(or_new=True, open_args=["open", "C:/p/research", "--who", "claude", "--message", "carry on with step 3"])
    code, said = deck.go(_cfg(tmp_path), note, opener=lambda a: opened.append(a) or (0, "claude is open in window 9"))
    assert code == 0 and "new session was started" in said
    assert opened == [note["open_args"]] and deck.typed() == [] and deck.notices == []


def test_window_name_match_ignores_case_and_refuses_a_duplicate(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    deck = FakeDeck(monkeypatch, [_win("Research", 4), _win("research", 5)], [IDLE])
    code, said = deck.go(cfg, _note())
    assert code == 1 and "2 session windows" in said and deck.typed() == []
    deck2 = FakeDeck(monkeypatch, [_win("Research", 4)], [IDLE])
    assert deck2.go(cfg, _note())[0] == 0


def test_a_window_not_running_claude_is_never_typed_into(tmp_path, monkeypatch):
    deck = FakeDeck(monkeypatch, [_win()], [IDLE], claude=False)
    code, said = deck.go(_cfg(tmp_path), _note())
    assert code == 1 and "not running Claude Code" in said and deck.typed() == []


def test_notify_uses_only_the_switched_on_channels(tmp_path):
    cfg = _cfg(tmp_path)
    calls: list[str] = []
    out = deliver_mod.notify(cfg, "t", "b",
                             send_toast=lambda *a: calls.append("toast") or (True, "ok"),
                             send_ntfy=lambda *a: calls.append("ntfy") or (True, "ok"),
                             send_telegram=lambda *a: calls.append("telegram") or (True, "ok"))
    assert calls == ["toast"] and out == [("toast", True, "ok")]     # toast on, ntfy and telegram off by default


def test_create_prompt_writes_a_prompt_note_and_the_same_task_scheduler_entry(tmp_path):
    cfg = _cfg(tmp_path)
    fake = FakeSchtasks()
    result = schedule_mod.create_prompt(cfg, "research", "carry on", AT, run=fake, now=1790000000)
    assert result.ok and result.id == "pantheon-start-prompt-research-1790000000"
    assert fake.calls[0][fake.calls[0].index("/tn") + 1] == result.id
    note = schedule_mod.read(cfg, result.id)
    assert note["kind"] == "prompt" and note["target"] == "research" and note["prompt"] == "carry on"
    assert note["status"] == "waiting" and note["or_new"] is False and "open_args" not in note


def test_fire_of_a_prompt_note_records_sent_and_removes_the_task(tmp_path):
    cfg = _cfg(tmp_path)
    fake = FakeSchtasks()
    made = schedule_mod.create_prompt(cfg, "research", "carry on", AT, run=fake)
    seen: list[dict] = []

    def deliverer(cfg_, manifest, opener=None):
        seen.append(dict(manifest))
        return 0, "prompt typed into 'research'"

    result = schedule_mod.fire(cfg, made.id, run=fake, deliverer=deliverer)
    assert result.ok and seen[0]["status"] == "trying"
    assert schedule_mod.read(cfg, made.id)["status"] == "sent"
    assert fake.calls[-1][1:] == ["/delete", "/tn", made.id, "/f"]
    assert not schedule_mod.fire(cfg, made.id, run=fake, deliverer=deliverer).ok and len(seen) == 1


def test_a_cancel_between_looks_stops_the_retry_loop(tmp_path, monkeypatch):
    deck = FakeDeck(monkeypatch, [_win()], [BUSY, IDLE])
    asked: list[int] = []
    code, said = deck.go(_cfg(tmp_path), _note(), cancelled=lambda: asked.append(1) or len(asked) > 1)
    assert code == 1 and "cancelled" in said
    assert deck.typed() == [] and deck.notices == []


def test_cancelling_a_prompt_that_is_retrying_types_nothing_and_stays_cancelled(tmp_path, monkeypatch):
    # The fire was already waiting out a busy screen
    # when --cancel ran; the old loop typed the prompt anyway and wrote the deleted note back.
    cfg = _cfg(tmp_path)
    fake = FakeSchtasks()
    made = schedule_mod.create_prompt(cfg, "research", "carry on", AT, run=fake)
    deck = FakeDeck(monkeypatch, [_win()], [BUSY, IDLE])
    cancelled: list = []

    def on_sleep(seconds):
        deck.slept.append(seconds)
        if seconds == 60 and not cancelled:
            cancelled.append(schedule_mod.cancel(cfg, made.id, run=fake))

    real = deliver_mod.run
    monkeypatch.setattr(deliver_mod, "run", lambda c, m, **kw: real(
        c, m, tmux="tmux", capture=deck.capture, sleep=on_sleep, notifier=deck.notifier, **kw))
    result = schedule_mod.fire(cfg, made.id, run=fake)
    assert cancelled and cancelled[0].ok
    assert not result.ok and deck.typed() == []
    assert schedule_mod.read(cfg, made.id) is None


def test_fire_of_a_prompt_note_that_was_not_sent_says_so(tmp_path):
    cfg = _cfg(tmp_path)
    made = schedule_mod.create_prompt(cfg, "research", "carry on", AT, run=FakeSchtasks())
    schedule_mod.fire(cfg, made.id, run=FakeSchtasks(), deliverer=lambda c, m, opener=None: (1, "the session 'research' is gone"))
    note = schedule_mod.read(cfg, made.id)
    assert note["status"] == "did not send" and "gone" in note["result"]


def test_a_new_session_note_is_unchanged_by_any_of_this(tmp_path):
    cfg = _cfg(tmp_path)
    made = schedule_mod.create(cfg, _project(cfg), AT, "claude", {"effort": "high"}, "hello", run=FakeSchtasks())
    note = schedule_mod.read(cfg, made.id)
    assert "kind" not in note and note["open_args"][-2:] == ["--message", "hello"]
    ran = schedule_mod.fire(cfg, made.id, run=FakeSchtasks(), opener=lambda a: (0, "ok"),
                            deliverer=lambda *a, **k: pytest.fail("a start note must not deliver a prompt"))
    assert ran.ok and schedule_mod.read(cfg, made.id)["status"] == "started"


def _wire_prompt(monkeypatch, cfg, names=("research",)):
    created: list[dict] = []
    monkeypatch.setattr(config_mod, "load", lambda *a, **k: cfg)
    monkeypatch.setattr(plan, "warn_for", lambda *a, **k: None)
    monkeypatch.setattr(tmuxctl, "list_windows",
                        lambda session=None, tmux=None: [_win(n, i) for i, n in enumerate(names)])

    def fake(cfg_, target, prompt, at, **kw):
        created.append({"target": target, "prompt": prompt, "at": at, **kw})
        return schedule_mod.ScheduleResult(True, "prompt for 'research' is typed in Sun 27 Sep 06:00", "id-1",
                                           {"command": "type into 'research': x"})

    monkeypatch.setattr(schedule_mod, "create_prompt", fake)
    return created


def test_cli_session_prompt_schedules_a_prompt(tmp_path, monkeypatch, capsys):
    cfg = _cfg(tmp_path)
    created = _wire_prompt(monkeypatch, cfg)
    assert cli_mod.main(["--session", "research", "--at", "06:00", "--prompt", "carry on"]) == 0
    assert created[0]["target"] == "research" and created[0]["prompt"] == "carry on" and created[0]["or_new"] is False
    assert "--cancel" in capsys.readouterr().out


def test_cli_prompt_file_is_read_now(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    created = _wire_prompt(monkeypatch, cfg)
    f = tmp_path / "p.txt"
    f.write_text("line one\nline two\n", encoding="utf-8")
    assert cli_mod.main(["--session", "research", "--at", "06:00", "--prompt-file", str(f)]) == 0
    assert created[0]["prompt"] == "line one\nline two\n"


def test_cli_or_new_needs_a_project_and_passes_it_on(tmp_path, monkeypatch, capsys):
    cfg = _cfg(tmp_path)
    created = _wire_prompt(monkeypatch, cfg)
    assert cli_mod.main(["--session", "research", "--at", "06:00", "--prompt", "x", "--or-new"]) != 0
    assert "needs the project" in capsys.readouterr().out and created == []
    assert cli_mod.main(["hiking_log_v2", "--session", "research", "--at", "06:00", "--prompt", "x", "--or-new"]) == 0
    assert created[0]["or_new"] is True and created[0]["project_dir"].endswith("hiking_log_v2")


def test_cli_refuses_a_session_name_tmux_does_not_have_unless_forced(tmp_path, monkeypatch, capsys):
    cfg = _cfg(tmp_path)
    created = _wire_prompt(monkeypatch, cfg, names=("other", "third"))
    assert cli_mod.main(["--session", "reserach", "--at", "06:00", "--prompt", "x"]) != 0
    out = capsys.readouterr().out
    assert "no session window is named 'reserach'" in out and "other, third" in out and created == []
    assert cli_mod.main(["--session", "reserach", "--at", "06:00", "--prompt", "x", "--force"]) == 0


def test_cli_cannot_check_the_name_when_tmux_is_silent_and_schedules_anyway(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    created = _wire_prompt(monkeypatch, cfg, names=())
    assert cli_mod.main(["--session", "research", "--at", "06:00", "--prompt", "x"]) == 0 and len(created) == 1


@pytest.mark.parametrize("argv, words", [
    (["--session", "research", "--at", "06:00"], "exactly one of --prompt"),
    (["--session", "research", "--at", "06:00", "--prompt", "a", "--prompt-file", "b"], "exactly one of --prompt"),
    (["--session", "research", "--at", "06:00", "--prompt", "  "], "prompt is empty"),
    (["--session", "research", "--at", "06:00", "--prompt-file", "/no/such/file"], "could not read"),
    (["hiking_log_v2", "--at", "06:00", "--prompt", "x"], "--session NAME"),
    (["--session", "research", "--at", "reset", "--prompt", "x"], "no usage file"),
])
def test_cli_prompt_refusals_schedule_nothing(tmp_path, monkeypatch, capsys, argv, words):
    cfg = _cfg(tmp_path)
    created = _wire_prompt(monkeypatch, cfg)
    monkeypatch.setattr(plan, "read_hud", lambda cfg_: None)
    assert cli_mod.main(argv) != 0 and created == []
    assert words in capsys.readouterr().out


def test_list_shows_a_prompt_note_by_its_target(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    schedule_mod.create_prompt(cfg, "research", "carry on", AT, or_new=True, run=FakeSchtasks())
    cli_mod._show_list(cfg)
    assert "prompt -> research  (or new)" in capsys.readouterr().out
