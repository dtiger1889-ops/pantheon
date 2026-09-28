"""acceptance 2 and 5: the planner's beliefs re-fit from the limits file
(pantheon/planning/fit.py), on the 27 calibration seed rows plus synthetic sized pairs."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from pantheon import config
from pantheon.hud import limits
from pantheon.hud import planning as P
from pantheon.planning import fit

SEED = Path(__file__).resolve().parent / "fixtures" / "calibration" / "usage-burn-seed.jsonl"
RESET = "2026-09-25T15:00:00Z"
WEEK_RESET = "2026-09-30T03:00:00Z"


def seed_rows() -> list[dict]:
    return [json.loads(line) for line in SEED.read_text(encoding="utf-8").splitlines() if line.strip()]


def sized_pairs(tokens=(90_000, 100_000, 110_000, 95_000, 105_000, 100_000), size="medium",
                start_pct=20, hour="10"):
    """One Sonnet worker at a time, each +1 point of the 5-hour window, all inside one window."""
    rows = []
    pct = start_pct
    for i, tok in enumerate(tokens):
        repo = f"synthetic{i}"
        rows.append({"utc": f"2026-09-25T{hour}:{i * 5:02d}:00Z", "event": "before", "repo": repo,
                     "five_hour": pct, "five_hour_resets_at": RESET, "seven_day": 30,
                     "seven_day_resets_at": WEEK_RESET, "tokens": 0})
        pct += 1
        rows.append({"utc": f"2026-09-25T{hour}:{i * 5 + 4:02d}:00Z", "event": "after", "repo": repo,
                     "five_hour": pct, "five_hour_resets_at": RESET, "seven_day": 30,
                     "seven_day_resets_at": WEEK_RESET, "tokens": tok, "size": size,
                     "model": "claude-sonnet-5"})
    return rows


def test_seed_file_is_the_27_rows():
    assert len(seed_rows()) == 27


def test_acceptance_2_token_table_and_per_100k_on_seed_plus_six_sized_pairs():
    rows = seed_rows() + sized_pairs()
    table = fit.token_table(rows)
    medium = table[("claude", "medium")]
    mean = sum((90_000, 100_000, 110_000, 95_000, 105_000, 100_000)) / 6
    assert medium.basis == P.FITTED and medium.count == 6
    assert abs(medium.tokens - mean) <= 0.2 * mean
    serial = fit.per_100k(rows, P.SERIAL)
    rate = serial[("claude", "five_hour")]
    assert rate.basis == P.FITTED and rate.count >= 11
    assert 1.1 * 0.8 <= rate.mid <= 1.5 * 1.2                # the README's 1.1-1.5, within 20%


def test_sizes_without_sized_rows_use_every_finished_worker():
    rows = seed_rows()
    small = fit.token_table(rows)[("claude", "small")]
    assert small.basis == P.ALL_WORKERS and small.count == 15    # 12 benchmark + 3 plate workers
    lo, hi = small.spread
    assert lo <= 1.0 <= hi


def test_empty_file_falls_back_to_seeds_and_readme_labelled_seeded():
    table = fit.token_table([])
    assert table[("claude", "large")].tokens == P.SEED_TOKENS["large"]
    assert table[("claude", "large")].basis == P.SEEDED
    together = fit.per_100k([], P.TOGETHER)
    assert together[("claude", "five_hour")].mid == 3.1
    assert together[("claude", "five_hour")].basis == P.SEEDED


def test_codex_is_not_enough_data():
    rows = seed_rows() + sized_pairs()
    for lane in P.LANES:
        rates = fit.per_100k(rows, lane)
        assert rates[("codex", "five_hour")].mid is None
        est = P.row_estimate("medium", "codex", "five_hour", fit.token_table(rows), rates)
        assert P.range_words(est) == "not enough data"


def test_acceptance_5_a_new_actual_moves_the_same_size_estimate_toward_it():
    rows = seed_rows() + sized_pairs()
    before = fit.token_table(rows)[("claude", "medium")].tokens
    actual = 190_000
    rows += sized_pairs(tokens=(actual,), start_pct=40, hour="11")
    after = fit.token_table(rows)[("claude", "medium")].tokens
    assert abs(actual - after) < abs(actual - before)
    rates = fit.per_100k(rows, P.SERIAL)
    est_before = P.row_estimate("medium", "claude", "five_hour", fit.token_table(rows[:-2]), rates)
    est_after = P.row_estimate("medium", "claude", "five_hour", fit.token_table(rows), rates)
    assert est_after.points > est_before.points


def test_believes_is_plain_json_with_row_counts():
    out = fit.believes(seed_rows() + sized_pairs())
    json.dumps(out)
    assert out["tokens"]["claude medium"]["rows"] == 6
    assert out["per_100k"]["claude one at a time five_hour"]["rows"] >= 11
    assert out["per_100k"]["codex together five_hour"]["basis"] == "not enough data"


def test_refit_detail_keeps_the_pooled_numbers_and_adds_the_ratios():
    rows = seed_rows()
    plain = limits.refit(rows)
    detail = limits.refit(rows, detail=True)
    for key, value in plain.items():
        assert detail[key] == value
    assert len(detail["serial_five_hour_ratios"]) == plain["five_hour_pairs"]


def test_after_mark_carries_size(tmp_path):
    cfg = config.Config(state_dir=str(tmp_path / "state"))
    config.ensure_state_dirs(cfg)
    row = limits.mark(cfg, "after", repo="x", tokens=5, session_id="", write=True, size="Medium")
    assert row["size"] == "medium"
    assert limits.read_rows(cfg)[-1]["size"] == "medium"
    plain = limits.mark(cfg, "after", repo="x", tokens=5, session_id="", write=False)
    assert plain["size"] is None


def test_one_sized_row_keeps_the_all_worker_width():
    rows = seed_rows() + sized_pairs(tokens=(150_000,))
    medium = fit.token_table(rows)[("claude", "medium")]
    assert medium.basis == P.FEW and medium.tokens == 150_000
    assert medium.spread[0] < 1.0 < medium.spread[1]
