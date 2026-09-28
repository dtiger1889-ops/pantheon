"""The arithmetic behind every number on the HUD."""
from datetime import datetime, timedelta, timezone

from pantheon.hud import calc


def _samples(pairs):
    """(minutes_ago, percent) -> the (timestamp, percent) shape collect() stores."""
    base = datetime(2026, 9, 1, 23, 50, tzinfo=timezone.utc)
    return [((base - timedelta(minutes=m)).isoformat().replace("+00:00", "Z"), p) for m, p in pairs]


# ---- burn rate ------------------------------------------------------------

def test_burn_rate_scales_thirty_minutes_to_an_hour():
    # 4 points up from 30% to 42% over 30 minutes == 24 percent per hour.
    burn = calc.burn_pct_per_hour(_samples([(30, 30.0), (20, 34.0), (10, 38.0), (0, 42.0)]))
    assert burn is not None and abs(burn - 24.0) < 0.01


def test_burn_rate_ignores_readings_older_than_the_window():
    burn = calc.burn_pct_per_hour(_samples([(600, 1.0), (20, 40.0), (0, 42.0)]))
    assert burn is not None and abs(burn - 6.0) < 0.01


def test_burn_rate_unknown_without_two_usable_readings():
    assert calc.burn_pct_per_hour([]) is None
    assert calc.burn_pct_per_hour(_samples([(0, 42.0)])) is None
    assert calc.burn_pct_per_hour([("not a time", 5.0), ("also not", 9.0)]) is None
    assert calc.burn_pct_per_hour(_samples([(0, 42.0), (0, 44.0)])) is None


def test_burn_rate_starts_over_after_a_window_reset():
    # The 5-hour window reset between the second and third reading; only the rising tail counts.
    burn = calc.burn_pct_per_hour(_samples([(40, 88.0), (30, 95.0), (20, 2.0), (0, 6.0)]))
    assert burn is not None and abs(burn - 12.0) < 0.01


def test_burn_rate_is_unknown_when_the_newest_reading_is_stale():
    # No live session for hours: the last readings are from the morning. With the clock given,
    # a newest reading older than the window is no reading at all -- the phone showed
    # `burn 9%/h` beside `5h ?`. Without a clock it stays a pure
    # measurement over the samples, which the other tests rely on.
    clock = datetime(2026, 9, 1, 23, 50, tzinfo=timezone.utc).timestamp()
    stale = _samples([(240, 30.0), (230, 34.0)])
    assert calc.burn_pct_per_hour(stale) is not None
    assert calc.burn_pct_per_hour(stale, now=clock) is None
    assert calc.burn_pct_per_hour(_samples([(20, 30.0), (10, 34.0)]), now=clock) is not None


def test_burn_rate_accepts_epoch_seconds_too():
    burn = calc.burn_pct_per_hour([(1788300000, 10.0), (1788301800, 15.0)])
    assert burn is not None and abs(burn - 10.0) < 0.01


# ---- time to limit --------------------------------------------------------

def test_time_to_limit_and_its_wording():
    now = datetime(2026, 9, 1, 23, 50, tzinfo=timezone.utc)
    reset = "2026-09-02T06:00:00.000Z"          # over 6 hours away
    seconds = calc.time_to_limit(42.0, 18.0, reset, now)
    assert seconds is not None and abs(seconds - 11600.0) < 1.0
    assert calc.format_time_to_limit(seconds, reset, now) == "~3h13m"


def test_says_window_when_the_reset_comes_first():
    now = datetime(2026, 9, 1, 23, 50, tzinfo=timezone.utc)
    reset = "2026-09-02T01:00:00.000Z"          # 70 minutes away
    seconds = calc.time_to_limit(42.0, 18.0, reset, now)
    assert calc.format_time_to_limit(seconds, reset, now) == ">window"


def test_time_to_limit_unknown_rather_than_guessed():
    assert calc.time_to_limit(None, 8.0) is None
    assert calc.time_to_limit(42.0, None) is None
    assert calc.time_to_limit(42.0, 0.0) is None
    assert calc.format_time_to_limit(None) == "?"
    assert calc.time_to_limit(100.0, 8.0) == 0.0


