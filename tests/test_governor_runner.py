"""The governor's 60-second tick, against a fake tmux and a fixed
clock -- no real tmux server, no real subprocess, no real waiting.
"""
from __future__ import annotations

import json

import pytest

from pantheon import config as config_mod
from pantheon import events as events_mod
from pantheon import tmuxctl
from pantheon.governor import parked as parked_mod
from pantheon.governor import policy, runner as runner_mod
from pantheon.models import Event, TmuxWindow, utcnow_iso


def _iso(dt) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


from datetime import datetime, timedelta, timezone

NOW = datetime(2026, 9, 2, 20, 0, 0, tzinfo=timezone.utc)


def cfg_for(tmp_path, governor=None, **kw):
    kw.setdefault("state_dir", str(tmp_path / "state"))
    kw.setdefault("tmux_session", "pantheon")
    kw["governor"] = governor or {}
    return config_mod.Config(**kw)


def write_hud(cfg, claude: dict, fetched_at=None) -> None:
    cfg.hud_file.parent.mkdir(parents=True, exist_ok=True)
    picture = {"fetched_at": fetched_at or _iso(NOW), "claude": claude, "codex": {}, "history": {}}
    cfg.hud_file.write_text(json.dumps(picture), encoding="utf-8")


class FakeTmux:
    """Records every tmux call; plays back a fixed window list and a scripted pane command
    sequence for `wait_for_command` polls (same shape as `test_providers.FakeTmux`)."""

    def __init__(self, windows=None, commands=None):
        self.calls: list[tuple] = []
        self.windows = list(windows or [])
        self.commands = list(commands or [])
        self._next_index = 10

    def run(self, *args, tmux=None, check=False):
        import subprocess

        self.calls.append(args)
        out = ""
        if args[0] == "list-windows":
            lines = [f"{w.index}|{w.name}|{w.command}|{w.path}|{w.pane_id}|{w.session}" for w in self.windows]
            out = "\n".join(lines) + ("\n" if lines else "")
        elif args[0] == "new-window":
            out = f"{self._next_index}\n"
            self._next_index += 1
        elif args[0] == "display-message":
            fmt = args[-1]
            if "pane_current_command" in fmt:
                out = (self.commands.pop(0) if len(self.commands) > 1 else (self.commands[0] if self.commands else "bash")) + "\n"
            elif "pane_id" in fmt:
                out = "%20\n"
        return subprocess.CompletedProcess(args, 0, out, "")


def typed_text(fake: FakeTmux) -> list[str]:
    return [c[4] for c in fake.calls if c[0] == "send-keys" and "-l" in c]


@pytest.fixture(autouse=True)
def _safe_hud_collect(monkeypatch):
    """A test with no `hud.json` at all (most of the parking/resume tests -- they only care
    about `state/limits/`) must never shell out to real ccusage/npx just because the picture
    looks stale. Any test that wants to exercise the real fallback path re-patches this itself."""
    monkeypatch.setattr(runner_mod.hud_sources, "collect",
                        lambda cfg, write=True: {"fetched_at": _iso(NOW), "claude": {}, "codex": {}})


# ---------------------------------------------------------------- acceptance 1: dry run


def test_dry_run_writes_would_lines_and_types_nothing(tmp_path, monkeypatch):
    cfg = cfg_for(tmp_path, governor={
        "enabled": True, "dry_run": True, "wind_down_at_percent": {"five_hour": 1},
    })
    win = TmuxWindow(3, "loom-os", "node", "C:/Home/x/Documents/Projects/loom-os", "%3", "pantheon")
    fake = FakeTmux(windows=[win])
    monkeypatch.setattr(tmuxctl, "run", fake.run)
    events_mod.append_event(cfg.events_file, Event(
        ts=_iso(NOW - timedelta(minutes=5)), event="SessionStart", session_id="s1", cwd=win.path, tmux_pane="%3",
    ))
    write_hud(cfg, {"provider": "claude", "five_hour_pct": 5.0, "five_hour_resets_at": _iso(NOW + timedelta(hours=2))})

    runner = runner_mod.GovernorRunner(cfg=cfg, sleep=lambda s: None, now_fn=lambda: NOW)
    runner.tick()

    dryrun_lines = parked_mod._read_lines(parked_mod.dryrun_path(cfg))
    assert len(dryrun_lines) >= 1
    assert all(l["event"].startswith("would_") for l in dryrun_lines)
    assert any(l["event"] == "would_wind_down" and l["session_id"] == "s1" for l in dryrun_lines)

    events, _errors = events_mod.read_events(cfg.events_file)
    assert not any(e.event == "wind_down" for e in events)  # events.jsonl untouched by dry run
    assert typed_text(fake) == []  # nothing was typed into any session
    assert parked_mod.read_pending(cfg) == {}  # dry run never sets a real grace clock either


