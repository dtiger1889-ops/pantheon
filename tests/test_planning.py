"""the projection arithmetic (pantheon/hud/planning.py). Pure functions: each has
a positive and a boundary case, and `None` in gives `None` / `?` out, never an exception."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pantheon.hud import planning as P


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


NOW = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- percentile

def test_percentile_is_linear_between_ranks():
    data = [1, 2, 3, 4, 5, 6]
    assert P.percentile(data, 10) == pytest.approx(1.5)     # (6-1)*0.1 = 0.5 -> 1 + 0.5
    assert P.percentile(data, 90) == pytest.approx(5.5)
    assert P.percentile(data, 50) == pytest.approx(3.5)


def test_percentile_boundaries():
    assert P.percentile([], 10) is None
    assert P.percentile([7.0], 90) == 7.0
    assert P.percentile([3, 1, 2], 0) == 1 and P.percentile([3, 1, 2], 100) == 3


# --------------------------------------------------------------------------- project_window

def test_project_window_adds_burn_times_hours():
    assert P.project_window(40, 6, 2.5) == pytest.approx(55)


def test_project_window_boundary_and_none():
    assert P.project_window(40, 6, 0) == 40
    assert P.project_window(40, 6, -1) == 40                 # the past is not projected
    assert P.project_window(None, 6, 1) is None
    assert P.project_window(40, None, 1) is None
    assert P.project_window(40, 6, None) is None
    assert P.project_window("x", 6, 1) is None


# --------------------------------------------------------------------------- row_estimate

SIX = (1.0, 2.0, 3.0, 4.0, 5.0, 6.0)


def test_row_estimate_interval_is_hand_computed_p10_p90_of_six_same_size_rows():
    table = {("claude", "medium"): 100_000}                   # a bare number counts as fitted
    rates = {("claude", "five_hour"): P.Rate(3.0, SIX, P.FITTED)}
    est = P.row_estimate("medium", "claude", "five_hour", table, rates)
    assert est.points == pytest.approx(3.0)
    assert est.low == pytest.approx(1.5) and est.high == pytest.approx(5.5)
    assert est.rows == 6 and est.labels == ()


def test_row_estimate_scales_with_tokens_and_widens_by_token_spread():
    table = {("claude", "large"): P.Tokens(200_000, (0.5, 2.0), 9, P.FITTED)}
    rates = {("claude", "five_hour"): P.Rate(3.0, SIX, P.FITTED)}
    est = P.row_estimate("large", "claude", "five_hour", table, rates)
    assert est.points == pytest.approx(6.0)
    assert est.low == pytest.approx(200_000 * 0.5 / 100_000 * 1.5)
    assert est.high == pytest.approx(200_000 * 2.0 / 100_000 * 5.5)


def test_row_estimate_under_five_rows_is_min_max_and_says_so():
    rates = {("claude", "five_hour"): P.Rate(2.0, (1.0, 4.0, 2.0, 3.0), P.FITTED)}
    est = P.row_estimate("medium", "claude", "five_hour", {("claude", "medium"): 100_000}, rates)
    assert (est.low, est.high) == (pytest.approx(1.0), pytest.approx(4.0))
    assert P.FEW in est.labels


def test_row_estimate_without_data_is_not_enough_data_and_never_raises():
    est = P.row_estimate("small", "codex", "five_hour", {}, {})
    assert est.points is None and P.NO_DATA in est.labels
    assert P.range_words(est) == "not enough data"
    est = P.row_estimate(None, "claude", "five_hour", None, None)
    assert est.points is None


def test_row_estimate_seeded_cost_has_no_range():
    est = P.row_estimate("medium", "claude", "five_hour", {}, {("claude", "five_hour"): 1.5})
    assert est.points == pytest.approx(1.5)                  # seed tokens 100k x 1.5
    assert est.low is None and est.high is None
    assert "tokens seeded, not fitted" in est.labels
    assert P.range_words(est) == "2% (no range)"


def test_unsized_rows_cost_as_medium():
    assert P.normal_size(None) == P.normal_size("unsized") == P.normal_size("n/a") == "medium"
    assert P.normal_size(" Large ") == "large"


# --------------------------------------------------------------------------- plan_totals

def test_plan_totals_sums_points_lows_and_highs():
    rows = [P.Estimate(100_000, 3.0, 1.5, 5.5, 6), P.Estimate(50_000, 1.0, 0.5, 2.0, 6)]
    total = P.plan_totals(rows)
    assert (total.points, total.low, total.high) == (4.0, 2.0, 7.5)
    assert total.tokens == 150_000


def test_plan_totals_boundaries():
    empty = P.plan_totals([])
    assert (empty.points, empty.low, empty.high) == (0.0, 0.0, 0.0)
    unknown = P.plan_totals([P.Estimate(1, 3.0, 1.0, 5.0), P.Estimate(1, None, None, None)])
    assert unknown.points is None                            # a partial sum is not the plan
    no_range = P.plan_totals([P.Estimate(1, 3.0, 1.0, 5.0), P.Estimate(1, 1.0, None, None)])
    assert no_range.points == 4.0 and no_range.low is None


# --------------------------------------------------------------------------- fits

def test_fits_words():
    total = P.Estimate(0, 10.0, 8.0, 14.0)
    assert P.fits(total, 50) == "fits"
    assert P.fits(total, 86) == "fits"                        # 86 + 14 = 100 exactly: fits
    assert P.fits(total, 87) == "tight (projected 101%)"
    assert P.fits(total, 90) == "tight (projected 104%)"      # 90 + 10 = 100: still expected to fit
    assert P.fits(total, 91) == "over by 1%"


def test_fits_unknown_is_question_mark():
    assert P.fits(None, 10) == "?"
    assert P.fits(P.Estimate(0, None, None, None), 10) == "?"
    assert P.fits(P.Estimate(0, 1.0, 0.5, 2.0), None) == "?"


# --------------------------------------------------------------------------- the card's projection

def _samples(*points):
    """(minutes before NOW, percent) -> the [iso, pct] pairs hud.json keeps."""
    return [[iso(NOW - timedelta(minutes=m)), pct] for m, pct in points]


def test_burn_band_is_calc_burn_with_a_whole_number_range():
    band = P.burn_band(_samples((30, 10), (15, 11), (0, 12)), now=NOW.timestamp())
    assert band["mid"] == pytest.approx(4.0)                 # +2 points in half an hour
    assert band["low"] == pytest.approx(2.0) and band["high"] == pytest.approx(6.0)
    assert band["readings"] == 3


def test_burn_band_none_when_calc_has_no_burn():
    assert P.burn_band([], now=NOW.timestamp()) is None
    assert P.burn_band(_samples((0, 12)), now=NOW.timestamp()) is None
    old = _samples((120, 10), (90, 12))                      # the newest reading is 90 min old
    assert P.burn_band(old, now=NOW.timestamp()) is None


def test_block_projection_to_the_reset():
    samples = _samples((30, 10), (0, 12))
    proj = P.block_projection(12, samples, iso(NOW + timedelta(hours=2)), now=NOW.timestamp())
    assert proj["pct"] == pytest.approx(20.0)
    assert proj["low"] == pytest.approx(16.0) and proj["high"] == pytest.approx(24.0)
    assert proj["readings"] == 2 and proj["caution"] is False


def test_block_projection_caution_when_the_top_crosses_90_before_reset():
    samples = _samples((30, 60), (0, 70))                     # 20%/h, range 18..22
    proj = P.block_projection(70, samples, iso(NOW + timedelta(hours=1)), now=NOW.timestamp())
    assert proj["caution"] is True and proj["high"] >= 90


def test_block_projection_none_inputs():
    samples = _samples((30, 10), (0, 12))
    reset = iso(NOW + timedelta(hours=2))
    assert P.block_projection(None, samples, reset, now=NOW.timestamp()) is None
    assert P.block_projection(12, [], reset, now=NOW.timestamp()) is None
    assert P.block_projection(12, samples, None, now=NOW.timestamp()) is None
    past = iso(NOW - timedelta(minutes=1))
    assert P.block_projection(12, samples, past, now=NOW.timestamp()) is None


def test_words():
    proj = {"pct": 70.4, "low": 64.2, "high": 131.0, "readings": 3}
    assert P.projection_words(proj, "01:10") == "70% (64-100%) by 01:10 · 3 readings"
    assert P.projection_words(None, "01:10") is None
    assert P.pct_words(None) == "?" and P.pct_words(0.2) == "<1%" and P.pct_words(0) == "0%"
    assert P.range_words(P.Estimate(0, 34.2, 28.0, 46.4)) == "34% (28-46%)"
