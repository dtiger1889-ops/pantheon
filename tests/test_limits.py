"""The plan-percentage series + batch arithmetic (pantheon/hud/limits.py): the collector appends a
sample only when a number moves, marks read the live capture, and the batch sums are the ones
The user plans by -- never a point across a window reset."""
from __future__ import annotations

import json

from pantheon import config
from pantheon.hud import limits, sources


def _cfg(tmp_path):
    cfg = config.Config(state_dir=str(tmp_path / "state"))
    config.ensure_state_dirs(cfg)
    return cfg


def _capture(cfg, session_id, five=40, week=10, five_reset=1_800_000_000, cost=3.5):
    data = {
        "session_id": session_id,
        "model": {"id": "claude-fable-5-1"},
        "cost": {"total_cost_usd": cost},
        "rate_limits": {
            "five_hour": {"used_percentage": five, "resets_at": five_reset},
            "seven_day": {"used_percentage": week, "resets_at": 1_800_500_000},
        },
    }
    sources.write_atomic(cfg.statusline_dir / f"{session_id}.json", json.dumps(data))


# --------------------------------------------------------------------------- the file

def test_rows_round_trip_and_bad_lines_are_skipped(tmp_path):
    cfg = _cfg(tmp_path)
    limits.append_row(cfg, limits._row(event="start", note="x"))
    with open(limits.limits_file(cfg), "a", encoding="utf-8") as fh:
        fh.write("not json\n\n")
    rows = limits.read_rows(cfg)
    assert len(rows) == 1 and rows[0]["event"] == "start" and rows[0]["provider"] == "claude"
    assert set(limits.FIELDS) <= set(rows[0])


def test_missing_file_reads_as_empty(tmp_path):
    assert limits.read_rows(_cfg(tmp_path)) == []


# --------------------------------------------------------------------------- samples

def test_collector_sample_only_when_a_number_moves():
    picture = {"fetched_at": "2026-09-04T20:00:00Z",
               "claude": {"five_hour_pct": 41.0, "seven_day_pct": 12.0,
                          "five_hour_resets_at": "2026-09-05T00:00:00Z", "seven_day_resets_at": None,
                          "source": "statusline"},
               "codex": {"five_hour_pct": None, "seven_day_pct": None}}
    first = limits.sample_rows(picture, [])
    assert [r["provider"] for r in first] == ["claude"]
    assert first[0]["event"] == "sample" and first[0]["five_hour"] == 41.0
    # nothing moved -> nothing appended
    assert limits.sample_rows(picture, first) == []
    # the seed rows carry no provider field and count as Claude
    seed = [{"event": "after", "five_hour": 41, "seven_day": 12,
             "five_hour_resets_at": "2026-09-05T00:00:00Z", "seven_day_resets_at": None}]
    assert limits.sample_rows(picture, seed) == []
    picture["claude"]["five_hour_pct"] = 43.0
    assert len(limits.sample_rows(picture, first)) == 1


def test_a_window_reset_is_a_new_sample_even_at_the_same_percent():
    picture = {"claude": {"five_hour_pct": 1.0, "five_hour_resets_at": "B"}}
    last = [{"provider": "claude", "five_hour": 1.0, "five_hour_resets_at": "A"}]
    assert len(limits.sample_rows(picture, last)) == 1


# --------------------------------------------------------------------------- marks

def test_mark_reads_this_sessions_capture(tmp_path):
    cfg = _cfg(tmp_path)
    _capture(cfg, "sess-1", five=63, week=12)
    row = limits.mark(cfg, "before", repo="android", session_id="sess-1", now="2026-09-03T19:53:07Z")
    assert row["five_hour"] == 63 and row["seven_day"] == 12 and row["repo"] == "android"
    assert row["five_hour_resets_at"] == "2027-01-15T08:00:00Z"
    assert row["model"] == "claude-fable-5-1" and row["session_cost_usd"] == 3.5
    assert row["note"] == ""
    assert limits.read_rows(cfg)[-1]["utc"] == "2026-09-03T19:53:07Z"


def test_mark_without_a_capture_lands_with_nulls_and_says_why(tmp_path):
    cfg = _cfg(tmp_path)
    row = limits.mark(cfg, "after", repo="loom-os", tokens=108507, duration_ms=130202,
                      session_id="", note="1 row")
    assert row["five_hour"] is None and row["tokens"] == 108507
    assert "no statusline capture" in row["note"] and row["note"].startswith("1 row")