def test_format_duration():
    assert calc.format_duration(None) == "?"
    assert calc.format_duration(840) == "~14m"
    assert calc.format_duration(11400) == "~3h10m"
    assert calc.format_duration(-5) == "~0m"


# ---- context bands --------------------------------------------------------

def test_context_bands_are_named_in_words():
    assert calc.context_band(0) == "ok"
    assert calc.context_band(59.9) == "ok"
    assert calc.context_band(60) == "getting full"
    assert calc.context_band(84.9) == "getting full"
    assert calc.context_band(85) == "compaction soon"
    assert calc.context_band(100) == "compaction soon"
    assert calc.context_band(None) == "unknown"


# ---- attention lines ------------------------------------------------------

def test_no_attention_lines_when_nothing_needs_the_user():
    quiet = {"provider": "claude", "five_hour_pct": 42.0, "seven_day_pct": 61.0}
    assert calc.attention_lines(quiet, None, 37.0) == []


def test_attention_lines_name_the_provider_and_the_number():
    claude = {"provider": "claude", "five_hour_pct": 93.0, "seven_day_pct": 88.0}
    codex = {"provider": "codex", "five_hour_pct": 12.0, "seven_day_pct": 5.0}
    lines = calc.attention_lines(claude, codex, 91.0)
    assert len(lines) == 3
    assert lines[0] == "Claude: 88% of this week's budget is used"
    assert lines[1] == "Claude: 93% of this 5-hour budget is used"
    assert "memory" in lines[2] and "91%" in lines[2]
    # Plain words only -- no bare field names on the user's screen.
    assert not any("ctx" in line or "pct" in line for line in lines)


def test_attention_lines_survive_missing_numbers():
    assert calc.attention_lines(None, None, None) == []
    assert calc.attention_lines({"provider": "claude"}, {"provider": "codex"}, None) == []


# ---- governor line ----------------------------------


def test_governor_line_silent_when_nothing_is_winding_down_or_parked():
    assert calc.governor_line(None) is None
    assert calc.governor_line({"winding_down": 0, "parked": 0}) is None


def test_governor_line_names_the_counts_and_the_five_hour_reset():
    summary = {"winding_down": 2, "parked": 1, "five_hour_resets_at": "2026-09-03T01:12:00.000Z",
              "dry_run": False}
    line = calc.governor_line(summary)
    assert line == (f"limit guard: 2 winding down · 1 parked · "
                    f"5-hour window resets {calc._fmt_clock_local(summary['five_hour_resets_at'])}")


def test_governor_line_marks_dry_run_and_only_counts_wind_downs():
    summary = {"winding_down": 2, "parked": 0, "five_hour_resets_at": "2026-09-03T01:12:00.000Z", "dry_run": True}
    line = calc.governor_line(summary)
    assert line.startswith("[dry run] limit guard: would wind down 2")
    assert "parked" not in line


def test_attention_lines_appends_the_governor_line_last():
    lines = calc.attention_lines(None, None, None, governor={"winding_down": 1, "parked": 0, "dry_run": False})
    assert len(lines) == 1 and lines[0].startswith("limit guard:")
    quiet = {"provider": "claude", "five_hour_pct": 42.0}
    lines2 = calc.attention_lines(quiet, None, None, governor={"winding_down": 0, "parked": 0})
    assert lines2 == []  # both the budget and the governor are silent


def test_governor_line_honours_the_twelve_hour_clock_setting():
    summary = {"winding_down": 1, "parked": 0, "five_hour_resets_at": "2026-09-03T01:12:00.000Z",
              "dry_run": False}
    line_24h = calc.governor_line(summary)
    line_12h = calc.governor_line(summary, clock="12h")
    assert line_24h != line_12h
    assert calc._fmt_clock_local(summary["five_hour_resets_at"], "12h") in line_12h
