"""Snapshot tests for the four screens: the combined
deck, the standalone supervisor ("THE PIT"), the standalone queue, and the budget/HUD pane.

One committed SVG per screen (`tests/__snapshots__/test_snapshots/`), compared byte-for-byte by
`pytest-textual-snapshot`'s `snap_compare` fixture. Determinism (no snapshot may ever legitimately
differ two runs apart) needs two things beyond the fixed `terminal_size` every app test already
uses:

1. Frozen fixture DATA -- the same frozen-fixture pattern as `test_deck_app.py`/`test_hud_app.py`
   (copy the Sprints fixtures, write one `events.jsonl` line, write one `hud.json` picture), but
   with every timestamp pinned to `FROZEN` instead of stamped at test-run time.
2. A frozen wall CLOCK -- most panes only ever format timestamps pulled out of that fixture data
   (`models.parse_ts` + `format_clock`), which is already deterministic. But a few spots read the
   real clock directly to decide what "now" is: the deck header's own clock (`deck/app.py`), the
   queue's "vault read" timestamp (`queue/pane.py`, stamped when it reads), the age math the
   supervisor and queue panes fold events through, the HUD's day-boundary/burn-since math, and the
   queue's day-count for a task's `started` date. `_freeze_clock` patches `datetime`/`date` in
   each of those modules (each did `from datetime import ...`, so each needs its own patch) to a
   fixed instant, so none of it drifts with the real clock between one test run and the next.

Update with `python -m pytest tests/test_snapshots.py --snapshot-update` after a deliberate
rendering change, then eyeball the new SVGs under `tests/__snapshots__/test_snapshots/` before
committing them -- an unreviewed `--snapshot-update` just rubber-stamps whatever the screen now
draws, bug included.
"""
from __future__ import annotations

import json
import shutil
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from pantheon import config as config_mod
from pantheon.deck import app as deck_app_mod
from pantheon.hud import app as hud_app_mod
from pantheon.hud import calc as hud_calc_mod
from pantheon.hud import sources as hud_sources_mod
from pantheon.hud import strip as hud_strip_mod
from pantheon.models import TmuxWindow
from pantheon.queue import pane as queue_pane_mod
from pantheon.supervisor import pane as supervisor_pane_mod
from pantheon.supervisor import rail as supervisor_rail_mod
from pantheon.tasks import obsidian_base as ob

from . import deck_session_fixture

FIXTURES = Path(__file__).parent / "fixtures"
CWD = "C:/Home/x/Documents/Projects/hiking_log_v2"

# One instant, well inside the fixture picture's 5-hour/weekly reset windows (hud.json resets
# both after 2026-09-01T23:50), so nothing crosses a reset boundary and starts reading "overdue".
FROZEN = datetime(2026, 9, 1, 23, 55, 0, tzinfo=timezone.utc)
FROZEN_ISO = FROZEN.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

PANTHEON_WINDOW = TmuxWindow(3, "hiking_log_v2", "node", CWD, "%3", "pantheon")


class _FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return FROZEN.astimezone(tz) if tz is not None else FROZEN.replace(tzinfo=None)


class _FrozenDate(date):
    @classmethod
    def today(cls):
        return FROZEN.date()


@pytest.fixture
def frozen_clock(monkeypatch):
    """Pin every module that reads the real clock to `FROZEN` (see the module docstring)."""
    for mod in (
        deck_app_mod, queue_pane_mod, supervisor_pane_mod, supervisor_rail_mod,
        hud_app_mod, hud_calc_mod, hud_strip_mod, hud_sources_mod,
    ):
        monkeypatch.setattr(mod, "datetime", _FrozenDateTime)
    monkeypatch.setattr(queue_pane_mod, "date", _FrozenDate)
    # The fixture picture is 5 minutes old at FROZEN, past the age at which the deck's strip
    # runs its own quick usage pass; that
    # pass would read the machine's real Codex logs into the picture. The snapshot is of the
    # fixture, so the pass is switched off here.
    monkeypatch.setattr(hud_sources_mod, "refresh_if_stale", lambda *a, **k: None)
    return FROZEN


def _vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    projects = vault / "Projects"
    shutil.copytree(FIXTURES / "sprints", projects / "Sprints")
    shutil.copy(FIXTURES / "Sprints.base", projects / "Sprints.base")
    return vault


def _events_line() -> str:
    return json.dumps({"ts": FROZEN_ISO, "source": "claude", "event": "Stop",
                       "session_id": "abc12345deadbeef", "cwd": CWD, "tmux_pane": "%3"}) + "\n"


def _write_hud_picture(cfg: config_mod.Config) -> None:
    picture = json.loads((FIXTURES / "hud" / "hud.json").read_text(encoding="utf-8"))
    cfg.hud_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.hud_file.write_text(json.dumps(picture), encoding="utf-8")