def test_mark_falls_back_to_the_newest_live_capture(tmp_path):
    cfg = _cfg(tmp_path)
    _capture(cfg, "other", five=70)
    row = limits.mark(cfg, "before", repo="x", session_id="desktop-app-session")
    assert row["five_hour"] == 70 and "newest live capture" in row["note"]


def test_mark_rejects_an_unknown_event(tmp_path):
    try:
        limits.mark(_cfg(tmp_path), "middle")
    except ValueError:
        return
    raise AssertionError("a bad event must not write a row")


# --------------------------------------------------------------------------- arithmetic

_CLOCK = [0]


def _t():
    _CLOCK[0] += 1
    return f"2026-09-05T01:{_CLOCK[0]:02d}:00Z"


def _pair(repo, before, after, tokens, reset="R1", reset_after=None, week=(12, 12)):
    return [
        {"event": "before", "repo": repo, "five_hour": before, "five_hour_resets_at": reset,
         "seven_day": week[0], "seven_day_resets_at": "W", "tokens": 0, "utc": _t()},
        {"event": "after", "repo": repo, "five_hour": after, "five_hour_resets_at": reset_after or reset,
         "seven_day": week[1], "seven_day_resets_at": "W", "tokens": tokens, "utc": _t()},
    ]


def test_parallel_agents_use_the_bar_total_not_the_sum_of_overlapping_rows():
    """The first live batch: four befores at 15%, afters at 20/20/20/30 -- summed +30 while the bar
    moved 14 -> 30 = +16. Overlapping pairs give up their own points; the batch reports the bar."""
    rows = [{"event": "start", "five_hour": 14, "five_hour_resets_at": "R", "seven_day": 11,
             "seven_day_resets_at": "W", "utc": "2026-09-05T01:28:24Z"}]
    for i, repo in enumerate(("a", "b", "c", "d")):
        rows.append({"event": "before", "repo": repo, "five_hour": 15, "five_hour_resets_at": "R",
                     "seven_day": 11, "seven_day_resets_at": "W", "utc": f"2026-09-05T01:29:5{i}Z"})
    for i, (repo, pct) in enumerate((("a", 20), ("b", 20), ("c", 20), ("d", 30))):
        rows.append({"event": "after", "repo": repo, "five_hour": pct, "five_hour_resets_at": "R",
                     "seven_day": 12 if pct < 30 else 13, "seven_day_resets_at": "W",
                     "tokens": 100_000, "utc": f"2026-09-05T01:{35 + i}:00Z"})
    s = limits.summarize(rows)
    assert s["parallel"] == 4 and s["rows"] == 4
    assert s["five_hour_points"] == 16 and s["seven_day_points"] == 2 and s["tokens"] == 400_000
    assert abs(s["points_per_100k"] - 4.0) < 1e-9
    assert "4 ran in parallel" in limits.batch_text(s)
    # refit never learns from overlapping pairs
    assert limits.refit(rows)["five_hour_pairs"] == 0


def test_no_start_means_no_batch():
    assert limits.summarize(_pair("a", 1, 3, 100_000)) is None
    assert limits.batch_text(None) is None


def test_batch_sums_points_tokens_and_rate():
    rows = [{"event": "start", "five_hour": 1, "five_hour_resets_at": "R1", "utc": "T0"}]
    rows += _pair("a", 1, 3, 100_000) + _pair("b", 3, 4, 120_000, week=(12, 13))
    s = limits.summarize(rows)
    assert s["rows"] == 2 and s["running"] == 0 and s["tokens"] == 220_000
    assert s["five_hour_points"] == 3 and s["seven_day_points"] == 1
    assert abs(s["points_per_100k"] - 3 / 2.2) < 1e-9
    assert s["bar_moved"] == 3 and s["since"] == "T0"
    assert limits.batch_text(s) == "2 rows · +3 pts 5h · +1 wk · 220k tok · 1.4 pts/100k"


