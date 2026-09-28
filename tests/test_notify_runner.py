"""The notify runner's tick, against a fake tmux, faked
senders, and a fixed clock -- no real tmux server, no real subprocess, no real network. Mirrors
`tests/test_governor_runner.py`'s shape.
"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from pantheon import config as config_mod
from pantheon import events as events_mod
from pantheon import tmuxctl
from pantheon.governor import parked as governor_parked
from pantheon.models import Event, TmuxWindow
from pantheon.notify import rules, runner as runner_mod, store as store_mod

NOW = datetime(2026, 9, 2, 20, 0, 0, tzinfo=timezone.utc)


def _iso(dt) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def cfg_for(tmp_path, notify=None, **kw):
    kw.setdefault("state_dir", str(tmp_path / "state"))
    kw.setdefault("tmux_session", "pantheon")
    kw["notify"] = notify or {}
    return config_mod.Config(**kw)


def write_hud(cfg, claude: dict, fetched_at=None) -> None:
    cfg.hud_file.parent.mkdir(parents=True, exist_ok=True)
    picture = {"fetched_at": fetched_at or _iso(NOW), "claude": claude, "codex": {}, "history": {}}
    cfg.hud_file.write_text(json.dumps(picture), encoding="utf-8")


def sent_lines(cfg) -> list[dict]:
    path = cfg.notify_sent_file
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class FakeTmux:
    """Same idiom as `test_governor_runner.FakeTmux`: records every call, plays back a fixed
    window list and an optional attached-client list."""

    def __init__(self, windows=None, clients=None):
        self.calls: list[tuple] = []
        self.windows = list(windows or [])
        self.clients = list(clients or [])  # [(tty, session, activity_epoch_str)]

    def run(self, *args, tmux=None, check=False):
        self.calls.append(args)
        out = ""
        if args[0] == "list-windows":
            lines = [f"{w.index}|{w.name}|{w.command}|{w.path}|{w.pane_id}|{w.session}" for w in self.windows]
            out = "\n".join(lines) + ("\n" if lines else "")
        elif args[0] == "list-clients":
            lines = [f"{tty}|{sess}|{act}" for tty, sess, act in self.clients]
            out = "\n".join(lines) + ("\n" if lines else "")
        return subprocess.CompletedProcess(args, 0, out, "")


class FakeSenders:
    """Records every send; always "succeeds" unless told to fail."""

    def __init__(self, fail: set[str] = frozenset()):
        self.calls: list[tuple] = []
        self.fail = fail

    def toast(self, title, body, pwsh):
        self.calls.append(("toast", title, body))
        return (False, "boom") if "toast" in self.fail else (True, "toast started")

    def ntfy(self, url, topic, title, body, priority):
        self.calls.append(("ntfy", title, body))
        return (False, "boom") if "ntfy" in self.fail else (True, "ntfy sent")

    def telegram(self, text, chat_id, credential_target, pwsh):
        self.calls.append(("telegram", text))
        return (False, "boom") if "telegram" in self.fail else (True, "telegram sent")


def make_runner(cfg, fake_tmux, senders=None, now=NOW):
    senders = senders or FakeSenders()
    return runner_mod.NotifyRunner(
        cfg=cfg, sleep=lambda s: None, now_fn=lambda: now,
        send_toast=senders.toast, send_ntfy=senders.ntfy, send_telegram=senders.telegram,
    )


# ---------------------------------------------------------------- disabled


def test_disabled_notifier_does_nothing(tmp_path, monkeypatch):
    cfg = cfg_for(tmp_path, notify={"enabled": False})
    fake = FakeTmux()
    monkeypatch.setattr(tmuxctl, "run", fake.run)
    make_runner(cfg, fake).tick()
    assert fake.calls == []
    assert sent_lines(cfg) == []
    assert not cfg.notify_last_file.exists()


# ---------------------------------------------------------------- acceptance 2/3: dry run + presence


def test_dry_run_needs_you_after_threshold_writes_exactly_one_line_when_not_present(tmp_path, monkeypatch):
    cfg = cfg_for(tmp_path, notify={"enabled": True, "dry_run": True, "needs_you_after_seconds": 60})
    win = TmuxWindow(3, "loom-os", "node", "C:/Home/x/Documents/Projects/loom-os", "%3", "pantheon")
    fake = FakeTmux(windows=[win])  # no clients attached at all
    monkeypatch.setattr(tmuxctl, "run", fake.run)
    events_mod.append_event(cfg.events_file, Event(
        ts=_iso(NOW - timedelta(minutes=5)), event="Notification", notification_type="permission_prompt",
        session_id="s1", cwd=win.path, tmux_pane="%3",
    ))
    r = make_runner(cfg, fake)
    r.tick()
    lines = sent_lines(cfg)
    assert len(lines) == 1
    assert lines[0]["dry_run"] is True and lines[0]["key"] == "s1:blocked-permission"
    assert lines[0]["ok"] is True

    r.tick()  # 10 more minutes of "nothing changes" must not add a second line
    assert len(sent_lines(cfg)) == 1


def test_present_suppresses_the_needs_you_notification(tmp_path, monkeypatch):
    cfg = cfg_for(tmp_path, notify={"enabled": True, "dry_run": True, "needs_you_after_seconds": 60,
                                    "present_seconds": 120})
    win = TmuxWindow(3, "loom-os", "node", "C:/Home/x/Documents/Projects/loom-os", "%3", "pantheon")
    # A client IS attached to the pantheon session, with activity 10s ago -- present.
    fake = FakeTmux(windows=[win], clients=[("/dev/pts/0", "pantheon", str(int((NOW - timedelta(seconds=10)).timestamp())))])
    monkeypatch.setattr(tmuxctl, "run", fake.run)
    events_mod.append_event(cfg.events_file, Event(
        ts=_iso(NOW - timedelta(minutes=5)), event="Notification", notification_type="permission_prompt",
        session_id="s1", cwd=win.path, tmux_pane="%3",
    ))
    make_runner(cfg, fake).tick()
    assert sent_lines(cfg) == []


# ---------------------------------------------------------------- acceptance 6: restart safety


def test_restart_safety_no_second_notification_after_a_fresh_runner_reloads_memory(tmp_path, monkeypatch):
    cfg = cfg_for(tmp_path, notify={"enabled": True, "dry_run": False, "toast": {"enabled": True}})
    win = TmuxWindow(3, "loom-os", "node", "C:/Home/x/Documents/Projects/loom-os", "%3", "pantheon")
    fake = FakeTmux(windows=[win])
    monkeypatch.setattr(tmuxctl, "run", fake.run)
    events_mod.append_event(cfg.events_file, Event(ts=_iso(NOW), event="failed", source="codex", session_id="job1",
                                                    cwd=win.path, job_id="job1", detail="exit code 1"))
    senders = FakeSenders()
    r1 = make_runner(cfg, fake, senders=senders)
    r1.tick()
    assert len(senders.calls) == 1  # toast fired once for the failed job
    assert len(sent_lines(cfg)) == 1

    # Simulate a restart: a brand new NotifyRunner with empty in-memory state, reloading only
    # what store.py persisted to state/notify/last.json.
    senders2 = FakeSenders()
    r2 = make_runner(cfg, fake, senders=senders2)
    r2.tick()
    assert senders2.calls == []  # the restarted runner must not re-fire
    assert len(sent_lines(cfg)) == 1  # still exactly one line total


def test_memory_file_holds_prev_status_and_history(tmp_path, monkeypatch):
    cfg = cfg_for(tmp_path, notify={"enabled": True, "dry_run": True})
    fake = FakeTmux()
    monkeypatch.setattr(tmuxctl, "run", fake.run)
    events_mod.append_event(cfg.events_file, Event(ts=_iso(NOW), event="SessionStart", session_id="s1", cwd="C:/x"))
    make_runner(cfg, fake).tick()
    prev_status, history = store_mod.read_memory(cfg)
    assert prev_status.get("s1") == "working"


# ---------------------------------------------------------------- channel gating + failures logged


def test_only_enabled_channels_are_actually_called(tmp_path, monkeypatch):
    cfg = cfg_for(tmp_path, notify={
        "enabled": True, "dry_run": False,
        "toast": {"enabled": False}, "ntfy": {"enabled": True, "url": "http://x", "topic": "p"},
        "telegram": {"enabled": False},
    })
    win = TmuxWindow(3, "loom-os", "node", "C:/x", "%3", "pantheon")
    fake = FakeTmux(windows=[win])
    monkeypatch.setattr(tmuxctl, "run", fake.run)
    events_mod.append_event(cfg.events_file, Event(ts=_iso(NOW), event="failed", source="codex",
                                                    session_id="job1", cwd="C:/x", job_id="job1"))
    senders = FakeSenders()
    make_runner(cfg, fake, senders=senders).tick()
    kinds = [c[0] for c in senders.calls]
    assert kinds == ["ntfy"]  # toast/telegram off in settings even though the rule wants all three


def test_a_failed_send_is_logged_and_never_raises(tmp_path, monkeypatch):
    cfg = cfg_for(tmp_path, notify={
        "enabled": True, "dry_run": False, "toast": {"enabled": False},
        "ntfy": {"enabled": True, "url": "http://x", "topic": "p"},
    })
    fake = FakeTmux()
    monkeypatch.setattr(tmuxctl, "run", fake.run)
    events_mod.append_event(cfg.events_file, Event(ts=_iso(NOW), event="failed", source="codex",
                                                    session_id="job1", cwd="C:/x", job_id="job1"))
    senders = FakeSenders(fail={"ntfy"})
    make_runner(cfg, fake, senders=senders).tick()  # must not raise
    lines = sent_lines(cfg)
    assert len(lines) == 1 and lines[0]["ok"] is False
    assert lines[0]["results"][0]["ok"] is False


# ---------------------------------------------------------------- quiet hours


def test_quiet_hours_suppresses_caution_notification(tmp_path, monkeypatch):
    cfg = cfg_for(tmp_path, notify={
        "enabled": True, "dry_run": False, "needs_you_after_seconds": 60,
        "quiet_hours": ["00:00", "23:59"],  # effectively always quiet for this test's local clock
        "toast": {"enabled": True},
    })
    win = TmuxWindow(3, "loom-os", "node", "C:/x", "%3", "pantheon")
    fake = FakeTmux(windows=[win])
    monkeypatch.setattr(tmuxctl, "run", fake.run)
    events_mod.append_event(cfg.events_file, Event(
        ts=_iso(NOW - timedelta(minutes=5)), event="Notification", notification_type="permission_prompt",
        session_id="s1", cwd=win.path, tmux_pane="%3",
    ))
    senders = FakeSenders()
    # `quiet_hours` spans the whole day, so any timezone `now.astimezone()` resolves to is still
    # "quiet" -- no need for a naive/local-clock trick here (that IS needed in
    # test_notify_rules.py, where the window is the realistic 23:00-08:00 default).
    make_runner(cfg, fake, senders=senders, now=NOW).tick()
    assert senders.calls == []  # Caution tier, quiet hours on: nothing actually sent
    lines = sent_lines(cfg)
    assert len(lines) == 1 and lines[0]["ok"] is False and lines[0].get("suppressed") == "quiet"


def test_quiet_hours_still_allows_warning_tier(tmp_path, monkeypatch):
    cfg = cfg_for(tmp_path, notify={
        "enabled": True, "dry_run": False,
        "quiet_hours": ["00:00", "23:59"],
        "toast": {"enabled": True},
    })
    fake = FakeTmux()
    monkeypatch.setattr(tmuxctl, "run", fake.run)
    events_mod.append_event(cfg.events_file, Event(ts=_iso(NOW), event="failed", source="codex",
                                                    session_id="job1", cwd="C:/x", job_id="job1"))
    senders = FakeSenders()
    make_runner(cfg, fake, senders=senders, now=NOW).tick()
    assert any(c[0] == "toast" for c in senders.calls)  # Warning tier still gets through


# ---------------------------------------------------------------- budget crossings wired end to end


def test_budget_crossing_produces_a_sent_line(tmp_path, monkeypatch):
    cfg = cfg_for(tmp_path, notify={"enabled": True, "dry_run": True})
    fake = FakeTmux()
    monkeypatch.setattr(tmuxctl, "run", fake.run)
    write_hud(cfg, {"five_hour_pct": 92.0, "five_hour_resets_at": _iso(NOW + timedelta(hours=2))})
    make_runner(cfg, fake).tick()
    lines = sent_lines(cfg)
    assert any(l["key"].startswith("budget:claude:five_hour:90:") for l in lines)


# ---------------------------------------------------------------- park_refused / handoff wired end to end


def test_park_refused_line_in_parked_jsonl_produces_a_notification(tmp_path, monkeypatch):
    cfg = cfg_for(tmp_path, notify={"enabled": True, "dry_run": True})
    fake = FakeTmux()
    monkeypatch.setattr(tmuxctl, "run", fake.run)
    governor_parked.append_park_refused(cfg, "s9", parked_count=6, max_parked=6, at=_iso(NOW))
    make_runner(cfg, fake).tick()
    lines = sent_lines(cfg)
    assert any(l["title"] == "Parking refused" for l in lines)


def test_handoff_event_produces_a_notification(tmp_path, monkeypatch):
    cfg = cfg_for(tmp_path, notify={"enabled": True, "dry_run": True})
    fake = FakeTmux()
    monkeypatch.setattr(tmuxctl, "run", fake.run)
    events_mod.append_event(cfg.events_file, Event(
        ts=_iso(NOW), event="handoff", source="pantheon", session_id="s1", project="loom-os",
        extra={"to_provider": "codex", "from_provider": "claude", "tracker_id": "row-1"},
    ))
    make_runner(cfg, fake).tick()
    lines = sent_lines(cfg)
    assert any(l["title"] == "Handed to codex" for l in lines)
