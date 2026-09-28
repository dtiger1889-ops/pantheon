"""Build A: `H` on a live Claude row
asks it to `/checkpoint`, waits for that to land, then opens the New-session steps for the same
folder with the last launch's choices highlighted. Pure pieces first, then the deck walk.

Named `test_handoff_here.py`, not the spec's `test_restart.py`: that file already holds 
deck restart (`R`). No test reaches a real tmux server or a real PowerShell: tmux calls go to
`FakeTmuxRun`, the necromancy digest to a fake `_run_pwsh`.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
from datetime import datetime, timedelta, timezone

import pytest
from textual.widgets import TextArea

from pantheon import session_ctl, tmuxctl
from pantheon.dispatch import restart as restart_mod
from pantheon.governor import handoff as handoff_mod
from pantheon.models import Event, LaunchResult, utcnow_iso
from pantheon.supervisor import pane as pane_mod
from pantheon.supervisor.app import SupervisorApp
from pantheon.supervisor.pane import SupervisorPane, toolbar_actions_for
from pantheon.widgets.modal import Confirm, Pick, TextPrompt
from tests.test_handoff import NECROMANCY_SAMPLE
from tests.test_supervisor_app import CWD, PANTHEON_WINDOW, FakeTmuxRun, make_config

OTHER = "C:/Home/x/Documents/Projects/loom-os"
SID = "abc12345deadbeef"


def _ev(event: str, cwd: str, ts: str, **extra) -> Event:
    return Event(ts=ts, event=event, source="pantheon", cwd=cwd, extra=extra)


# ------------------------------------------------------------------ last_options_for (acceptance 1)

def test_last_options_for_picks_the_newest_launch_in_that_folder_only():
    events = [
        _ev("new_session", CWD, "2026-09-26T10:00:00Z", provider="claude",
            launch_options={"model": "sonnet", "effort": "high"}),
        _ev("new_session", CWD.replace("/", "\\"), "2026-09-26T11:00:00Z", provider="claude",
            launch_options={"model": "fable", "effort": "low", "mode": "plan", "resume": "old-id"}),
        # newer, but another project's
        _ev("new_session", OTHER, "2026-09-26T12:00:00Z", provider="claude",
            launch_options={"model": "haiku", "effort": "max"}),
        # newer, same folder, but Codex -- its model name means nothing on Claude's picker
        _ev("new_session", CWD, "2026-09-26T13:00:00Z", provider="codex",
            launch_options={"model": "gpt-5"}),
        # newer, same folder, but it never started
        _ev("new_session_failed", CWD, "2026-09-26T14:00:00Z", provider="claude",
            launch_options={"model": "opus"}),
    ]
    assert restart_mod.last_options_for(CWD, events) == {"model": "fable", "effort": "low", "mode": "plan"}
    assert restart_mod.last_options_for(OTHER, events) == {"model": "haiku", "effort": "max"}
    assert restart_mod.last_options_for("C:/nowhere", events) == {}
    assert restart_mod.last_options_for(None, events) == {}


def test_last_options_for_a_launch_on_defaults_carries_nothing():
    events = [
        _ev("new_session", CWD, "2026-09-26T10:00:00Z", provider="claude",
            launch_options={"model": "sonnet"}),
        _ev("new_session", CWD, "2026-09-26T11:00:00Z", provider="claude"),
    ]
    assert restart_mod.last_options_for(CWD, events) == {}


# ------------------------------------------------------------------ checkpoint_settled

SINCE = datetime(2026, 9, 26, 12, 0, 0, 500000, tzinfo=timezone.utc)


def _claude(event: str, ts: str, sid: str = SID) -> Event:
    return Event(ts=ts, event=event, source="claude", session_id=sid, cwd=CWD)


def test_settled_on_session_end_after_the_typing():
    events = [_claude("SessionEnd", "2026-09-26T11:59:00Z"),            # an older one: ignored
              _claude("SessionEnd", "2026-09-26T12:00:00Z", sid="other")]
    assert restart_mod.checkpoint_settled(SID, SINCE, events) is None
    events.append(_claude("SessionEnd", "2026-09-26T12:00:00Z"))           # same second counts
    assert restart_mod.checkpoint_settled(SID, SINCE, events) == "ended"


def test_settled_on_a_rewritten_checkpoint_then_a_finished_turn():
    saved = datetime(2026, 9, 26, 12, 1, 0, tzinfo=timezone.utc)
    # The turn it was already on finishes BEFORE the checkpoint is written: not yet.
    events = [_claude("Stop", "2026-09-26T12:00:30Z")]
    assert restart_mod.checkpoint_settled(SID, SINCE, events, saved) is None
    # A Stop after the rewrite: the checkpoint turn is done.
    events.append(_claude("Stop", "2026-09-26T12:01:10Z"))
    assert restart_mod.checkpoint_settled(SID, SINCE, events, saved) == "saved"
    # A CHECKPOINT.md older than the typing never counts, however many turns end after it.
    old = datetime(2026, 9, 26, 11, 0, 0, tzinfo=timezone.utc)
    assert restart_mod.checkpoint_settled(SID, SINCE, events, old) is None


# ------------------------------------------------------------------ session_ctl.request_checkpoint

def test_request_checkpoint_types_it_and_presses_enter_as_two_calls(monkeypatch):
    fake = FakeTmuxRun()
    monkeypatch.setattr(tmuxctl, "run", fake)
    assert session_ctl.request_checkpoint("pantheon:3", sleep=lambda s: None) is None
    assert fake.sends() == [("send-keys", "-t", "pantheon:3", "-l", "/checkpoint"),
                            ("send-keys", "-t", "pantheon:3", "Enter")]


def test_request_checkpoint_refuses_a_window_that_is_not_claude(monkeypatch):
    fake = FakeTmuxRun(pane_command="bash")
    monkeypatch.setattr(tmuxctl, "run", fake)
    assert session_ctl.request_checkpoint("pantheon:3", sleep=lambda s: None) == session_ctl.REFUSED_NOT_CLAUDE
    assert fake.sends() == []


# ------------------------------------------------------------------ the deck walk (acceptance 2-4)

def working_fixture() -> list[dict]:
    """Stamped when each test builds its config, so a slow suite never ages the row into
    `quiet` before the test presses `H`."""
    return [
    {"ts": utcnow_iso(), "source": "claude", "event": "SessionStart",
     "session_id": SID, "cwd": CWD, "tmux_pane": "%3"},
    {"ts": utcnow_iso(), "source": "claude", "event": "PostToolUse", "tool_name": "Edit",
     "session_id": SID, "cwd": CWD, "tmux_pane": "%3"},
    {"ts": "2026-09-20T09:00:00Z", "source": "pantheon", "event": "new_session", "cwd": CWD,
     "project": "hiking_log_v2", "provider": "claude",
     "launch_options": {"model": "sonnet", "effort": "high", "mode": "acceptEdits"}},
    ]


class FakeProvider:
    name = "claude"

    def capabilities(self):
        return {"interactive_tmux"}

    def launch(self, project_dir, briefing_path, interactive, options=None):
        return LaunchResult(True, "tmux", 5, "x", message="open in window 5")


@pytest.fixture
def fake_tmux(monkeypatch):
    fake = FakeTmuxRun()
    monkeypatch.setattr(tmuxctl, "run", fake)
    monkeypatch.setattr(pane_mod, "HANDOFF_POLL_SECONDS", 0.05)
    return fake


def _app(tmp_path):
    cfg = make_config(tmp_path, working_fixture())
    return cfg, SupervisorApp(cfg=cfg, window_source=lambda: [PANTHEON_WINDOW])


async def _press_h_and_confirm(app, pilot) -> SupervisorPane:
    await pilot.pause()
    sup = app.query_one(SupervisorPane)
    sup.providers = {"claude": FakeProvider()}
    row = sup.selected()
    assert row is not None and row.status.value == "working"
    assert any(a.key == "H" and a.enabled for a in toolbar_actions_for(row, {"claude"}))
    await pilot.press("H")
    await pilot.pause()
    assert isinstance(app.screen, Confirm)
    await pilot.press("y")
    await pilot.pause()
    return sup


async def _wait_for(pilot, predicate, tries=60):
    for _ in range(tries):
        if predicate():
            return True
        await pilot.pause(0.05)
    return predicate()


def _end_the_old_session(cfg):
    with open(cfg.events_file, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": (datetime.now(timezone.utc) + timedelta(seconds=2)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"), "source": "claude", "event": "SessionEnd",
            "session_id": SID, "cwd": CWD, "detail": "prompt_input_exit"}) + "\n")


def test_h_types_one_checkpoint_and_never_kills(tmp_path, fake_tmux):
    """Acceptance 2."""
    cfg, app = _app(tmp_path)

    async def drive():
        async with app.run_test(size=(120, 40)) as pilot:
            sup = await _press_h_and_confirm(app, pilot)
            status = str(sup.query_one("#status").content)
            await pilot.press("H")           # a second press while waiting types nothing more
            await pilot.pause()
            return status, str(sup.query_one("#status").content)

    status, again = asyncio.run(drive())
    assert fake_tmux.sends() == [("send-keys", "-t", "pantheon:3", "-l", "/checkpoint"),
                                 ("send-keys", "-t", "pantheon:3", "Enter")]
    assert not [c for c in fake_tmux.calls if c and c[0] == "kill-window"]
    assert "asked hiking_log_v2 to /checkpoint" in status
    assert "already waiting for hiking_log_v2" in again


def test_session_end_opens_the_chain_on_model_with_the_last_choices(tmp_path, fake_tmux):
    """Acceptance 3: after the old session's `SessionEnd`, the model screen opens with the
    folder's last launch highlighted; effort too; Escape walks back to "who works here"."""
    cfg, app = _app(tmp_path)
    seen = {}

    async def drive():
        async with app.run_test(size=(120, 40)) as pilot:
            await _press_h_and_confirm(app, pilot)
            assert not isinstance(app.screen, Pick)     # still waiting: nothing opened early
            _end_the_old_session(cfg)
            assert await _wait_for(pilot, lambda: isinstance(app.screen, Pick))
            seen["model_title"], seen["model_current"] = app.screen.title_text, app.screen.current
            await pilot.press("enter")                   # the family screen (model_picker.py)
            await pilot.pause()
            seen["version_current"] = app.screen.current
            await pilot.press("enter")                   # then the version
            await pilot.pause()
            seen["effort_title"], seen["effort_current"] = app.screen.title_text, app.screen.current
            await pilot.press("escape")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            seen["back_to"] = app.screen.title_text

    asyncio.run(drive())
    assert seen["model_title"].startswith("model for hiking_log_v2")
    assert seen["model_current"] == "family:Sonnet"
    assert "sonnet" in str(seen["version_current"]).lower()
    assert seen["effort_title"].startswith("effort for hiking_log_v2")
    assert seen["effort_current"] == "high"
    assert seen["back_to"].startswith("who works in hiking_log_v2")
    assert not [c for c in fake_tmux.calls if c and c[0] == "kill-window"]