def test_a_pair_across_a_window_reset_counts_tokens_but_never_points():
    rows = [{"event": "start", "five_hour": 74, "five_hour_resets_at": "R1"}]
    rows += _pair("loom-os", 74, 0, 110_112, reset="R1", reset_after="R2")
    rows += _pair("spatial", 1, 3, 112_331, reset="R2")
    s = limits.summarize(rows)
    assert s["crossed_reset"] == 1 and s["unmeasured"] == 1
    assert s["five_hour_points"] == 2 and s["tokens"] == 222_443
    assert abs(s["points_per_100k"] - 2 / 1.12331) < 1e-9     # only the measured tokens
    assert s["bar_moved"] is None                             # start and newest sit in different windows
    assert "1 crossed a reset" in limits.batch_text(s)


def test_a_running_row_shows_as_running_and_a_second_start_resets_the_batch():
    rows = [{"event": "start"}] + _pair("a", 1, 3, 100_000)
    rows += [{"event": "start"}, {"event": "before", "repo": "b", "five_hour": 3}]
    s = limits.summarize(rows)
    assert s["rows"] == 0 and s["running"] == 1
    assert limits.batch_text(s).startswith("0 rows (1 running)")


def test_codex_rows_pair_separately_from_claude_rows():
    rows = [{"event": "start"}]
    rows += [{"event": "before", "repo": "x", "provider": "codex", "five_hour": 10, "five_hour_resets_at": "C"},
             {"event": "before", "repo": "x", "provider": "claude", "five_hour": 50, "five_hour_resets_at": "R"},
             {"event": "after", "repo": "x", "provider": "codex", "five_hour": 14, "five_hour_resets_at": "C", "tokens": 50_000},
             {"event": "after", "repo": "x", "provider": "claude", "five_hour": 51, "five_hour_resets_at": "R", "tokens": 90_000}]
    claude, codex = limits.summarize(rows, "claude"), limits.summarize(rows, "codex")
    assert claude["rows"] == 1 and claude["five_hour_points"] == 1 and claude["tokens"] == 90_000
    assert codex["rows"] == 1 and codex["five_hour_points"] == 4 and codex["tokens"] == 50_000
    assert limits.summarize([{"event": "start"}], "codex") is None      # no Codex rows -> no Codex line


def test_refit_uses_every_pair_on_file_not_just_the_batch():
    rows = _pair("a", 1, 3, 100_000) + [{"event": "start"}] + _pair("b", 3, 4, 100_000)
    r = limits.refit(rows)
    assert r["pairs"] == 2 and r["five_hour_pairs"] == 2
    assert abs(r["five_hour_points_per_100k"] - 1.5) < 1e-9
    assert r["tokens_measured"] == 200_000


def test_refit_on_the_seed_file_reproduces_the_readme_constants(tmp_path):
    """The 24 habit_notes rows: rows 6-11 (fresh window) gave ~1.1 five-hour points per 100k tokens;
    the pair that straddled the 20:40 reset and the 42-minute rebase row are in the file too, so the
    all-pairs figure sits a little above that. The README's ~1.1 was over the clean rows only."""
    from pathlib import Path
    tracked = Path(__file__).resolve().parent / "fixtures" / "calibration" / "usage-burn-seed.jsonl"
    rows = [json.loads(l) for l in tracked.read_text(encoding="utf-8").splitlines() if l.strip()]
    r = limits.refit(rows)
    assert r["pairs"] == 12
    assert r["five_hour_pairs"] == 11                      # loom-os crossed the reset
    assert 0.9 <= r["five_hour_points_per_100k"] <= 1.6
    assert 0.05 <= r["seven_day_points_per_100k"] <= 0.3


def test_the_batch_total_stops_at_the_last_mark_not_a_later_sample():
    rows = [{"event": "start", "five_hour": 14, "five_hour_resets_at": "R", "utc": "T0"}]
    rows += [{"event": "before", "repo": "a", "five_hour": 15, "five_hour_resets_at": "R", "utc": "T1"},
             {"event": "after", "repo": "a", "five_hour": 20, "five_hour_resets_at": "R", "tokens": 100_000, "utc": "T2"},
             {"event": "sample", "five_hour": 29, "five_hour_resets_at": "R", "utc": "T3"}]
    s = limits.summarize(rows)
    assert s["bar_moved"] == 6 and s["five_hour_points"] == 5


# --------------------------------------------------------------------------- batch pairing

def _mark(event, repo="", five=None, week=None, tokens=0, utc="", reset="R", **extra):
    row = {"event": event, "repo": repo, "five_hour": five, "five_hour_resets_at": reset,
           "seven_day": week, "seven_day_resets_at": "W", "tokens": tokens, "utc": utc}
    row.update(extra)
    return row


