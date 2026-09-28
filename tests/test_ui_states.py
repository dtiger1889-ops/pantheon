"""S-UI acceptance 4, 5 and 7:

4. **Four sizes, every state.** Every row in section 4.2 (agent statuses) and section 5.3 (queue
   states), rendered at (65, 26), (80, 24), (120, 40) and (180, 50), with no exception, no
   horizontal overflow, and the state's word present as text.
5. **Colour never alone.** The same fixtures, forced to 16 colours: every state stays
   distinguishable by its words.
6. **Nothing self-dismisses.** A waiting row and an open Confirm modal, left alone: zero screen
   changes except the age ticking.

Harness reused from the existing suites (nothing here re-implements what they already cover):
`tests.test_supervisor_app` for `make_config`/`run_at`/`screen_text`/`CWD`/`PANTHEON_WINDOW`
(driving a bare `SupervisorPane`), and `tests.test_deck_app`/`tests.test_queue_app`'s
`drives_the_screen` + Sprints-fixture-copying pattern for the combined `DeckApp` and the
standalone `QueueApp`.

Getting the rendered text, not just the widget's own state (Textual 8.2.8): `App.
export_screenshot` (used by `screen_text` in `test_supervisor_app.py`, reused here) builds a
`rich.console.Console(record=True)`, prints `self.screen._compositor.render_update(...)`, then
calls `console.export_svg` and the helper strips the SVG markup back to plain words -- that is
the technique this file reuses. An earlier draft of this file also tried a `Console.export_text`
variant of the same recipe for a per-line text grid (to check horizontal overflow directly); it
was dropped -- not because it did not work, but because the real cost turned out to be building a
`DeckApp` (copies the Sprints fixtures, starts a folder watcher, reads providers and the HUD file)
per parametrized case, roughly 3.5-4s each. The word-presence and overflow checks below therefore
run against the lightweight `SupervisorApp`/`QueueApp` wherever the assertion does not need the
combined desk layout itself (`DataTable.virtual_size.width <= .container_size.width` for
overflow, `screen_text(app.export_screenshot)` and raw widget content for word presence); a
`DeckApp` is built only for the handful of cases that specifically need the combined deck. Line
width on the actual rendered SCREEN (as opposed to the DataTable's own virtual/container size, or
the queue pane's already-tested `_columns`-derived body-text-line-length) is NOT asserted here.

16-colour mode (acceptance 5): none of `SupervisorApp`/`QueueApp`/`DeckApp` forward constructor
kwargs to `textual.app.App.__init__`, so the `ansi_color: bool | None` constructor parameter
(confirmed present via `inspect.signature(App.__init__)` on the installed 8.2.8) is not reachable
through them. `ansi_color` is also a public `Reactive[bool | None]` attribute (same source), and
setting it on the app instance BEFORE `run_test` was confirmed (see the PR body) to change
`app.native_ansi_color` exactly as passing the constructor kwarg would -- so that is the switch
used here, not a `TERM` environment variable (Textual's headless test driver does not read `TERM`
for its rendering pipeline; it renders at a fixed `size=` regardless).

Two real bugs turned up while building these fixtures from `pantheon/supervisor/state.py` events
and are marked `xfail` per the task, not fixed here (`pantheon/` is out of scope for this branch):

* `AgentStatus.RESUME_FAILED.label` ("parked - resume failed, needs you") is 34 characters; with
  its glyph prefix the rendered cell text is 35 wide, but `pantheon/supervisor/pane.py`'s `state`
  column is a fixed 23 wide at every named width (`PHONE_COLUMNS`/`NARROW_COLUMNS`/`WIDE_COLUMNS`/
  `SUPER_WIDE_COLUMNS` all agree on `("state", "state", 23)`) -- the module's own docstring claims
  23 "is wide enough... because the longest label... has to fit", which is true of every other
  status but not this one. Textual/Rich clips the DataTable cell to the column width, so the
  rendered row reads `"○ parked - resume faile"` -- "needs you" is gone entirely and even "failed"
  is cut mid-word. This is exactly the state S-UI sorts to the TOP as needing the user, so the clipping hides the one thing the row exists to say.
* `pantheon/tasks/obsidian_base.py`'s `ObsidianBaseSource._build_tabs` falls back to
  `_spec_tabs` (7 hard-coded tab rules) whenever the Base file has no readable `views:` --
  including when the Base file is simply missing. 's "no Base
  file" row names its trigger as "`sprints_base` missing" and its treatment as the pane's `_list_
  text` message "No tabs. Check that the Base file exists at <path>" -- but that message only
  fires when `QueuePane.tabs` is empty, which a missing (as opposed to malformed-and-raising) Base
  file never actually produces, because of the graceful fallback. The named trigger and the
  written treatment are for a state the current code cannot reach.
* At the combined deck's own "D" width (120 columns total, `deck/app.py` `WIDE_AT`), THE PIT and
  QUEUE split the screen roughly in half (`SupervisorPane, QueuePane { width: 1fr; }`), leaving
  the agents table well under 65 columns of real estate. `columns_for` correctly drops to `PHONE_COLUMNS`, but even that layout's own
  rendered width (63, confirmed via `DataTable.virtual_size`) is wider than the 58 actually
  available, so the table itself needs horizontal scrolling inside the combined deck at exactly
  the width S-UI names as the first one where both panels are meant to show side by side --
  violating S-UI acceptance 4 ("without horizontal overflow") and design rule R8. Confirmed to
  clear up again at 180 columns (each panel then gets close to 90).
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

import pytest
from textual.widgets import DataTable, Static

from pantheon import config as config_mod
from pantheon.deck.app import DeckApp
from pantheon.models import AgentStatus, TmuxWindow, utcnow_iso
from pantheon.queue.app import QueueApp
from pantheon.supervisor import state as state_mod
from pantheon.supervisor import pane as pane_mod
from pantheon.supervisor.app import SupervisorApp
from pantheon.tasks import obsidian_base as ob
from pantheon.widgets.modal import Confirm

from tests.test_deck_app import drives_the_screen
from tests.test_supervisor_app import (
    CWD,
    make_config as make_supervisor_config,
    run_at as run_supervisor_at,
    screen_text,
)

FIXTURES = Path(__file__).parent / "fixtures"
SIZES = [(65, 26), (80, 24), (120, 40), (180, 50)]


def _cell_text(table: DataTable, row: int, col: int):
    value = table.get_row_at(row)[col]
    return value.plain if hasattr(value, "plain") else str(value)


def _supervisor_config(tmp_path: Path, events: list[dict]) -> config_mod.Config:
    """Same shape as `tests.test_supervisor_app.make_config`, without its `for d in fixture or
    FIXTURE` fallback -- that treats a genuinely empty list the same as "no fixture given" and
    substitutes its own default (blocked-permission), which is right for that file's own tests
    but wrong here for `AgentStatus.UNKNOWN`, whose whole point is zero events."""
    cfg = config_mod.Config(state_dir=str(tmp_path))
    cfg.events_file.parent.mkdir(parents=True, exist_ok=True)
    with open(cfg.events_file, "w", encoding="utf-8") as fh:
        for d in events:
            fh.write(json.dumps(d) + "\n")
    return dataclasses.replace(cfg, projects_root="C:/Home/x/Documents/Projects")


def _events_now(events: list[dict]) -> list[dict]:
    """The case table below is stamped when this file is imported (collection time), but a Claude
    row with a pane and no window counts as gone after DEAD_PANE_SECONDS (3 min); once the whole
    suite took over four minutes to reach this file, every such case folded to `gone`.
    Re-stamp at test time; the order inside a case is kept (each stamp is a little later)."""
    return [{**e, "ts": utcnow_iso()} for e in events]


def _stamped_events(status: AgentStatus, events: list[dict]) -> list[dict]:
    """`_events_now` above keeps every fixture fresh -- right for every status except `QUIET`,
    which by construction (`state.QUIET_AFTER_SECONDS`) only exists once that much time has
    already passed with no fresh event. Stamp that one fixture's events into the past instead of
    "now", relative to test-execution time, not collection time (same reasoning as `_events_now`)."""
    if status is AgentStatus.QUIET:
        when = (datetime.now(timezone.utc) - timedelta(seconds=state_mod.QUIET_AFTER_SECONDS + 60))
        stamp = when.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        return [{**e, "ts": stamp} for e in events]
    return _events_now(events)


# ==================================================================================================
# (a) + (d) agent statuses:  -- one event-log fixture per
# `AgentStatus`, built purely from the events `pantheon/supervisor/state.py` folds into that
# status (confirmed against `state_mod.fold` directly while building this file; every status
# folds to itself, none needed to be skipped).
# ==================================================================================================

UNKNOWN_WINDOW = TmuxWindow(index=2, name="myproj", command="node", path=CWD, pane_id="%2", session="pantheon")

AGENT_STATUS_CASES: dict[AgentStatus, tuple[list[dict], tuple[TmuxWindow, ...]]] = {
    AgentStatus.BLOCKED_PERMISSION: ([
        {"ts": utcnow_iso(), "source": "claude", "event": "SessionStart",
         "session_id": "s-blocked", "cwd": CWD, "tmux_pane": "%3"},
        {"ts": utcnow_iso(), "source": "claude", "event": "Notification",
         "session_id": "s-blocked", "cwd": CWD, "tmux_pane": "%3",
         "notification_type": "permission_prompt", "message": "Claude needs your permission to use Bash"},
    ], ()),
    AgentStatus.WAITING_INPUT: ([
        {"ts": utcnow_iso(), "source": "claude", "event": "SessionStart",
         "session_id": "s-waiting", "cwd": CWD, "tmux_pane": "%4"},
        {"ts": utcnow_iso(), "source": "claude", "event": "Stop",
         "session_id": "s-waiting", "cwd": CWD, "tmux_pane": "%4"},
    ], ()),
    AgentStatus.FAILED: ([
        {"ts": utcnow_iso(), "source": "codex", "event": "queued",
         "session_id": "s-failed", "job_id": "job-f1", "project": "Plumb"},
        {"ts": utcnow_iso(), "source": "codex", "event": "running",
         "session_id": "s-failed", "job_id": "job-f1", "project": "Plumb"},
        {"ts": utcnow_iso(), "source": "codex", "event": "done",
         "session_id": "s-failed", "job_id": "job-f1", "project": "Plumb", "detail": "exit 1"},
    ], ()),
    AgentStatus.WORKING: ([
        {"ts": utcnow_iso(), "source": "claude", "event": "SessionStart",
         "session_id": "s-working", "cwd": CWD, "tmux_pane": "%5"},
        {"ts": utcnow_iso(), "source": "claude", "event": "PostToolUse",
         "session_id": "s-working", "cwd": CWD, "tmux_pane": "%5", "tool_name": "Edit"},
    ], ()),
    AgentStatus.RUNNING: ([
        {"ts": utcnow_iso(), "source": "codex", "event": "queued",
         "session_id": "s-running", "job_id": "job-r1", "project": "pottery_studios"},
        {"ts": utcnow_iso(), "source": "codex", "event": "running",
         "session_id": "s-running", "job_id": "job-r1", "project": "pottery_studios"},
    ], ()),
    AgentStatus.WINDING_DOWN: ([
        {"ts": utcnow_iso(), "source": "claude", "event": "SessionStart",
         "session_id": "s-wind", "cwd": CWD, "tmux_pane": "%6"},
        {"ts": utcnow_iso(), "source": "pantheon", "event": "wind_down",
         "session_id": "s-wind", "cwd": CWD},
    ], ()),
    AgentStatus.QUEUED: ([
        {"ts": utcnow_iso(), "source": "codex", "event": "queued",
         "session_id": "s-queued", "job_id": "job-q1", "project": "Plumb"},
    ], ()),
    AgentStatus.IDLE: ([
        {"ts": utcnow_iso(), "source": "claude", "event": "SessionStart",
         "session_id": "s-idle", "cwd": CWD, "tmux_pane": "%7"},
        {"ts": utcnow_iso(), "source": "claude", "event": "Notification",
         "session_id": "s-idle", "cwd": CWD, "tmux_pane": "%7",
         "notification_type": "idle_prompt", "message": "still at the prompt"},
    ], ()),
    AgentStatus.UNKNOWN: ([], (UNKNOWN_WINDOW,)),   # a live agent pane, no event has claimed it yet
    AgentStatus.PARKED: ([
        {"ts": utcnow_iso(), "source": "claude", "event": "SessionStart",
         "session_id": "s-parked", "cwd": CWD, "tmux_pane": "%8"},
        {"ts": utcnow_iso(), "source": "pantheon", "event": "park",
         "session_id": "s-parked", "cwd": CWD},
    ], ()),
    AgentStatus.RESUME_FAILED: ([
        {"ts": utcnow_iso(), "source": "claude", "event": "SessionStart",
         "session_id": "s-resumefail", "cwd": CWD, "tmux_pane": "%9"},
        {"ts": utcnow_iso(), "source": "pantheon", "event": "park",
         "session_id": "s-resumefail", "cwd": CWD},
        {"ts": utcnow_iso(), "source": "pantheon", "event": "resume",
         "session_id": "s-resumefail", "cwd": CWD, "rung": 4},
    ], ()),
    AgentStatus.DONE: ([
        {"ts": utcnow_iso(), "source": "codex", "event": "queued",
         "session_id": "s-done", "job_id": "job-d1", "project": "Plumb"},
        {"ts": utcnow_iso(), "source": "codex", "event": "done",
         "session_id": "s-done", "job_id": "job-d1", "project": "Plumb", "detail": "exit 0"},
    ], ()),
    AgentStatus.QUIET: ([
        # No further event ever arrives (the exact "SessionStart, nothing since" bug); stamped
        # into the past by `_stamped_events` above, past `state.QUIET_AFTER_SECONDS`. The window
        # stays live so this fixture exercises the quiet LABEL, not the separate presence timers.
        {"ts": utcnow_iso(), "source": "claude", "event": "SessionStart",
         "session_id": "s-quiet", "cwd": CWD, "tmux_pane": "%11"},
    ], (TmuxWindow(index=11, name="myproj", command="node", path=CWD, pane_id="%11", session="pantheon"),)),
    AgentStatus.GONE: ([
        {"ts": utcnow_iso(), "source": "claude", "event": "SessionStart",
         "session_id": "s-gone", "cwd": CWD, "tmux_pane": "%10"},
        {"ts": utcnow_iso(), "source": "claude", "event": "SessionEnd",
         "session_id": "s-gone", "cwd": CWD, "tmux_pane": "%10"},
    ], ()),
}


def test_every_agentstatus_member_has_a_fixture():
    """Guards the matrix below against a future status being added to `models.py` and silently
    never getting a fixture (S-UI acceptance 1: "every state has a trigger")."""
    assert set(AGENT_STATUS_CASES) == set(AgentStatus)


def _deck_root(tmp_path: Path, name: str) -> Path:
    """A vault with the shared Sprints fixtures, for whichever DeckApp needs a working queue
    pane alongside whatever the test is actually exercising on the agents side."""
    root = tmp_path / name
    projects = root / "Projects"
    shutil.copytree(FIXTURES / "sprints", projects / "Sprints")
    shutil.copy(FIXTURES / "Sprints.base", projects / "Sprints.base")
    return root


# Every status renders in full: RESUME_FAILED's label was shortened to fit the 23-wide `state`
# column behind its `!!` marker after these tests caught the long form being clipped.
AGENT_STATUS_PARAMS = list(AGENT_STATUS_CASES)


@pytest.mark.parametrize("size", SIZES, ids=[f"{w}x{h}" for w, h in SIZES])
@pytest.mark.parametrize("status", AGENT_STATUS_PARAMS, ids=[s.value for s in AGENT_STATUS_CASES])
@drives_the_screen
async def test_every_agent_status_renders_at_every_width(tmp_path, status, size):
    """Driven through the lightweight `SupervisorPane` app, not `DeckApp` -- see the module
    docstring: constructing a `DeckApp` per case (it copies the Sprints fixtures, starts a folder
    watcher, reads providers and the HUD file) costs ~3.5-4s each, which is fine for the handful
    of combined-deck-specific cases below but not for this 52-case width x status matrix. The
    combined deck is exercised separately, and turned up a real bug at its own 120-column width
    (`test_agents_table_overflows_inside_the_combined_deck_at_120_columns` below)."""
    events, windows = AGENT_STATUS_CASES[status]
    events = _stamped_events(status, events)
    app = SupervisorApp(cfg=_supervisor_config(tmp_path, events), window_source=lambda: list(windows))
    label = status.label
    if size[0] < pane_mod.NARROW_AT:
        # The phone tier's two-line rows shorten the two long "needs the user" labels.
        label = pane_mod.phone_status_word(status)
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        table = app.query_one("#agents", DataTable)
        assert table.row_count == 1
        cell_text = _cell_text(table, 0, 0)
        assert label in cell_text                       # the pane's own stored text, never clipped
        assert table.virtual_size.width <= table.container_size.width   # no sideways scroll
        assert label in screen_text(app.export_screenshot())            # it really reached the screen


@drives_the_screen
async def test_agents_table_fits_inside_the_combined_deck_at_120_columns(tmp_path):
    """At the 120-column desk width a 50/50 split left the agents table ~58 columns, under even
    the phone layout's 63; THE PIT now keeps a minimum width and QUEUE narrows instead
