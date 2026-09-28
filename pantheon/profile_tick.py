"""Perf-sweep profiling script: every performance fix ships with a before/after number from this
one-tick profiler.

Builds a synthetic `state/agents/events.jsonl` at three sizes (100 / 1,000 / 10,000 lines, a
realistic mix of SessionStart/PostToolUse/Stop/Notification/SessionEnd across a dozen sessions,
plus a few dispatch/codex lines) and a fake tmux window list, then times the per-tick hot paths
named in that open thread:

  (a) `events.read_events` + `supervisor.state.fold`
  (b) `dispatch.marks.dispatch_marks`
  (c) one `SupervisorPane.refresh_rows` and one `QueuePane._tick`, each run inside Textual's
      headless `App.run_test` harness (no real terminal, no real tmux) -- plus a SECOND,
      back-to-back call to each with nothing changed, which is what an idle 5-second tick looks
      like and what fixes 2-4 target
  (d) `QueuePane._paint_header` against ~180 fake task rows across 8 tabs

This is a hand-run tool, not a test: it prints a table, it asserts nothing, and it has no real
tmux to measure against, so every number is the headless-harness cost on this machine, not a
claim about the user's real terminal. Run it with `bin/profile_tick`.
"""
from __future__ import annotations

import asyncio
import json
import statistics
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from textual.app import App, ComposeResult

from . import config as config_mod
from . import events as events_mod
from . import tmuxctl
from .dispatch import marks as marks_mod
from .models import TaskRow, TmuxWindow, utcnow_iso
from .queue.pane import QueuePane
from .supervisor import state as state_mod
from .supervisor.pane import SupervisorPane
from .tasks.base import TabSpec

SIZES = (100, 1_000, 10_000)
REPEATS = 3          # median of this many runs per measurement -- one-shot timings are noisy
SESSIONS = 12
FAKE_PROJECTS_ROOT = "C:/Home/x/Documents/Projects"  # synthetic fixture data only -- no real box
PROJECTS = [
    "hiking_log_v2", "Plumb", "loom-os", "habit_notes", "project_lanterns", "dune_tracker",
]
TASK_ROW_COUNT = 180
TASK_TABS = 8


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# ---------------------------------------------------------------- synthetic events