def _one_before_five_afters():
    """The 2026-09-22 evening batch as it sits in state/limits/limits.jsonl: one `before`, five
    concurrent workers (three Sonnet, two Opus) each writing an `after` on the same repo."""
    return [
        _mark("start", five=29, week=69, utc="2026-09-22T21:20:56Z"),
        _mark("before", "project_lanterns", 29, 69, utc="2026-09-22T21:21:44Z"),
        _mark("after", "project_lanterns", 46, 71, 84_775, "2026-09-22T21:28:16Z"),
        _mark("after", "project_lanterns", 64, 73, 148_246, "2026-09-22T21:41:55Z"),
        _mark("after", "project_lanterns", 65, 73, 147_302, "2026-09-22T21:42:48Z"),
        _mark("after", "project_lanterns", 84, 76, 146_828, "2026-09-22T22:00:08Z"),
        _mark("after", "project_lanterns", 87, 76, 150_513, "2026-09-22T22:05:47Z"),
    ]


def test_one_before_and_five_afters_is_a_five_row_batch_not_one_row():
    s = limits.summarize(_one_before_five_afters())
    assert s["rows"] == 5 and s["running"] == 0
    assert s["tokens"] == 677_664
    assert s["five_hour_points"] == 58 and s["seven_day_points"] == 7      # 29 -> 87, 69 -> 76
    assert abs(s["points_per_100k"] - 58 / 6.77664) < 1e-9                 # ~8.6, the hand figure
    assert s["parallel"] == 5
    assert "5 ran in parallel" in limits.batch_text(s)


def test_the_clean_first_pair_of_a_concurrent_batch_never_teaches_the_serial_refit():
    """Its own pair read 29 -> 46 (+17 for 85k tokens, ~20 per 100k) only because four other
    workers were burning beside it. The refit counts the batch once, in its own lane."""
    r = limits.refit(_one_before_five_afters())
    assert r["five_hour_pairs"] == 0 and r["five_hour_points_per_100k"] is None
    assert r["batches"] == 1 and r["batch_tokens_measured"] == 677_664
    assert abs(r["batch_five_hour_points_per_100k"] - 58 / 6.77664) < 1e-9
    assert abs(r["batch_seven_day_points_per_100k"] - 7 / 6.77664) < 1e-9


def test_several_befores_on_one_repo_pair_first_in_first_out_instead_of_overwriting():
    """2026-09-22 afternoon: five `habit_notes` befores; the old dict kept only the last one."""
    rows = [_mark("start", five=10, week=58, utc="T00")]
    rows += [_mark("before", "habit_notes", 11, 59, utc=f"T0{i}") for i in range(1, 4)]
    rows += [_mark("after", "habit_notes", 20 + i, 60, 100_000, f"T1{i}") for i in range(3)]
    pairs, pending, batched = limits._match(rows)
    assert len(pairs) == 3 and not pending and not batched
    assert [b["utc"] for b, _ in pairs] == ["T01", "T02", "T03"]
    s = limits.summarize(rows)
    assert s["rows"] == 3 and s["tokens"] == 300_000
    assert s["five_hour_points"] == 12                  # bar 10 -> 22, overlapping rows give way


def test_befores_still_waiting_count_as_running():
    rows = [_mark("start", five=10, utc="T0")]
    rows += [_mark("before", "a", 10, utc="T1"), _mark("before", "a", 10, utc="T2")]
    rows += [_mark("after", "a", 12, tokens=90_000, utc="T3")]
    s = limits.summarize(rows)
    assert s["rows"] == 1 and s["running"] == 1


def test_an_after_with_no_before_joins_the_batch_total():
    """2026-09-23 early: afters for android and habit_notes arrived with no `before`."""
    rows = [_mark("start", five=5, week=20, utc="T0"),
            _mark("before", "a", 5, 20, utc="T1"),
            _mark("after", "a", 9, 21, 100_000, "T2"),
            _mark("after", "android", 12, 21, 100_000, "T3")]
    s = limits.summarize(rows)
    assert s["rows"] == 2 and s["tokens"] == 200_000
    assert s["five_hour_points"] == 7 and s["seven_day_points"] == 1   # the bar, start -> last mark
    assert s["parallel"] == 2