def test_dry_run_does_nothing_below_threshold(tmp_path, monkeypatch):
    cfg = cfg_for(tmp_path, governor={"enabled": True, "dry_run": True, "wind_down_at_percent": {"five_hour": 99}})
    fake = FakeTmux()
    monkeypatch.setattr(tmuxctl, "run", fake.run)
    write_hud(cfg, {"provider": "claude", "five_hour_pct": 5.0})
    runner_mod.GovernorRunner(cfg=cfg, sleep=lambda s: None, now_fn=lambda: NOW).tick()
    assert parked_mod._read_lines(parked_mod.dryrun_path(cfg)) == []


def test_disabled_governor_does_nothing_at_all(tmp_path, monkeypatch):
    cfg = cfg_for(tmp_path, governor={"enabled": False, "dry_run": False, "wind_down_at_percent": {"five_hour": 1}})
    fake = FakeTmux()
    monkeypatch.setattr(tmuxctl, "run", fake.run)
    write_hud(cfg, {"provider": "claude", "five_hour_pct": 99.0})
    runner_mod.GovernorRunner(cfg=cfg, sleep=lambda s: None, now_fn=lambda: NOW).tick()
    assert fake.calls == []
    events, _ = events_mod.read_events(cfg.events_file)
    assert events == []


# ---------------------------------------------------------------- live wind-down


def test_live_wind_down_types_the_sentence_and_starts_the_grace_clock(tmp_path, monkeypatch):
    cfg = cfg_for(tmp_path, governor={
        "enabled": True, "dry_run": False, "grace_seconds": 300,
        "wind_down_at_percent": {"five_hour": 85},
    })
    win = TmuxWindow(3, "loom-os", "node", "C:/Home/x/Documents/Projects/loom-os", "%3", "pantheon")
    # `commands=["node"]`: the pane is running Claude Code, so the guard lets the text through.
    fake = FakeTmux(windows=[win], commands=["node"])
    monkeypatch.setattr(tmuxctl, "run", fake.run)
    events_mod.append_event(cfg.events_file, Event(
        ts=_iso(NOW - timedelta(minutes=5)), event="SessionStart", session_id="s1", cwd=win.path, tmux_pane="%3",
    ))
    write_hud(cfg, {"provider": "claude", "five_hour_pct": 86.0, "five_hour_resets_at": _iso(NOW + timedelta(hours=2))})

    runner_mod.GovernorRunner(cfg=cfg, sleep=lambda s: None, now_fn=lambda: NOW).tick()

    typed = typed_text(fake)
    assert len(typed) == 1 and "86%" in typed[0] and "/checkpoint" in typed[0]
    events, _ = events_mod.read_events(cfg.events_file)
    wind = [e for e in events if e.event == "wind_down"]
    assert len(wind) == 1 and wind[0].session_id == "s1"
    pending = parked_mod.read_pending(cfg)
    assert "s1" in pending and pending["s1"]["cwd"] == win.path


def test_live_wind_down_types_the_twelve_hour_clock_when_that_is_the_setting(tmp_path, monkeypatch):
    cfg = cfg_for(
        tmp_path, governor={
            "enabled": True, "dry_run": False, "grace_seconds": 300,
            "wind_down_at_percent": {"five_hour": 85},
        }, appearance={"clock": "12h"},
    )
    win = TmuxWindow(3, "loom-os", "node", "C:/Home/x/Documents/Projects/loom-os", "%3", "pantheon")
    fake = FakeTmux(windows=[win], commands=["node"])
    monkeypatch.setattr(tmuxctl, "run", fake.run)
    events_mod.append_event(cfg.events_file, Event(
        ts=_iso(NOW - timedelta(minutes=5)), event="SessionStart", session_id="s1", cwd=win.path, tmux_pane="%3",
    ))
    write_hud(cfg, {"provider": "claude", "five_hour_pct": 86.0, "five_hour_resets_at": _iso(NOW + timedelta(hours=2))})

    runner_mod.GovernorRunner(cfg=cfg, sleep=lambda s: None, now_fn=lambda: NOW).tick()

    typed = typed_text(fake)
    assert len(typed) == 1
    assert "am" in typed[0] or "pm" in typed[0]


