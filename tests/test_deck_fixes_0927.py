"""The deck fixes seen live on 2026-09-27 01:08:

B. an ended session never counts as running and sits with its project's finished ones, and the
   Assistant is left out of THE PIT's columns table by its session id AND its pane;
C. throwaway trial folders (temp locations) never become projects in SESSIONS;
D. the Assistant's phone link (Remote Control) dying is noticed, can be re-made with one key, and
   the Assistant carries a recognisable name.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
from pathlib import Path

from textual.app import App, ComposeResult

from pantheon import assistant as assistant_mod
from pantheon import config as config_mod
from pantheon.models import AgentState, AgentStatus
from pantheon.session_view import models as session_models
from pantheon.session_view import recent as recent_mod
from pantheon.supervisor import state as state_mod
from pantheon.supervisor.rail import SessionSidebar

ROOT = "C:/Home/x/Documents/Projects"


def make_cfg(tmp_path: Path, **extra) -> config_mod.Config:
    cfg = config_mod.Config(state_dir=str(tmp_path / "state"), projects_root=ROOT,
                            claude_home=str(tmp_path / "claude_home"), **extra)
    cfg.events_file.parent.mkdir(parents=True, exist_ok=True)
    return cfg


def save_assistant(cfg, **record) -> None:
    Path(cfg.state_dir).mkdir(parents=True, exist_ok=True)
    (Path(cfg.state_dir) / "assistant.json").write_text(json.dumps(record), encoding="utf-8")


def row(sid, status=AgentStatus.WAITING_INPUT, project="workspace", window=None, pane=None, cwd=ROOT,
        ts="2026-09-27T05:07:55.084Z"):
    return AgentState(session_id=sid, provider="claude", status=status, project=project, cwd=cwd,
                      window_index=window, tmux_pane=pane, tmux_session="pantheon" if window is not None else None,
                      last_event_ts=ts)


# --------------------------------------------------------------------------- B


def test_an_ended_session_is_a_finished_one_in_the_sidebar(tmp_path):
    cfg = make_cfg(tmp_path)
    rows = [row("alive", project="comic_strip_drafts", window=3, pane="%3"),
            row("ended", AgentStatus.GONE, project="comic_strip_drafts"),
            row("job", AgentStatus.DONE, project="comic_strip_drafts")]
    out = {e.session_id: e for e in recent_mod.live_entries(rows, cfg, claude_home=tmp_path / "ch")}
    assert out["alive"].group == session_models.LIVE
    for sid in ("ended", "job"):
        assert out[sid].group == session_models.RECENT
        assert out[sid].window_index is None
        assert out[sid].modified_ts == "2026-09-27T05:07:55.084Z"


def test_running_counts_leave_ended_rows_out():
    rows = [row("a"), row("b", AgentStatus.GONE), row("c", AgentStatus.FAILED), row("d", AgentStatus.WORKING)]
    assert state_mod.running_count(rows) == 2


def test_the_sidebar_heading_and_bottom_line_count_only_running_sessions(tmp_path):
    cfg = make_cfg(tmp_path)
    rows = [row("alive", project="comic_strip_drafts", window=3, pane="%3"),
            row("ended", AgentStatus.GONE, project="comic_strip_drafts")]
    got = {}

    class _App(App):
        def __init__(self):
            super().__init__()
            self.sidebar = SessionSidebar(
                cfg, rows_source=lambda: rows,
                entries_source=lambda r: recent_mod.live_entries(r, cfg, claude_home=tmp_path / "ch"),
                id="rail")

        def compose(self) -> ComposeResult:
            yield self.sidebar

    async def drive():
        app = _App()
        async with app.run_test(size=(60, 24)) as pilot:
            await pilot.pause()
            plan = app.sidebar._plan()
            got["heading"] = next(app.sidebar._row_text(s).plain for s in plan if s[0] == "project")
            got["sessions"] = [(s[1].session_id, s[1].group) for s in plan if s[0] == "session"]
            got["status"] = str(app.sidebar.query_one("#sidebar-status").render())

    asyncio.run(drive())
    assert got["heading"].endswith("comic_strip_drafts · 1 running")
    assert got["sessions"] == [("alive", session_models.LIVE), ("ended", session_models.RECENT)]
    assert got["status"] == "1 running"


def test_the_assistant_is_known_by_its_session_id_and_by_its_pane(tmp_path):
    cfg = make_cfg(tmp_path)
    save_assistant(cfg, session_id="5f40ea10", pane_id="%17", started_at="2026-09-25T18:02:02.000Z")
    by_id = row("5f40ea10", window=5, pane="%17")
    by_pane = row("after-a-clear", window=5, pane="%17")          # a /clear made a new conversation
    old_pane = row("dead-reuse", AgentStatus.GONE, pane="%17")    # same id, no live window: not it
    other = row("loom-os", project="loom-os", window=4, pane="%7")
    left = assistant_mod.without_assistant(cfg, [by_id, by_pane, old_pane, other])
    assert [r.session_id for r in left] == ["dead-reuse", "loom-os"]
    assert assistant_mod.reserved_panes(cfg) == frozenset({"%17"})
    view = assistant_mod.view(cfg, [by_id, by_pane, other], [])
    assert {"5f40ea10", "after-a-clear"} <= view.hide
    assert "loom-os" not in view.hide
    off = dataclasses.replace(cfg, assistant={"enabled": False})
    assert len(assistant_mod.without_assistant(off, [by_id, other])) == 2


def test_the_columns_table_leaves_the_assistant_out(tmp_path, monkeypatch):
    from pantheon.supervisor import pane as pane_mod

    cfg = make_cfg(tmp_path)
    save_assistant(cfg, session_id="5f40ea10", pane_id="%17", started_at="2026-09-25T18:02:02.000Z")
    rows = [row("5f40ea10", window=5, pane="%17"), row("loom-os", project="loom-os", window=4, pane="%7"),
            row("f95f1d79", AgentStatus.GONE)]
    monkeypatch.setattr(pane_mod.state_mod, "fold", lambda *a, **k: list(rows))
    got = {}

    class _App(App):
        def compose(self) -> ComposeResult:
            yield pane_mod.SupervisorPane(cfg, window_source=lambda: [], id="supervisor")

    async def drive():
        app = _App()
        async with app.run_test(size=(160, 30)) as pilot:
            sup = app.query_one("#supervisor")
            sup.hide_assistant = True          # the deck sets this while its sidebar shows
            sup._mark_dialogs = lambda r: r
            sup._mark_approvals = lambda r: r
            sup.refresh_rows()
            await pilot.pause()
            got["table"] = list(sup._row_keys)
            got["all"] = [r.session_id for r in sup.rows]
            got["subtitle"] = sup.subtitle()

    asyncio.run(drive())
    assert "5f40ea10" not in got["table"] and "loom-os" in got["table"]
    assert "5f40ea10" in got["all"]                  # still there for the sidebar's Assistant line
    assert got["subtitle"].startswith("1 running")   # the gone row is not running


# --------------------------------------------------------------------------- E


def test_the_phone_table_keeps_the_assistant_as_a_named_two_line_row(tmp_path, monkeypatch):
    from pantheon.supervisor import pane as pane_mod

    cfg = make_cfg(tmp_path)
    transcript = tmp_path / "assistant.jsonl"
    transcript.write_text(json.dumps({"type": "custom-title", "customTitle": "Pocket Fury"}) + "\n",
                          encoding="utf-8")
    save_assistant(cfg, session_id="5f40ea10", pane_id="%17", started_at="2026-09-25T18:02:02.000Z",
                   transcript_path=str(transcript))
    rows = [row("5f40ea10", AgentStatus.IDLE, window=3, pane="%17"),
            row("loom-os", AgentStatus.WORKING, project="loom-os", window=4, pane="%7")]
    rows[1].last_action = "Edit CHECKPOINT.md"
    monkeypatch.setattr(pane_mod.state_mod, "fold", lambda *a, **k: list(rows))
    got = {}

    class _App(App):
        def compose(self) -> ComposeResult:
            yield pane_mod.SupervisorPane(cfg, window_source=lambda: [], id="supervisor")

    async def drive():
        app = _App()
        async with app.run_test(size=(46, 30)) as pilot:
            sup = app.query_one("#supervisor")
            sup._mark_dialogs = lambda r: r
            sup._mark_approvals = lambda r: r
            sup.refresh_rows()
            await pilot.pause()
            got["phone"] = {k: sup._cell_values(r, pane_mod.datetime.now(pane_mod.timezone.utc))[0]
                            for k, r in zip(sup._row_keys, sup.table_rows)}
            sup.set_hide_assistant(True)                  # the deck at desk width
            await pilot.pause()
            got["desk"] = list(sup._row_keys)

    asyncio.run(drive())
    first, second = got["phone"]["5f40ea10"].splitlines()
    assert first.endswith("Assistant · Pocket Fury") and "workspace" not in first
    assert second.strip().startswith("idle · window 3")      # no `workspace` on the Assistant's row
    life_first, life_second = got["phone"]["loom-os"].splitlines()
    assert "loom-os" in life_first                        # no transcript: `untitled · loom-os`
    assert life_second.strip().startswith("working · loom-os · window 4")
    assert all(len(line) <= 46 - 4 for line in (first, second, life_first, life_second))   # clipped, never wrapped
    assert got["desk"] == ["loom-os"]
