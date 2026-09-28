"""the planning page. Three queue rows picked -> the totals footer's expected
5-hour figure is the sum of the three per-row figures (the same numbers `bin/plan_show` prints);
a fourth large row past a threshold set 1% above that sum flips `fits` to `over by`."""
from __future__ import annotations

import asyncio

import pytest

from pantheon import config
from pantheon.hud import planning as P
from pantheon.models import TaskRow
from pantheon.planning import app as plan_app
from pantheon.planning import fit
from tests.test_plan_fit import seed_rows, sized_pairs


class FakeSource:
    def __init__(self, rows):
        self._rows = rows

    def rows(self):
        return list(self._rows)

    def refresh(self):
        return False


ROWS = [
    TaskRow(id="a", summary="write the S13 spec closeout", project="project_lanterns", est_context="small", agent=True),
    TaskRow(id="b", summary="vet a plugin", project="habit_notes", est_context="medium", agent=True),
    TaskRow(id="c", summary="rework the queue header", project="project_lanterns", est_context=None),
    TaskRow(id="d", summary="big refactor", project="loom-os", est_context="large"),
    TaskRow(id="e", summary="already done", done=True, est_context="small"),
]
LIMITS = seed_rows() + sized_pairs()


def _beliefs(five_now=None):
    return plan_app.Beliefs.from_rows(LIMITS, {"claude": {"five_hour_pct": five_now, "seven_day_pct": 20.0}})


def test_done_rows_are_not_offered_and_the_agents_plate_comes_first():
    rows = plan_app.open_rows(ROWS)
    assert [r.id for r in rows] == ["a", "b", "c", "d"]


def test_acceptance_4_totals_are_the_sum_and_fits_flips_with_a_fourth_large_row():
    candidates = plan_app.open_rows(ROWS)
    beliefs = _beliefs()
    view = plan_app.render_plan(candidates, ["a", "b", "c"], 0, beliefs, "claude", P.SERIAL, 160)
    per_row = [e.points for e in view["rows_five"]]
    assert view["total_five"].points == pytest.approx(sum(per_row))
    # the same per-row figures plan_show prints
    shown = fit.believes(LIMITS)["row_estimates"]
    for size, points in zip(("small", "medium", "medium"), per_row):
        assert shown[f"claude {P.SERIAL} five_hour {size}"]["points"] == pytest.approx(points, abs=0.01)
    # threshold 1% above the sum: the window has sum + 1 points left
    now = 100.0 - (view["total_five"].points + 1.0)
    beliefs = _beliefs(five_now=now)
    three = plan_app.render_plan(candidates, ["a", "b", "c"], 0, beliefs, "claude", P.SERIAL, 160)
    assert not any("over by" in line for line in three["totals"])
    four = plan_app.render_plan(candidates, ["a", "b", "c", "d"], 0, beliefs, "claude", P.SERIAL, 160)
    assert any("over by" in line for line in four["totals"])


def test_totals_line_shows_range_and_counts():
    view = plan_app.render_plan(plan_app.open_rows(ROWS), ["a", "b"], 0, _beliefs(10.0), "claude",
                                P.TOGETHER, 160)
    line = view["totals"][0]
    assert line.startswith("2 rows · 5h: ") and "(" in line and "wk: " in line
    assert "fits the 5-hour window (now 10%)" in view["totals"][1] or "5-hour window:" in view["totals"][1]
    assert "from 0 past batches" not in view["basis"]
    assert "past one-at-a-time" not in view["basis"]           # together lane names batches


def test_codex_says_not_enough_data():
    view = plan_app.render_plan(plan_app.open_rows(ROWS), ["a"], 0, _beliefs(10.0), "codex",
                                P.TOGETHER, 160)
    assert "5h: not enough data" in view["totals"][0]
    assert "Codex: not enough data to cost this plan" in view["totals"]
    assert "not enough data" in view["basis"]


def test_phone_width_is_one_line_per_row_without_the_weekly_column():
    view = plan_app.render_plan(plan_app.open_rows(ROWS), ["a", "b"], 0, _beliefs(10.0), "claude",
                                P.TOGETHER, 60)
    assert all(len(line) <= 60 for line in view["list"] + view["plan"])
    assert all("wk" not in line for line in view["plan"])
    assert "wk:" not in view["totals"][0]


def test_the_page_mounts_and_space_adds_rows_in_order():
    async def _go():
        cfg = config.Config()
        app = plan_app.PlanApp(cfg, source=FakeSource(ROWS), limits_loader=lambda: LIMITS,
                               picture_loader=lambda: {"claude": {"five_hour_pct": 30.0}})
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.press("down", "space", "up", "space", "t")
            await pilot.pause()
            return app.picked, app.lane, app.view

    picked, lane, view = asyncio.run(_go())
    assert picked == ["b", "a"] and lane == P.SERIAL
    assert view["plan"][0].startswith(" 1. vet a plugin")
    assert "not wired yet" in view["dispatch"]