def test_before_batch_is_the_anchor_every_bare_after_pairs_against(tmp_path):
    cfg = _cfg(tmp_path)
    _capture(cfg, "sess", five=30, week=40)
    start = limits.mark(cfg, "start", session_id="sess", now="2026-09-23T01:00:00Z")
    anchor = limits.mark(cfg, "before", note="3 workers", session_id="sess", batch=True,
                         now="2026-09-23T01:00:01Z")
    assert anchor["batch"] is True and "batch" not in start
    for i, repo in enumerate(("x", "y", "z")):
        _capture(cfg, "sess", five=40 + 5 * i, week=41)
        limits.mark(cfg, "after", repo=repo, tokens=100_000, session_id="sess",
                    now=f"2026-09-23T01:1{i}:00Z")
    rows = limits.read_rows(cfg)
    _, pending, batched = limits._match(rows)
    assert not pending and [b is not None and b.get("batch") for b, _ in batched] == [True] * 3
    s = limits.summarize(rows)
    assert s["rows"] == 3 and s["running"] == 0 and s["tokens"] == 300_000
    assert s["five_hour_points"] == 20 and s["seven_day_points"] == 1


def test_batch_flag_only_goes_on_a_before(tmp_path):
    cfg = _cfg(tmp_path)
    try:
        limits.mark(cfg, "after", batch=True, session_id="")
    except ValueError:
        assert not limits.limits_file(cfg).exists()
        return
    raise AssertionError("--batch on an after must not write a row")


def test_marks_only_ever_append_so_a_running_batch_keeps_its_rows(tmp_path):
    """A batch is being marked while this code changes under it: every mark appends one line and
    leaves the earlier lines byte-for-byte as they were."""
    cfg = _cfg(tmp_path)
    limits.mark(cfg, "start", session_id="", now="2026-09-23T01:00:00Z")
    limits.mark(cfg, "before", repo="a", session_id="", now="2026-09-23T01:00:01Z")
    first = limits.limits_file(cfg).read_bytes()
    limits.mark(cfg, "after", repo="a", tokens=5, session_id="", now="2026-09-23T01:05:00Z")
    after = limits.limits_file(cfg).read_bytes()
    assert after.startswith(first) and after.count(b"\n") == first.count(b"\n") + 1


def test_a_batch_still_running_or_with_no_percentages_never_enters_the_refit():
    running = _one_before_five_afters()[:-1] + [_mark("before", "late", 84, 76, utc="2026-09-22T22:01:00Z")]
    assert limits.refit(running)["batches"] == 0
    blind = [_mark("start", utc="T0"), _mark("before", "a", utc="T1"),
             _mark("after", "a", tokens=100_000, utc="T2"), _mark("after", "a", tokens=90_000, utc="T3")]
    r = limits.refit(blind)
    assert r["batches"] == 0 and r["batch_five_hour_points_per_100k"] is None
    assert limits.summarize(blind)["tokens"] == 190_000


def test_a_solo_pair_after_a_finished_batch_is_serial_and_does_not_stretch_the_batch():
    """2026-09-26: a reviewer pair marked two days after the 2026-09-24 batch with no new `start`.
    The batch must still count once (its own window), and the pair must count as a serial row."""
    rows = _one_before_five_afters() + [
        _mark("before", "loom-os", 17, 34, utc="2026-09-26T17:55:11Z", reset="R2"),
        _mark("after", "loom-os", 18, 34, 157_966, "2026-09-26T17:58:08Z", reset="R2"),
    ]
    r = limits.refit(rows)
    assert r["batches"] == 1 and r["batch_tokens_measured"] == 677_664
    assert abs(r["batch_five_hour_points_per_100k"] - 58 / 6.77664) < 1e-9
    assert r["five_hour_pairs"] == 1 and r["tokens_measured"] == 157_966
    assert abs(r["five_hour_points_per_100k"] - 1 / 1.57966) < 1e-9


def test_a_lone_bare_after_does_not_claim_it_ran_in_parallel():
    rows = [_mark("start", five=5, utc="T0"), _mark("after", "Apps", 8, tokens=100_000, utc="T1")]
    text = limits.batch_text(limits.summarize(rows))
    assert "ran in parallel" not in text and text.endswith("points are the batch total")