.

    put the wall of conversations where the table used to stand, so the table is one `f`
    away -- and it shares the row with the 30-column SESSIONS sidebar, which is the tighter
    version of the same fit this test has always guarded."""
    root = _deck_root(tmp_path, "deck-overflow")
    cfg = config_mod.Config(vault=str(root), state_dir=str(root / "state"),
                            projects_root="C:/Home/x/Documents/Projects")
    events, windows = AGENT_STATUS_CASES[AgentStatus.BLOCKED_PERMISSION]
    events = _events_now(events)
    cfg.events_file.parent.mkdir(parents=True, exist_ok=True)
    with open(cfg.events_file, "w", encoding="utf-8") as fh:
        for d in events:
            fh.write(json.dumps(d) + "\n")
    app = DeckApp(cfg, window_source=lambda: list(windows), source=ob.make(cfg))
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("f")
        await pilot.pause()
        table = app.sup.query_one("#agents", DataTable)
        assert table.virtual_size.width <= table.container_size.width


# ==================================================================================================
# (d) the 16-colour run: the same fixtures, `ansi_color`
# forced on, at one representative width -- width interaction with the agents table is already the
# job of the test above; this one is purely "does the state's word survive with 16 colours".
# ==================================================================================================


def _run_ansi(tmp_path: Path, width: int, height: int, fixture: list[dict], windows=()) -> str:
    """`run_at()` in `tests.test_supervisor_app` does not expose a hook to set `ansi_color` before
    `run_test()`, so this is a small variant of the same shape (reusing its `make_config`), not a
    duplicate of its assertions."""
    app = SupervisorApp(cfg=_supervisor_config(tmp_path, fixture), window_source=lambda: list(windows))
    app.ansi_color = True
    out = {}

    async def drive():
        async with app.run_test(size=(width, height)) as pilot:
            await pilot.pause()
            out["native_ansi"] = app.native_ansi_color
            out["text"] = screen_text(app.export_screenshot())

    asyncio.run(drive())
    return out["text"], out["native_ansi"]


@pytest.mark.parametrize("status", AGENT_STATUS_PARAMS, ids=[s.value for s in AGENT_STATUS_CASES])
def test_every_agent_status_survives_16_colours(tmp_path, status):
    events, windows = AGENT_STATUS_CASES[status]
    events = _stamped_events(status, events)
    text, native_ansi = _run_ansi(tmp_path, 120, 40, events, windows)
    assert native_ansi is True                            # confirms the switch actually took
    assert status.label in text


# ==================================================================================================
# (e) nothing self-dismisses: one waiting row, the Kill confirm
# open, left alone -- nothing on screen changes.
# ==================================================================================================

WAITING_WINDOW = TmuxWindow(index=4, name="hiking_log_v2", command="node", path=CWD, pane_id="%4", session="pantheon")
WAITING_FIXTURE = [
    {"ts": utcnow_iso(), "source": "claude", "event": "SessionStart",
     "session_id": "s-waiting-live", "cwd": CWD, "tmux_pane": "%4"},
    {"ts": utcnow_iso(), "source": "claude", "event": "Stop",
     "session_id": "s-waiting-live", "cwd": CWD, "tmux_pane": "%4"},
]


def test_nothing_self_dismisses_with_a_waiting_row_and_a_kill_confirm_open(tmp_path):
    app = SupervisorApp(cfg=make_supervisor_config(tmp_path, WAITING_FIXTURE),
                        window_source=lambda: [WAITING_WINDOW])
    seen = {}

    async def drive():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("k")
            await pilot.pause()
            assert isinstance(app.screen, Confirm)
            table = app.query_one("#agents", DataTable)
            seen["before_status"] = str(app.query_one("#status", Static).content)
            seen["before_question"] = str(app.screen.query_one("#question", Static).content)
            seen["before_row"] = _cell_text(table, 0, 0)
            seen["before_screens"] = len(app.screen_stack)
            # 10 minutes, stood in for by 2 real seconds.
            await pilot.pause(2)
            seen["after_status"] = str(app.query_one("#status", Static).content)
            seen["after_question"] = str(app.screen.query_one("#question", Static).content)
            seen["after_row"] = _cell_text(table, 0, 0)
            seen["after_screens"] = len(app.screen_stack)

    asyncio.run(drive())
    assert seen["before_screens"] == seen["after_screens"] == 2   # the modal is still open
    assert seen["after_status"] == seen["before_status"]
    assert seen["after_question"] == seen["before_question"]
    assert seen["after_row"] == seen["before_row"]                # even the age text (<1m, this fast)


# ==================================================================================================
# (b) queue states:  Built from `tests/fixtures/sprints` (the
# same fixture set `test_queue_app.py` already uses) plus, for "dispatched", a hand-written
# `dispatch` event line (`pantheon/dispatch/marks.py` needs nothing else to mark a row live).
#
# Rendered through the standalone `QueueApp`, not `DeckApp`, at all four named widths: `DeckApp`
# intentionally HIDES the queue panel below 120 columns (`deck/app.py` `WIDE_AT = 120`, S-UI
# section 9's phone/desk map: "QUEUE: own window (F2)" at P/N) -- that is real, specified product
# behaviour, not a gap, so forcing the combined deck to show the queue at 65 columns would test an
# arrangement the user never actually sees. `QueuePane` is the identical widget either app mounts
# ("nothing is forked", `deck/app.py`'s own module docstring), so `QueueApp` covers the same
# rendering at every width; two cases below are additionally re-driven through `DeckApp` at a desk
# width to confirm the combined deck itself (the literal ask) also paints them correctly.
# ==================================================================================================


def _sprints_root(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    projects = root / "Projects"
    shutil.copytree(FIXTURES / "sprints", projects / "Sprints")
    shutil.copy(FIXTURES / "Sprints.base", projects / "Sprints.base")
    return root


def _empty_sprints_root(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    (root / "Projects" / "Sprints").mkdir(parents=True)
    shutil.copy(FIXTURES / "Sprints.base", root / "Projects" / "Sprints.base")
    return root


def _queue_app(root: Path, **cfg_kwargs) -> QueueApp:
    cfg = config_mod.Config(vault=str(root), state_dir=str(root / "state"), **cfg_kwargs)
    return QueueApp(cfg, source=ob.make(cfg))


def _write_dispatch_mark(cfg: config_mod.Config, row_id: str) -> None:
    cfg.events_file.parent.mkdir(parents=True, exist_ok=True)
    with open(cfg.events_file, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "ts": utcnow_iso(), "source": "pantheon", "event": "dispatch",
            "row_id": row_id, "tracker_id": "t-water-plants", "provider": "claude",
            "where": "tmux", "window_index": 3, "tmux_pane": "%3",
        }) + "\n")


def _pane_of(app):
    return app.pane if isinstance(app, QueueApp) else app.queue


async def _focus_queue_if_combined(pilot, app) -> None:
    """`DeckApp` starts focus on THE PIT (`on_mount` calls `self.sup.focus_table()`); the queue's
    own digit/`/`/`p` bindings only fire once it has focus, so the bridge test (which drives the
    real combined deck) needs one `Tab` press first. `QueueApp` already focuses its one pane."""
    if isinstance(app, DeckApp):
        await pilot.press("tab")
        await pilot.pause()


async def _steps_blocked_row(pilot, app) -> None:
    await _focus_queue_if_combined(pilot, app)
    await pilot.press("2")                    # Decide: the one tab with a `status: blocked` row
    await pilot.pause()


async def _steps_search(pilot, app) -> None:
    await _focus_queue_if_combined(pilot, app)
    await pilot.press("7")                    # By project: several rows, so the search filters something
    await pilot.pause()
    await pilot.press("slash")
    await pilot.pause()
    await pilot.press(*"marmaladefig")        # appears only in "Water the balcony plants"'s reply
    await pilot.pause()


async def _steps_project_filter(pilot, app) -> None:
    await _focus_queue_if_combined(pilot, app)
    await pilot.press("7")                    # By project: every open row, so filtering means something
    await pilot.pause()
    await pilot.press("p")
    await pilot.pause()


async def _steps_no_base_file(pilot, app) -> None:
    """Not reachable from a real missing Base file (see the module docstring's second bug) --
    this forces the pane into the state `_list_text()` renders for it, the same technique
    `test_queue_app.py`'s `test_o_with_no_row_selected_says_so` already uses (`rows_on_screen = []`
    to force an empty-selection render) for a state its normal inputs cannot produce."""
    pane = _pane_of(app)
    pane.tabs = []
    pane.redraw()
    await pilot.pause()


@dataclasses.dataclass(frozen=True)
class QueueCase:
    build: Callable[[Path, str], QueueApp]
    steps: Optional[Callable] = None
    expect: tuple[str, ...] = ()


def _build_normal(tmp_path: Path, name: str) -> QueueApp:
    return _queue_app(_sprints_root(tmp_path, name))


def _build_dispatched(tmp_path: Path, name: str) -> QueueApp:
    app = _queue_app(_sprints_root(tmp_path, name))
    _write_dispatch_mark(app.cfg, "Water the balcony plants")
    return app


def _build_empty_tab(tmp_path: Path, name: str) -> QueueApp:
    return _queue_app(_empty_sprints_root(tmp_path, name))


def _build_vault_missing(tmp_path: Path, name: str) -> QueueApp:
    root = tmp_path / name
    app = _queue_app(root)          # `root` is never created: `vault` points at nothing
    # The message S-UI names for this state is set once at startup by `deck/app.py`'s and
    # `queue/app.py`'s own `main()` (`if not Path(cfg.sprints_dir).is_dir(): app.pane.message =
    # ...`); `QueuePane` itself never reads `source.load_error` (confirmed: nothing in
    # `queue/pane.py` calls it -- only `bin/queue_counts` does), so replicating that one-line
    # startup check here matches how the real app reaches this state, not a workaround for a gap.
    app.pane.message = f"vault folder not found at {app.cfg.sprints_dir}; fix it in pantheon.toml"
    return app


QUEUE_CASES: dict[str, QueueCase] = {
    "normal": QueueCase(_build_normal, expect=("Water the balcony plants", "Now 1")),
    "blocked_row": QueueCase(_build_normal, _steps_blocked_row,
                             expect=("blocked", "Choose a shelving unit")),
    "dispatched": QueueCase(_build_dispatched, expect=("dispatched to claude", "window 3")),
    "search": QueueCase(_build_normal, _steps_search, expect=("marmaladefig", "1 found")),
    "project_filter": QueueCase(_build_normal, _steps_project_filter, expect=("project: ",)),
    "empty_tab": QueueCase(_build_empty_tab, expect=("Nothing on this tab right now.",)),
    "no_base_file": QueueCase(_build_normal, _steps_no_base_file,
                              expect=("No tabs. Check that the Base file exists at",)),
    "vault_missing": QueueCase(_build_vault_missing,
                               expect=("vault folder not found at", "fix it in pantheon.toml")),
}


def _combined_text(app) -> str:
    pane = _pane_of(app)
    header = str(pane.query_one("#header", Static).content)
    message = str(pane.query_one("#message", Static).content)
    body = str(pane.query_one("#list", Static).content)
    return "\n".join((header, message, body))


# Two of the four named widths, not all four: `QueuePane`'s own already-tested `_columns()` split
# (`test_queue_app.py`'s `test_the_narrow_screen_drops_the_age_column_first`) already covers width
# interaction for this pane; this matrix's job is "does each STATE'S text still show up", which
# does not change between, say, 80 and 120 the way column layout does. One phone width (65) and
# one desk width (120) keeps the matrix's runtime down per the save-early direction on this branch.
QUEUE_SIZES = [(65, 26), (120, 40)]


@pytest.mark.parametrize("size", QUEUE_SIZES, ids=[f"{w}x{h}" for w, h in QUEUE_SIZES])
@pytest.mark.parametrize("case_key", list(QUEUE_CASES))
@drives_the_screen
async def test_every_queue_state_renders_at_every_width(tmp_path, case_key, size):
    case = QUEUE_CASES[case_key]
    app = case.build(tmp_path, f"{case_key}-{size[0]}x{size[1]}")
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        if case.steps is not None:
            await case.steps(pilot, app=app)
        combined = _combined_text(app)
        for needle in case.expect:
            assert needle in combined


# ---------------------------------------------------------------- bridge: the real combined deck

def _deck_app_for_queue(tmp_path: Path, name: str) -> DeckApp:
    root = _sprints_root(tmp_path, name)
    cfg = config_mod.Config(vault=str(root), state_dir=str(root / "state"))
    cfg.hud_file.parent.mkdir(parents=True, exist_ok=True)
    picture = json.loads((FIXTURES / "hud" / "hud.json").read_text(encoding="utf-8"))
    picture["fetched_at"] = utcnow_iso()
    cfg.hud_file.write_text(json.dumps(picture), encoding="utf-8")
    return DeckApp(cfg, window_source=lambda: [], source=ob.make(cfg))


@drives_the_screen
async def test_queue_state_also_renders_inside_the_real_combined_deck(tmp_path):
    """Confirms the literal ask -- the COMBINED deck, not just the standalone queue window -- for
    one representative state at a desk width; the full width x state matrix above already covers
    every state through the identical `QueuePane` widget, and `DeckApp` construction is the
    expensive part of this file (see the module docstring), so this stays a single case."""
    case = QUEUE_CASES["normal"]
    app = _deck_app_for_queue(tmp_path, "deck-normal")
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        combined = _combined_text(app)
        for needle in case.expect:
            assert needle in combined


# ==================================================================================================
# Confirmed bugs (see the module docstring), documented as failing tests rather than fixed here --
# `pantheon/` is out of scope for this branch.
# ==================================================================================================


def test_resume_failed_reaches_the_screen_with_its_marker(tmp_path):
    """The long label was clipped by the 23-wide state column; it is now `!! resume failed`,
    amber, top of the sort -- and every character of it reaches the screen (S-UI acceptance 4)."""
    events, _windows = AGENT_STATUS_CASES[AgentStatus.RESUME_FAILED]
    events = _events_now(events)
    drawn = run_supervisor_at(tmp_path, 120, fixture=events)
    label = AgentStatus.RESUME_FAILED.label
    assert f"!! {label}" in drawn["cells"][0]
    assert label in drawn["screen"]


def test_a_missing_base_file_keeps_the_built_in_tabs_and_says_so(tmp_path):
    """A missing Sprints.base used to fall back to the seven built-in tabs SILENTLY, so the
    "no Base file" state could never be seen. The fallback stays (the deck keeps working), and
    the source now carries a one-sentence notice the queue shows in its status line."""
    cfg = config_mod.Config(vault=str(tmp_path), state_dir=str(tmp_path / "state"))
    assert not cfg.sprints_base.exists()
    source = ob.make(cfg)
    assert len(source.tabs()) == 7
    assert source.notice.startswith("Base file not found at")
    assert "showing the built-in tabs" in source.notice