def make_events(n: int) -> list[dict]:
    """A realistic mix, not a worst case: mostly Claude hook lines cycling through the states a
    real session actually visits, spread over a dozen sessions and half a dozen projects, plus a
    handful of dispatch/codex lines so `marks.dispatch_marks` has something to find."""
    start = datetime.now(timezone.utc) - timedelta(hours=2)
    session_ids = [f"session{i:02d}deadbeefcafe" for i in range(SESSIONS)]
    out: list[dict] = []
    for i in range(n):
        ts = _iso(start + timedelta(seconds=i))
        sid = session_ids[i % SESSIONS]
        project = PROJECTS[i % len(PROJECTS)]
        cwd = f"{FAKE_PROJECTS_ROOT}/{project}"
        pane = f"%{(i % 9) + 1}"
        cycle = i % 7
        if cycle == 0:
            out.append({"ts": ts, "source": "claude", "event": "SessionStart",
                       "session_id": sid, "cwd": cwd, "tmux_pane": pane})
        elif cycle in (1, 2, 3):
            out.append({"ts": ts, "source": "claude", "event": "PostToolUse", "session_id": sid,
                       "cwd": cwd, "tmux_pane": pane, "tool_name": "Read"})
        elif cycle == 4:
            out.append({"ts": ts, "source": "claude", "event": "Stop", "session_id": sid, "cwd": cwd})
        elif cycle == 5:
            out.append({"ts": ts, "source": "claude", "event": "Notification", "session_id": sid,
                       "cwd": cwd, "notification_type": "permission_prompt",
                       "message": "Claude needs your permission to use Bash"})
        else:
            out.append({"ts": ts, "source": "claude", "event": "SessionEnd", "session_id": sid, "cwd": cwd})
    for j in range(max(1, n // 200)):
        job = f"job{j}"
        out.append({"ts": _iso(start), "source": "pantheon", "event": "dispatch", "session_id": job,
                   "cwd": f"{FAKE_PROJECTS_ROOT}/Plumb", "tracker_id": f"row-{j}",
                   "row_id": f"row-{j}", "where": "tmux", "window_index": (j % 9) + 1})
        out.append({"ts": _iso(start), "source": "codex", "event": "queued", "job_id": job})
        out.append({"ts": _iso(start), "source": "codex", "event": "running", "job_id": job})
    return out


def write_events(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for d in rows:
            fh.write(json.dumps(d) + "\n")


def make_windows() -> list[TmuxWindow]:
    return [
        TmuxWindow(i + 1, PROJECTS[i % len(PROJECTS)], "node",
                  f"{FAKE_PROJECTS_ROOT}/{PROJECTS[i % len(PROJECTS)]}",
                  f"%{i + 1}", "pantheon")
        for i in range(SESSIONS)
    ]


# ---------------------------------------------------------------- synthetic task rows + tabs


def make_task_rows(n: int) -> list[TaskRow]:
    statuses = ["open", "in-progress", "blocked", "done"]
    tiers = ["now", "soon", "someday"]
    rows = []
    for i in range(n):
        rows.append(TaskRow(
            id=f"task-{i:04d}", summary=f"Do the thing number {i} with several words in it",
            project=PROJECTS[i % len(PROJECTS)], tier=tiers[i % len(tiers)],
            status=statuses[i % len(statuses)], complexity=["quick", "moderate", "heavy"][i % 3],
            agent=(i % 5 == 0), created=_iso(datetime.now(timezone.utc) - timedelta(days=i % 40)),
        ))
    return rows


@dataclass
class _FakeSource:
    """The minimum `tasks.base.TaskSource` shape `QueuePane` needs, standing in for a real
    Obsidian Base read."""

    name: str = "fake"
    kind: str = "profiling fixture"
    mode: str = "poll"
    notice: str = ""
    _rows: list[TaskRow] = field(default_factory=list)

    def rows(self) -> list[TaskRow]:
        return self._rows

    def tabs(self) -> list[TabSpec]:
        return [
            TabSpec("Now", "1", lambda r: r.tier == "now" and r.status != "done"),
            TabSpec("Decide", "2", lambda r: r.status == "blocked"),
            TabSpec("Soon", "3", lambda r: r.tier == "soon"),
            TabSpec("Agent's plate", "4", lambda r: r.agent is True,
                   group_by=lambda r: r.complexity or "unsized"),
            TabSpec("Someday", "5", lambda r: r.tier == "someday"),
            TabSpec("Done", "6", lambda r: r.status == "done"),
            TabSpec("By project", "7", lambda r: True),
            TabSpec("Quick wins", "8", lambda r: r.complexity == "quick" and r.status != "done"),
        ]

    def refresh(self) -> bool:
        return False

    def parse_errors(self) -> int:
        return 0

    def open_for_edit(self, row: TaskRow) -> str:
        return ""

    def flagged_tabs(self) -> set:
        return set()


class _Harness(App):
    """One widget, filling the screen -- just enough of an `App` for `run_test()` to mount it."""

    def __init__(self, widget) -> None:
        super().__init__()
        self._widget = widget

    def compose(self) -> ComposeResult:
        yield self._widget


def _timed(fn: Callable[[], object], repeats: int = REPEATS) -> float:
    """Median milliseconds over `repeats` calls."""
    samples = []
    for _ in range(repeats):
        t0 = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - t0) * 1000.0)
    return statistics.median(samples)


# ---------------------------------------------------------------- the per-size measurement


async def _pane_timings(cfg: config_mod.Config, windows: list[TmuxWindow], source: _FakeSource) -> dict:
    out: dict[str, float] = {}

    sup = SupervisorPane(cfg, window_source=lambda: windows)
    async with _Harness(sup).run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        out["sup_tick_cold_ms"] = _timed(sup.refresh_rows, repeats=1)
        out["sup_tick_idle_ms"] = _timed(sup.refresh_rows)   # nothing changed between calls now

    queue = QueuePane(cfg, source=source, standalone=False, window_source=lambda: windows)
    async with _Harness(queue).run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        out["queue_tick_cold_ms"] = _timed(queue._tick, repeats=1)
        out["queue_tick_idle_ms"] = _timed(queue._tick)
        out["paint_header_ms"] = _timed(queue._paint_header)

    # The scenario the sweep targets: both panes of the combined deck doing their own tick,
    # back to back, against the SAME events.jsonl -- this is where a shared read/poll pays off.
    sup2 = SupervisorPane(cfg, window_source=lambda: windows)
    queue2 = QueuePane(cfg, source=source, standalone=False, window_source=lambda: windows)
    async with _Harness(sup2).run_test(size=(120, 40)) as pilot_a:
        await pilot_a.pause()
        async with _Harness(queue2).run_test(size=(120, 40)) as pilot_b:
            await pilot_b.pause()

            def _one_combined_tick() -> None:
                sup2.refresh_rows()
                queue2._tick()

            out["combined_deck_tick_ms"] = _timed(_one_combined_tick)
    return out


def profile_size(n: int, tmp_root: Path) -> dict:
    cfg = config_mod.Config(
        state_dir=str(tmp_root / f"n{n}"), tmux_session="pantheon",
        projects_root=FAKE_PROJECTS_ROOT,
    )
    write_events(cfg.events_file, make_events(n))
    windows = make_windows()
    now = datetime.now(timezone.utc)

    row: dict = {"n": n}
    row["read_events_ms"] = _timed(lambda: events_mod.read_events(cfg.events_file))
    evs, _errors = events_mod.read_events(cfg.events_file)
    row["fold_ms"] = _timed(lambda: state_mod.fold(evs, windows, now, cfg.tmux_session, cfg.projects_root))
    row["dispatch_marks_ms"] = _timed(lambda: marks_mod.dispatch_marks(evs, windows))

    source = _FakeSource(_rows=make_task_rows(TASK_ROW_COUNT))
    row.update(asyncio.run(_pane_timings(cfg, windows, source)))
    return row


COLUMNS = [
    ("n", "events"),
    ("read_events_ms", "read_events"),
    ("fold_ms", "fold"),
    ("dispatch_marks_ms", "dispatch_marks"),
    ("sup_tick_cold_ms", "sup tick (cold)"),
    ("sup_tick_idle_ms", "sup tick (idle)"),
    ("queue_tick_cold_ms", "queue tick (cold)"),
    ("queue_tick_idle_ms", "queue tick (idle)"),
    ("paint_header_ms", "paint_header"),
    ("combined_deck_tick_ms", "combined deck tick"),
]


def render_table(rows: list[dict]) -> str:
    lines = ["| " + " | ".join(label for _key, label in COLUMNS) + " |",
             "|" + "---|" * len(COLUMNS)]
    for row in rows:
        cells = []
        for key, _label in COLUMNS:
            v = row.get(key)
            cells.append(str(v) if key == "n" else f"{v:.3f}")
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    label = (argv or sys.argv[1:] or ["run"])[0]
    with tempfile.TemporaryDirectory(prefix="pantheon-profile-") as tmp:
        tmp_root = Path(tmp)
        rows = [profile_size(n, tmp_root) for n in SIZES]
    print(f"# pantheon profile_tick -- {label}")
    print(f"# {utcnow_iso()} -- headless Textual harness, no real tmux (this machine only)\n")
    print(render_table(rows))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
