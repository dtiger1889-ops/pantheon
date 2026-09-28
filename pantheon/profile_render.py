"""Counts how
often `SupervisorPane` and `QueuePane` actually touch a Textual widget (`DataTable.clear` /
`DataTable.update_cell` / `Static.update`) over a simulated desk session, instead of just timing
one tick the way `profile_tick` does. Read-only instrumentation: it wraps the widget methods to
count calls, it does not change either pane's code. Run with `bin/profile_render`.

Desk size is 200x50,
since redraw cost only matters at the size the user's terminal actually runs.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from textual.app import App, ComposeResult
from textual.widgets import DataTable, Static

from . import config as config_mod
from .models import TaskRow, TmuxWindow, utcnow_iso
from .queue.pane import QueuePane
from .supervisor.pane import SupervisorPane
from .tasks.base import TabSpec

DESK_SIZE = (200, 50)   # redraw cost is only meaningful at desk size
TICKS = 60              # 5 minutes of the default 5-second tick, a typical desk session
PROJECTS = ["hiking_log_v2", "Plumb", "loom-os", "habit_notes", "project_lanterns"]


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


@dataclass
class _FakeSource:
    """Same fixture shape as `profile_tick._FakeSource`. `refresh()` is scripted per-tick by the
    scenario (below) instead of always returning False, since the real Obsidian source's `changed`
    return value is exactly what decides whether `QueuePane.redraw()` runs at all."""

    name: str = "fake"
    kind: str = "profiling fixture"
    mode: str = "poll"
    notice: str = ""
    _rows: list[TaskRow] = field(default_factory=list)
    _changed_script: list[bool] = field(default_factory=list)
    _i: int = 0

    def rows(self) -> list[TaskRow]:
        return self._rows

    def tabs(self) -> list[TabSpec]:
        return [
            TabSpec("Now", "1", lambda r: r.tier == "now" and r.status != "done"),
            TabSpec("Agent's plate", "4", lambda r: r.agent is True),
            TabSpec("By project", "7", lambda r: True),
        ]

    def refresh(self) -> bool:
        if self._i < len(self._changed_script):
            v = self._changed_script[self._i]
        else:
            v = False
        self._i += 1
        return v

    def parse_errors(self) -> int:
        return 0

    def open_for_edit(self, row: TaskRow) -> str:
        return ""

    def flagged_tabs(self) -> set:
        return set()


def make_task_rows(n: int = 40) -> list[TaskRow]:
    return [
        TaskRow(id=f"task-{i:03d}", summary=f"Do thing {i}", project=PROJECTS[i % len(PROJECTS)],
                tier="now", status="open", complexity="quick", agent=(i % 4 == 0),
                created=_iso(datetime.now(timezone.utc)))
        for i in range(n)
    ]


class _Harness(App):
    def __init__(self, widget) -> None:
        super().__init__()
        self._widget = widget

    def compose(self) -> ComposeResult:
        yield self._widget


def _scenario_events(tick: int, start: datetime, active_panes: list[str]) -> list[dict]:
    """What actually changes on the event log between two ticks, in a realistic desk session:
    mostly quiet (an idle agent, nothing happening for several ticks), occasional tool-use lines
    from whichever agents are "working" this tick, and once in a while a Stop / SessionEnd. This
    is a MIX, not the worst case -- the point is what a normal 5-minute stretch actually costs,
    which is what item 4 asks to measure."""
    ts = _iso(start + timedelta(seconds=tick * 5))
    out: list[dict] = []
    if tick % 3 != 0:  # 2 ticks out of 3: one active pane does something
        pane = active_panes[tick % len(active_panes)]
        out.append({"ts": ts, "source": "claude", "event": "PostToolUse", "session_id": f"s{pane}",
                   "cwd": f"C:/Home/x/Documents/Projects/{PROJECTS[tick % len(PROJECTS)]}",
                   "tmux_pane": pane, "tool_name": "Read"})
    if tick % 11 == 0 and tick > 0:  # occasionally an agent finishes
        pane = active_panes[tick % len(active_panes)]
        out.append({"ts": ts, "source": "claude", "event": "SessionEnd", "session_id": f"s{pane}",
                   "cwd": "C:/Home/x/Documents/Projects/project_lanterns"})
    return out


def run_scenario() -> dict:
    import tempfile
    with tempfile.TemporaryDirectory(prefix="pantheon-render-") as tmp:
        cfg = config_mod.Config(
            state_dir=tmp, tmux_session="pantheon",
            projects_root="C:/Home/x/Documents/Projects",
        )
        active_panes = [f"%{i + 1}" for i in range(5)]  # 5 agents running -- a busy desk session
        windows = [TmuxWindow(i + 1, PROJECTS[i % len(PROJECTS)], "node",
                              f"C:/Home/x/Documents/Projects/{PROJECTS[i % len(PROJECTS)]}",
                              p, "pantheon") for i, p in enumerate(active_panes)]
        start = datetime.now(timezone.utc)
        events_path = Path(cfg.events_file)
        events_path.parent.mkdir(parents=True, exist_ok=True)
        events_path.write_text("", encoding="utf-8")

        # Vault "changed" about 1 tick in 6 -- an Obsidian Base file re-saved by the user mid-session
        # is not a 5-second-clockwork event, it is however often he actually edits the note.
        changed_script = [(t % 6 == 0) for t in range(TICKS)]
        source = _FakeSource(_rows=make_task_rows(40), _changed_script=changed_script)

        counts = {
            "sup_full_rebuild": 0, "sup_age_patch_cells": 0, "sup_noop_ticks": 0,
            "queue_redraw_ran": 0, "queue_redraw_skipped": 0,
            "queue_body_update_called": 0, "queue_body_update_skipped": 0,
        }

        import asyncio

        async def _drive():
            sup = SupervisorPane(cfg, window_source=lambda: windows)
            queue = QueuePane(cfg, source=source, standalone=False, window_source=lambda: windows)
            async with _Harness(sup).run_test(size=DESK_SIZE) as pilot_a:
                await pilot_a.pause()
                async with _Harness(queue).run_test(size=DESK_SIZE) as pilot_b:
                    await pilot_b.pause()

                    real_clear = DataTable.clear
                    real_update_cell = DataTable.update_cell
                    real_static_update = Static.update
                    real_queue_redraw = QueuePane.redraw

                    def counted_clear(self, *a, **kw):
                        if self is sup.query_one("#agents", DataTable):
                            counts["sup_full_rebuild"] += 1
                        return real_clear(self, *a, **kw)

                    def counted_update_cell(self, *a, **kw):
                        if self is sup.query_one("#agents", DataTable):
                            counts["sup_age_patch_cells"] += 1
                        return real_update_cell(self, *a, **kw)

                    def counted_static_update(self, *a, **kw):
                        try:
                            is_queue_body = self is queue.query_one("#list", Static)
                        except Exception:
                            is_queue_body = False
                        if is_queue_body:
                            counts["queue_body_update_called"] += 1
                        return real_static_update(self, *a, **kw)

                    def counted_redraw(self):
                        if self is queue:
                            counts["queue_redraw_ran"] += 1
                        return real_queue_redraw(self)

                    with patch.object(DataTable, "clear", counted_clear), \
                         patch.object(DataTable, "update_cell", counted_update_cell), \
                         patch.object(Static, "update", counted_static_update), \
                         patch.object(QueuePane, "redraw", counted_redraw):
                        for tick in range(TICKS):
                            new_lines = _scenario_events(tick, start, active_panes)
                            if new_lines:
                                with open(events_path, "a", encoding="utf-8") as fh:
                                    import json
                                    for d in new_lines:
                                        fh.write(json.dumps(d) + "\n")

                            before_rebuild = counts["sup_full_rebuild"]
                            before_patch = counts["sup_age_patch_cells"]
                            sup.refresh_rows()
                            if counts["sup_full_rebuild"] == before_rebuild and \
                               counts["sup_age_patch_cells"] == before_patch:
                                counts["sup_noop_ticks"] += 1

                            before_redraw = counts["queue_redraw_ran"]
                            queue._tick()
                            if counts["queue_redraw_ran"] == before_redraw:
                                counts["queue_redraw_skipped"] += 1

        asyncio.run(_drive())
        counts["queue_body_update_skipped"] = counts["queue_redraw_ran"] - counts["queue_body_update_called"]
        return counts


def main(argv: list[str] | None = None) -> int:
    label = (argv or sys.argv[1:] or ["run"])[0]
    print(f"# pantheon profile_render -- {label}")
    print(f"# {utcnow_iso()} -- {TICKS} ticks (5s each = {TICKS * 5}s) at desk size {DESK_SIZE},")
    print("# 5 simulated active agents, headless Textual harness, no real tmux (this machine only)\n")
    counts = run_scenario()
    print(f"supervisor: {TICKS} ticks -> {counts['sup_full_rebuild']} full table rebuilds "
         f"(table.clear + re-add_row), {counts['sup_age_patch_cells']} individual age-cell "
         f"patches (update_cell) across all ticks, {counts['sup_noop_ticks']} ticks touched the "
         f"table not at all")
    print(f"queue:      {TICKS} ticks -> {counts['queue_redraw_ran']} ran redraw() (source said "
         f"'changed'), {counts['queue_redraw_skipped']} skipped redraw() entirely; of the ticks "
         f"that ran redraw(), {counts['queue_body_update_called']} actually called "
         f"Static.update() on the list body, {counts['queue_body_update_skipped']} were byte-"
         f"identical and were skipped by the item-3 guard")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
