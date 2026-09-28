"""A Claude window stuck on a startup question shows up on the supervisor, toasts once, and the
folder-trust one can be answered from the deck with Enter then `y`. No real tmux: `tmuxctl.run` is
replaced by a fake pane that shows the real captured trust screen and reacts to keys."""
from __future__ import annotations

import asyncio
import dataclasses
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from textual.widgets import DataTable, Static

from pantheon import config as config_mod
from pantheon import dialogs, tmuxctl
from pantheon.models import AgentState, AgentStatus, TmuxWindow
from pantheon.notify import channels, startup_dialogs
from pantheon.supervisor import state as state_mod
from pantheon.supervisor.app import SupervisorApp

FIXTURES = Path(__file__).parent / "fixtures" / "dialogs"
TRUST = (FIXTURES / "trust_folder_2.1.283.txt").read_text(encoding="utf-8")
NORMAL = ("─" * 60 + "\n> \n" + "─" * 60 + "\n  5h 68% · wk 45% · ctx ? · $0.00 · Opus 5.5\n")
NOW = datetime(2026, 9, 26, 22, 0, tzinfo=timezone.utc)


def _row(sid, age=None, window=4, provider="claude", session="pantheon", pane="%9"):
    ts = None if age is None else (NOW - timedelta(seconds=age)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return AgentState(session_id=sid, provider=provider, last_event_ts=ts, window_index=window,
                      tmux_session=session, tmux_pane=pane, project="loom-os")


# --------------------------------------------------------------------------- pure state rules


def test_only_live_claude_windows_without_a_fresh_hook_event_are_read():
    rows = [_row("pane:%9"), _row("fresh", age=5), _row("stale", age=300),
            _row("desk", window=None), _row("cdx", provider="codex")]
    picked = [r.session_id for r in state_mod.dialog_candidates(rows, NOW)]
    assert picked == ["pane:%9", "stale"]


def test_a_dialog_turns_the_row_into_needs_you_with_the_reason_on_top():
    rows = [_row("a", age=300), _row("pane:%9")]
    rows[0].status = AgentStatus.WORKING
    out = state_mod.apply_dialogs(rows, {"pane:%9": "asking whether to trust loom-os"})
    assert out[0].session_id == "pane:%9"
    assert out[0].status == AgentStatus.BLOCKED_PERMISSION and out[0].needs_human
    assert out[0].last_action == "asking whether to trust loom-os"
    assert state_mod.counts(out) == (1, 1)


def test_the_scanner_caps_captures_per_tick_and_rereads_a_quiet_window_only_now_and_then():
    reads = []
    clock = [0.0]

    def capture(target, tmux=None):
        reads.append(target)
        return TRUST if target == "pantheon:1" else NORMAL

    scanner = dialogs.Scanner(capture=capture, max_per_tick=2, rescan_seconds=10, clock=lambda: clock[0])
    rows = [_row(f"pane:%{i}", window=i, pane=f"%{i}") for i in range(1, 4)]
    found = scanner.scan(rows)
    assert len(reads) == 2 and set(found) == {"pane:%1"}
    clock[0] = 1.0
    reads.clear()
    found = scanner.scan(rows)
    assert reads == ["pantheon:1", "pantheon:3"]      # the dialog window + the one never read
    clock[0] = 2.0
    reads.clear()
    scanner.scan(rows)
    assert reads == ["pantheon:1"]                    # the quiet ones wait for rescan_seconds
    # The dialog window gets a fresh hook event (it started): it leaves the candidates and its
    # mark goes with it.
    assert scanner.scan(rows[1:]) == {}


# --------------------------------------------------------------------------- the toast


def _cfg(tmp_path, **notify):
    cfg = config_mod.Config(state_dir=str(tmp_path))
    return dataclasses.replace(cfg, notify=notify) if notify else cfg


def test_one_toast_per_appearance_and_again_after_it_clears(tmp_path):
    sent = []
    cfg = _cfg(tmp_path)
    fake = lambda *a: (sent.append(a) or (True, "toast started"))
    assert startup_dialogs.announce(cfg, "%9|trust_folder", "body", send_toast=fake)
    assert not startup_dialogs.announce(cfg, "%9|trust_folder", "body", send_toast=fake)
    startup_dialogs.forget_except(cfg, [])
    assert startup_dialogs.announce(cfg, "%9|trust_folder", "body", send_toast=fake)
    assert len(sent) == 2
    assert sent[0][0] == startup_dialogs.TITLE and sent[0][3].endswith("hooks/pantheon_toast.ps1")
    assert ":" in sent[0][3][:3]                       # a drive path pwsh.exe can open
    lines = cfg.notify_sent_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2 and json.loads(lines[0])["kind"] == "startup_dialog"


def test_the_off_switch_records_but_does_not_toast(tmp_path):
    sent = []
    cfg = _cfg(tmp_path, startup_dialogs=False)
    assert not startup_dialogs.announce(cfg, "k", "body", send_toast=lambda *a: sent.append(a))
    assert sent == []


def test_msys_paths_become_drive_paths():
    assert startup_dialogs.windows_path(Path("/c/Home/x/hooks/t.ps1")) == "C:/Home/x/hooks/t.ps1"
    assert startup_dialogs.windows_path(Path("/tmp/pw/t.ps1")) == "C:/msys64/tmp/pw/t.ps1"


# --------------------------------------------------------------------------- the deck, end to end


class FakeTmux:
    def __init__(self, screen):
        self.screen = screen
        self.keys = []

    def run(self, *args, tmux=None, check=False):
        out = ""
        if args[0] == "capture-pane":
            out = self.screen
        elif args[0] == "send-keys" and "-l" not in args:
            key = args[-1]
            self.keys.append(key)
            if key == "Down":
                self.screen = self.screen.replace(" > No, exit", "   No, exit").replace(
                    "   Yes, I trust this folder", " > Yes, I trust this folder")
            elif key == "Enter" and " > Yes, I trust this folder" in self.screen:
                self.screen = NORMAL
        return subprocess.CompletedProcess(args, 0, out, "")


def test_the_deck_shows_the_trust_question_toasts_once_and_answers_it(tmp_path, monkeypatch):
    fake = FakeTmux(TRUST)
    monkeypatch.setattr(tmuxctl, "run", fake.run)
    toasts = []
    monkeypatch.setattr(channels, "toast", lambda *a, **k: (toasts.append(a) or (True, "ok")))
    cfg = config_mod.Config(state_dir=str(tmp_path))
    cfg.events_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.events_file.write_text("", encoding="utf-8")
    cfg = dataclasses.replace(cfg, projects_root="C:/Home/x/Documents/Projects")
    window = TmuxWindow(index=4, name="trustprobe_a1", command="C:\\Home\\x\\.local\\bin\\claude.exe",
                        path="C:/msys64/tmp/trustprobe_a1", pane_id="%9", session="pantheon")
    app = SupervisorApp(cfg=cfg, window_source=lambda: [window])
    seen = {}

    async def drive():
        async with app.run_test(size=(120, 24)) as pilot:
            await pilot.pause()
            app.pane.refresh_rows()                    # a second tick: still one toast
            table = app.query_one("#agents", DataTable)
            seen["cells"] = [c.plain if hasattr(c, "plain") else str(c) for c in table.get_row_at(0)]
            seen["header"] = str(app.query_one("#header", Static).content)
            await pilot.press("enter")
            await pilot.pause()
            seen["question"] = str(app.screen.query_one("#question", Static).content)
            seen["body"] = str(app.screen.query_one("#body", Static).content)
            await pilot.press("y")
            await pilot.pause()
            seen["status"] = str(app.query_one("#status", Static).content)
            seen["row_after"] = app.pane.rows[0].status

    asyncio.run(drive())
    #: the row reads as the one-line request (risk word, what, which).
    assert any("trust · folder" in c for c in seen["cells"])
    assert "1 need you" in seen["header"]
    assert len(toasts) == 1 and "window 4" in toasts[0][1].lower()
    assert "Trust" in seen["question"] and "closes Claude" in seen["body"]
    assert fake.keys == ["Down", "Enter"]
    assert "answered yes" in seen["status"]
    assert seen["row_after"] != AgentStatus.BLOCKED_PERMISSION