def test_live_wind_down_never_types_into_a_pane_that_is_not_claude(tmp_path, monkeypatch):
    """The row still carries window 3, but a shell took the pane over after the agent exited:
    the sentence must not be typed into bash as a stray command."""
    cfg = cfg_for(tmp_path, governor={
        "enabled": True, "dry_run": False, "grace_seconds": 300,
        "wind_down_at_percent": {"five_hour": 85},
    })
    win = TmuxWindow(3, "loom-os", "node", "C:/Home/x/Documents/Projects/loom-os", "%3", "pantheon")
    fake = FakeTmux(windows=[win], commands=["bash"])
    monkeypatch.setattr(tmuxctl, "run", fake.run)
    events_mod.append_event(cfg.events_file, Event(
        ts=_iso(NOW - timedelta(minutes=5)), event="SessionStart", session_id="s1", cwd=win.path, tmux_pane="%3",
    ))
    write_hud(cfg, {"provider": "claude", "five_hour_pct": 86.0, "five_hour_resets_at": _iso(NOW + timedelta(hours=2))})

    runner_mod.GovernorRunner(cfg=cfg, sleep=lambda s: None, now_fn=lambda: NOW).tick()

    assert typed_text(fake) == []


def test_wind_down_does_not_repeat_for_the_same_reset(tmp_path, monkeypatch):
    cfg = cfg_for(tmp_path, governor={"enabled": True, "dry_run": False, "wind_down_at_percent": {"five_hour": 85}})
    win = TmuxWindow(3, "loom-os", "node", "C:/x", "%3", "pantheon")
    fake = FakeTmux(windows=[win])
    monkeypatch.setattr(tmuxctl, "run", fake.run)
    events_mod.append_event(cfg.events_file, Event(ts=_iso(NOW - timedelta(minutes=5)), event="SessionStart",
                                                   session_id="s1", cwd="C:/x", tmux_pane="%3"))
    write_hud(cfg, {"provider": "claude", "five_hour_pct": 86.0, "five_hour_resets_at": _iso(NOW + timedelta(hours=2))})
    r = runner_mod.GovernorRunner(cfg=cfg, sleep=lambda s: None, now_fn=lambda: NOW)
    r.tick()
    r.tick()
    events, _ = events_mod.read_events(cfg.events_file)
    assert len([e for e in events if e.event == "wind_down"]) == 1


def test_stale_hud_falls_back_to_the_sources_directly(tmp_path, monkeypatch):
    cfg = cfg_for(tmp_path, governor={"enabled": True, "dry_run": True, "wind_down_at_percent": {"five_hour": 1}})
    write_hud(cfg, {"provider": "claude", "five_hour_pct": 1.0}, fetched_at=_iso(NOW - timedelta(minutes=30)))
    monkeypatch.setattr(tmuxctl, "run", FakeTmux().run)
    called = {}

    def fake_collect(cfg_arg, write=True):
        called["yes"] = True
        return {"fetched_at": _iso(NOW), "claude": {"provider": "claude", "five_hour_pct": 90.0,
                                                     "five_hour_resets_at": _iso(NOW + timedelta(hours=1))}}

    monkeypatch.setattr(runner_mod.hud_sources, "collect", fake_collect)
    runner_mod.GovernorRunner(cfg=cfg, sleep=lambda s: None, now_fn=lambda: NOW).tick()
    assert called.get("yes") is True


# ---------------------------------------------------------------- parking