def test_deck_combined_view(frozen_clock, snap_compare, tmp_path):
    vault = _vault(tmp_path)
    cfg = config_mod.Config(vault=str(vault), state_dir=str(tmp_path / "state"),
                            projects_root="C:/Home/x/Documents/Projects")
    cfg.events_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.events_file.write_text(_events_line(), encoding="utf-8")
    _write_hud_picture(cfg)
    app = deck_app_mod.DeckApp(cfg, window_source=lambda: [PANTHEON_WINDOW], source=ob.make(cfg))
    # the SESSIONS sidebar's own source scans `~/.claude/projects` and `~/.codex/sessions`
    # and stamps each row with a file mtime, which would make this snapshot differ every run and
    # publish whatever the user happened to be running. The wall reads the sidebar's list, so
    # pinning the sidebar pins both panels (tests/deck_session_fixture.py).
    app.rail._entries_source = deck_session_fixture.entries
    # the sidebar's MORE PROJECTS section scans the real workspace folder; pin it too.
    app.rail._projects_source = deck_session_fixture.projects

    async def _wait(pilot):
        await pilot.pause()
        # The wall parses its transcripts on a worker thread; without this the snapshot catches
        # the cards before their conversations arrive and freezes a wall of empty boxes.
        await pilot.app.workers.wait_for_complete()
        await pilot.pause()

    assert snap_compare(app, terminal_size=(180, 45), run_before=_wait)


def test_supervisor_pit_view(frozen_clock, snap_compare, tmp_path):
    cfg = config_mod.Config(state_dir=str(tmp_path / "state"),
                            projects_root="C:/Home/x/Documents/Projects")
    cfg.events_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.events_file.write_text(_events_line(), encoding="utf-8")
    from pantheon.supervisor.app import SupervisorApp
    app = SupervisorApp(cfg=cfg, window_source=lambda: [PANTHEON_WINDOW])
    assert snap_compare(app, terminal_size=(120, 40), run_before=lambda pilot: pilot.pause())


def test_queue_view(frozen_clock, snap_compare, tmp_path):
    vault = _vault(tmp_path)
    cfg = config_mod.Config(vault=str(vault), state_dir=str(tmp_path / "state"))
    from pantheon.queue.app import QueueApp
    app = QueueApp(cfg, source=ob.make(cfg))
    assert snap_compare(app, terminal_size=(120, 40), run_before=lambda pilot: pilot.pause())


def test_budget_hud_view(frozen_clock, snap_compare, tmp_path):
    cfg = config_mod.Config(state_dir=str(tmp_path / "state"))
    _write_hud_picture(cfg)
    app = hud_app_mod.HudApp(cfg, start_refresher=False)

    async def _wait(pilot):
        await pilot.app.workers.wait_for_complete()
        await pilot.pause()

    assert snap_compare(app, terminal_size=(92, 30), run_before=_wait)


# ---------------------------------------------------------------- the phone tier must not move

PHONE_RENDER = "docs/renders/deck-65x26.txt"
PHONE_COLUMNS = 65


def test_deck_phone_view(frozen_clock, snap_compare, tmp_path):
    """The 65-column deck, over SSH from the user's phone -- the tier is not allowed to change.

    The desk snapshot above is regenerated deliberately whenever the look changes; this one is
    the floor. A diff here means a desk-only change reached the phone, and the fix is the change,
    not a new baseline."""
    vault = _vault(tmp_path)
    cfg = config_mod.Config(vault=str(vault), state_dir=str(tmp_path / "state"),
                            projects_root="C:/Home/x/Documents/Projects")
    cfg.events_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.events_file.write_text(_events_line(), encoding="utf-8")
    _write_hud_picture(cfg)
    app = deck_app_mod.DeckApp(cfg, window_source=lambda: [PANTHEON_WINDOW], source=ob.make(cfg))
    assert snap_compare(app, terminal_size=(PHONE_COLUMNS, 26),
                        run_before=lambda pilot: pilot.pause())


def test_nothing_on_the_phone_render_runs_past_65_columns():
    """The failure this catches, found regenerating the renders for the deck's phone status
    line had grown to 64 characters against the 63 the row can draw, so `F3 = budget` reached
    The user's phone as `F3 =`. A sentence one character too long is invisible in code review and
    obvious here."""
    path = Path(__file__).resolve().parent.parent / PHONE_RENDER
    if not path.exists():
        pytest.skip(f"{PHONE_RENDER} is not in this checkout (renders are not shipped)")
    over = [(n, line) for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
            if len(line.rstrip()) > PHONE_COLUMNS]
    assert not over, f"{PHONE_RENDER} draws past {PHONE_COLUMNS} columns on line(s): {over}"
