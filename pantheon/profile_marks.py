"""Isolates `dispatch.marks.dispatch_marks` cost as a function of the
number of currently-open dispatch marks AND the size of the
event log, independently of `profile_tick`'s combined-deck scenario (which grows both together at
a fixed small ratio and never isolates the mark-count dimension).

Before the fix, `dispatch_marks` called `_ended_after` once
per open mark, and each call re-scanned the *entire* event list -- O(marks x events). After the
fix, `_ended_after` looks up two dicts built in one O(events) pass, so the whole function is
O(events + marks). This script holds one dimension fixed while growing the other, so the
before/after numbers show which term dominated.

Run with `bin/profile_marks`.
"""
from __future__ import annotations

import statistics
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Callable

from .dispatch import marks as marks_mod
from .models import Event, TmuxWindow, utcnow_iso

REPEATS = 5


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def make_events(n_events: int, n_marks: int) -> list[Event]:
    """`n_marks` open (never-ended) dispatch marks, one per tmux pane 1..n_marks, plus filler
    Claude hook events cycling through ordinary states up to a total of `n_events` lines. Marks
    are left open (no matching SessionEnd/kill) so `_ended_after` always runs its full scan in the
    unfixed code -- the worst case, and the realistic one: an open mark is exactly "an agent still
    running", which is what the queue pane is checking on every 5-second tick."""
    start = datetime.now(timezone.utc) - timedelta(hours=1)
    out: list[Event] = []
    n_marks = max(0, n_marks)
    for j in range(n_marks):
        pane = f"%{j + 1}"
        out.append(Event(
            ts=_iso(start), event="dispatch", source="pantheon", tmux_pane=pane,
            extra={"row_id": f"row-{j}", "tracker_id": f"row-{j}", "where": "tmux",
                   "window_index": (j % 50) + 1},
        ))
    n_filler = max(0, n_events - n_marks)
    cycle_events = ["PostToolUse", "PostToolUse", "Stop", "Notification", "PostToolUse"]
    for i in range(n_filler):
        ts = _iso(start + timedelta(seconds=i + 1))
        out.append(Event(
            ts=ts, event=cycle_events[i % len(cycle_events)], source="claude",
            session_id=f"session{i % 20:02d}", tmux_pane=f"%{(i % 50) + 1}",
        ))
    return out


def make_windows(n_marks: int) -> list[TmuxWindow]:
    return [TmuxWindow(j + 1, "project_lanterns", "node", "C:/x", f"%{j + 1}", "pantheon")
            for j in range(max(1, n_marks))]


def _timed(fn: Callable[[], object], repeats: int = REPEATS) -> float:
    samples = []
    for _ in range(repeats):
        t0 = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - t0) * 1000.0)
    return statistics.median(samples)


# Three synthetic sizes, isolating the marks dimension: events fixed at a realistic 2,000-line
# log (about what a day of hook traffic produces), marks growing 20x across the sweep -- from "a
# few agents running" to "way more than the user would ever run at once" so the growth curve is
# visible before it would ever bite in practice.
SIZES_FIXED_EVENTS = [(2_000, 25), (2_000, 250), (2_000, 2_500)]

# And the other axis: marks fixed at a realistic "5 agents running", event log growing -- this is
# what actually happens over a long session as the log accumulates.
SIZES_FIXED_MARKS = [(2_000, 5), (10_000, 5), (50_000, 5)]


def _run_sweep(label: str, sizes: list[tuple[int, int]]) -> list[dict]:
    rows = []
    for n_events, n_marks in sizes:
        events = make_events(n_events, n_marks)
        windows = make_windows(n_marks)
        ms = _timed(lambda: marks_mod.dispatch_marks(events, windows))
        rows.append({"events": n_events, "marks": n_marks, "ms": ms})
    return rows


def render(label: str, rows: list[dict]) -> str:
    lines = [f"### {label}", "| events | open marks | dispatch_marks_ms |", "|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['events']} | {r['marks']} | {r['ms']:.3f} |")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    label = (argv or sys.argv[1:] or ["run"])[0]
    print(f"# pantheon profile_marks -- {label}")
    print(f"# {utcnow_iso()} -- isolates dispatch_marks() cost; no real tmux, this machine only\n")
    print(render("fixed events (2,000), marks growing 25 -> 250 -> 2,500",
                  _run_sweep(label, SIZES_FIXED_EVENTS)))
    print()
    print(render("fixed marks (5), events growing 2,000 -> 10,000 -> 50,000",
                  _run_sweep(label, SIZES_FIXED_MARKS)))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