def test_carrying_a_digest_fills_the_message_box(tmp_path, fake_tmux, monkeypatch):
    """Acceptance 4: `d` on the digest question puts the old session's digest -- its FIRST ASK
    line included -- into the first-message box; Escape from the box goes back to the question."""
    monkeypatch.setattr(handoff_mod, "_run_pwsh",
                        lambda argv: subprocess.CompletedProcess(argv, 0, NECROMANCY_SAMPLE, ""))
    cfg, app = _app(tmp_path)
    seen = {}

    async def drive():
        async with app.run_test(size=(120, 40)) as pilot:
            await _press_h_and_confirm(app, pilot)
            _end_the_old_session(cfg)
            assert await _wait_for(pilot, lambda: isinstance(app.screen, Pick))
            for _ in range(4):     # model family, model version, effort, mode: keep what's lit
                await pilot.press("enter")
                await pilot.pause()
            seen["question"] = app.screen.title_text
            await pilot.press("d")
            assert await _wait_for(pilot, lambda: isinstance(app.screen, TextPrompt))
            seen["box"] = app.screen.query_one("#prompt-text", TextArea).text
            await pilot.press("escape")
            await pilot.pause()
            seen["after_escape"] = app.screen.title_text

    asyncio.run(drive())
    assert seen["question"].startswith("first message for hiking_log_v2")
    assert seen["box"].startswith("Continuing from the previous session in this folder.")
    assert "== FIRST ASK ==" in seen["box"]
    assert "fix the Homebase task card overflow on narrow screens" in seen["box"]
    assert "== PRS ==" not in seen["box"]              # the digest's own filter still applies
    assert seen["after_escape"].startswith("first message for hiking_log_v2")


def test_no_checkpoint_in_time_says_so_and_leaves_the_window(tmp_path, fake_tmux):
    """Must-not 2: on the grace timeout the deck says the window is still open and stops."""
    import dataclasses

    cfg, _ = _app(tmp_path)
    cfg = dataclasses.replace(cfg, governor={"grace_seconds": 1})
    app = SupervisorApp(cfg=cfg, window_source=lambda: [PANTHEON_WINDOW])

    async def drive():
        async with app.run_test(size=(120, 40)) as pilot:
            sup = await _press_h_and_confirm(app, pilot)
            await _wait_for(pilot, lambda: "no checkpoint seen" in str(sup.query_one("#status").content),
                            tries=80)
            return str(sup.query_one("#status").content), app.screen

    status, screen = asyncio.run(drive())
    assert "no checkpoint seen from hiking_log_v2 in time; window 3 is still open" in status
    assert "nothing was closed" in status
    assert not isinstance(screen, Pick)
    assert not [c for c in fake_tmux.calls if c and c[0] == "kill-window"]