def test_parks_on_session_end_and_never_twice(tmp_path, monkeypatch):
    cfg = cfg_for(tmp_path, governor={"enabled": True, "dry_run": False})
    monkeypatch.setattr(tmuxctl, "run", FakeTmux().run)
    parked_mod.set_pending(cfg, "s1", {"cwd": "C:/x", "project": "loom-os", "window_name": "loom-os",
                                       "parked_at_deadline": _iso(NOW + timedelta(minutes=5)),
                                       "resume_after": _iso(NOW + timedelta(hours=1))})
    events_mod.append_event(cfg.events_file, Event(ts=_iso(NOW), event="SessionEnd", session_id="s1", cwd="C:/x"))
    r = runner_mod.GovernorRunner(cfg=cfg, sleep=lambda s: None, now_fn=lambda: NOW)
    r.tick()
    parks = parked_mod.open_parks(cfg)
    assert "s1" in parks and parks["s1"]["reason"] == "checkpointed"
    assert parked_mod.read_pending(cfg) == {}
    events, _ = events_mod.read_events(cfg.events_file)
    assert len([e for e in events if e.event == "park"]) == 1
    r.tick()  # a second tick must not park it again
    events, _ = events_mod.read_events(cfg.events_file)
    assert len([e for e in events if e.event == "park"]) == 1


def test_park_refused_past_max_parked(tmp_path, monkeypatch):
    cfg = cfg_for(tmp_path, governor={"enabled": True, "dry_run": False, "max_parked": 1})
    monkeypatch.setattr(tmuxctl, "run", FakeTmux().run)
    parked_mod.append_park(cfg, parked_mod.Parked(session_id="already", parked_at="t0", reason="checkpointed"))
    parked_mod.set_pending(cfg, "s2", {"cwd": "C:/y", "parked_at_deadline": _iso(NOW - timedelta(seconds=1))})
    events_mod.append_event(cfg.events_file, Event(ts=_iso(NOW), event="SessionEnd", session_id="s2", cwd="C:/y"))
    runner_mod.GovernorRunner(cfg=cfg, sleep=lambda s: None, now_fn=lambda: NOW).tick()
    assert "s2" not in parked_mod.open_parks(cfg)
    lines = parked_mod._read_lines(parked_mod.parked_path(cfg))
    assert any(l["kind"] == "park_refused" and l["session_id"] == "s2" for l in lines)


# ---------------------------------------------------------------- resume


def test_resume_opens_a_window_and_types_the_prompt_once_the_session_is_ready(tmp_path, monkeypatch):
    cfg = cfg_for(tmp_path, governor={"enabled": True, "dry_run": False, "auto_resume": True})
    fake = FakeTmux(commands=["bash", "node"])
    monkeypatch.setattr(tmuxctl, "run", fake.run)
    parked_mod.append_park(cfg, parked_mod.Parked(
        session_id="s1", project="loom-os", cwd="C:/Home/x/Documents/Projects/loom-os",
        window_name="loom-os", parked_at=_iso(NOW - timedelta(hours=6)), reason="checkpointed",
        resume_after=_iso(NOW - timedelta(minutes=1)),
    ))
    runner_mod.GovernorRunner(cfg=cfg, sleep=lambda s: None, now_fn=lambda: NOW).tick()
    new_window = next(c for c in fake.calls if c[0] == "new-window")
    assert "loom-os" in new_window
    typed = typed_text(fake)
    assert any("resume" in cmd.lower() or "--resume" in cmd for cmd in typed) or any("s1" in cmd for cmd in typed)
    assert any("Limit reset" in cmd for cmd in typed)
    assert "s1" not in parked_mod.open_parks(cfg)


def test_resume_not_attempted_before_resume_after(tmp_path, monkeypatch):
    cfg = cfg_for(tmp_path, governor={"enabled": True, "dry_run": False, "auto_resume": True})
    fake = FakeTmux()
    monkeypatch.setattr(tmuxctl, "run", fake.run)
    parked_mod.append_park(cfg, parked_mod.Parked(
        session_id="s1", cwd="C:/x", parked_at=_iso(NOW), reason="checkpointed",
        resume_after=_iso(NOW + timedelta(hours=1)),
    ))
    runner_mod.GovernorRunner(cfg=cfg, sleep=lambda s: None, now_fn=lambda: NOW).tick()
    assert not any(c[0] == "new-window" for c in fake.calls)  # the tick still lists windows; it opens none
    assert "s1" in parked_mod.open_parks(cfg)


def test_resume_skipped_when_the_user_already_did_it_by_hand(tmp_path, monkeypatch):
    cfg = cfg_for(tmp_path, governor={"enabled": True, "dry_run": False, "auto_resume": True})
    win = TmuxWindow(5, "loom-os", "node", "C:/Home/x/Documents/Projects/loom-os", "%5", "pantheon")
    fake = FakeTmux(windows=[win])
    monkeypatch.setattr(tmuxctl, "run", fake.run)
    parked_mod.append_park(cfg, parked_mod.Parked(
        session_id="s1", cwd="C:/Home/x/Documents/Projects/loom-os", parked_at=_iso(NOW - timedelta(hours=1)),
        reason="checkpointed", resume_after=_iso(NOW - timedelta(minutes=1)),
    ))
    runner_mod.GovernorRunner(cfg=cfg, sleep=lambda s: None, now_fn=lambda: NOW).tick()
    assert not any(c[0] == "new-window" for c in fake.calls)
    lines = parked_mod._read_lines(parked_mod.parked_path(cfg))
    assert lines[-1]["kind"] == "resumed" and lines[-1]["by"] == "the user"


def test_auto_resume_off_leaves_parked_sessions_alone(tmp_path, monkeypatch):
    cfg = cfg_for(tmp_path, governor={"enabled": True, "dry_run": False, "auto_resume": False})
    fake = FakeTmux()
    monkeypatch.setattr(tmuxctl, "run", fake.run)
    parked_mod.append_park(cfg, parked_mod.Parked(
        session_id="s1", cwd="C:/x", parked_at=_iso(NOW), reason="checkpointed",
        resume_after=_iso(NOW - timedelta(minutes=1)),
    ))
    runner_mod.GovernorRunner(cfg=cfg, sleep=lambda s: None, now_fn=lambda: NOW).tick()
    assert not any(c[0] == "new-window" for c in fake.calls)
    assert "s1" in parked_mod.open_parks(cfg)


def test_resume_ladder_climbs_a_rung_on_a_failed_attempt_and_marks_resume_failed_at_four(tmp_path, monkeypatch):
    cfg = cfg_for(tmp_path, governor={"enabled": True, "dry_run": False, "auto_resume": True})
    # `wait_for_command` never sees a live agent -- every attempt "fails" to start.
    fake = FakeTmux(commands=["bash"])
    monkeypatch.setattr(tmuxctl, "run", fake.run)
    monkeypatch.setattr(tmuxctl, "wait_for_command", lambda *a, **k: False)
    parked_mod.append_park(cfg, parked_mod.Parked(
        session_id="s1", cwd="C:/x", parked_at=_iso(NOW), reason="checkpointed",
        resume_after=_iso(NOW - timedelta(minutes=1)),
    ))
    r = runner_mod.GovernorRunner(cfg=cfg, sleep=lambda s: None, now_fn=lambda: NOW)
    r.tick()
    assert r._rungs["s1"] == 1
    r.tick()
    assert r._rungs["s1"] == 2
    r.tick()
    assert r._rungs["s1"] == 3
    r.tick()  # rung 4: give up, no new window, needs the user
    assert r._rungs["s1"] == 4
    events, _ = events_mod.read_events(cfg.events_file)
    last = [e for e in events if e.event == "resume"][-1]
    assert last.extra.get("rung") == 4
    assert "s1" in parked_mod.open_parks(cfg)  # still parked -- the governor does not retry past rung 4


# ---------------------------------------------------------------- wind_down_text wording


def test_wind_down_text_names_the_window_percent_and_reset_clock():
    text = runner_mod.wind_down_text("five_hour", 86.0, _iso(NOW + timedelta(hours=2)))
    assert "86%" in text and "five-hour" in text and "/checkpoint" in text and "Do not start new work" in text
    weekly = runner_mod.wind_down_text("seven_day", 91.0, None)
    assert "weekly" in weekly and "91%" in weekly


def test_wind_down_text_honours_the_twelve_hour_clock_setting():
    resets = _iso(NOW + timedelta(hours=2))
    text_24h = runner_mod.wind_down_text("five_hour", 86.0, resets)
    text_12h = runner_mod.wind_down_text("five_hour", 86.0, resets, clock="12h")
    assert text_24h != text_12h
    assert "am" in text_12h or "pm" in text_12h
