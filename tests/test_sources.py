"""Reading the three local usage sources. Fixtures carry the real shapes, captured
2026-09-01 from Claude Code's statusline docs, `ccusage` output, and a Codex session log."""
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

from pantheon import config
from pantheon.hud import capture, line, sources

FIXTURES = Path(__file__).parent / "fixtures" / "hud"


def _load(name):
    with open(FIXTURES / name, "r", encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture()
def cfg(tmp_path):
    return config.Config(state_dir=str(tmp_path / "state"), codex_home=str(tmp_path / "codex"))


def _statusline_dir(tmp_path, *names):
    folder = tmp_path / "statusline"
    folder.mkdir(parents=True, exist_ok=True)
    for name in names:
        shutil.copy(FIXTURES / name, folder / name)
    return folder


# ---- 1. statusline --------------------------------------------------------

# The fixture's 5-hour window resets at epoch 1788307200; a window whose
# reset has passed is dropped, so the tests that read its number pin the clock before that.
# (Pinned `now` also makes the freshly copied files count as fresh: a negative age is not old.)
_STATUSLINE_CLOCK = 1788300000.0


def test_statusline_reads_both_windows_and_the_context(tmp_path):
    folder = _statusline_dir(tmp_path, "statusline.json")
    usage = sources.claude_from_statusline(folder, now=_STATUSLINE_CLOCK)
    assert usage.provider == "claude" and usage.source == "statusline"
    assert usage.five_hour_pct == 42.0 and usage.seven_day_pct == 61.0
    assert usage.context_pct == 37.0
    # resets_at is epoch SECONDS in the payload and ISO-8601 UTC on the way out.
    assert usage.five_hour_resets_at.startswith("2026-")


def test_statusline_takes_the_highest_percent_across_live_sessions(tmp_path):
    folder = _statusline_dir(tmp_path, "statusline.json")
    higher = _load("statusline.json")
    higher["session_id"] = "second-session"
    higher["rate_limits"]["five_hour"]["used_percentage"] = 77.0
    (folder / "second-session.json").write_text(json.dumps(higher), encoding="utf-8")
    assert sources.claude_from_statusline(folder, now=_STATUSLINE_CLOCK).five_hour_pct == 77.0


def test_statusline_drops_a_window_whose_reset_has_passed(tmp_path):
    # Same rule as Codex's log: past its reset time the 5-hour number is about nothing; the
    # weekly one is still running and stays.
    folder = _statusline_dir(tmp_path, "statusline.json")
    usage = sources.claude_from_statusline(folder, now=1788307200.0 + 60)
    assert usage.source == "statusline"
    assert usage.five_hour_pct is None and usage.seven_day_pct == 61.0


def test_statusline_tolerates_a_session_with_no_rate_limits(tmp_path):
    folder = _statusline_dir(tmp_path, "statusline_no_limits.json")
    usage = sources.claude_from_statusline(folder)
    assert usage.five_hour_pct is None and usage.seven_day_pct is None
    assert usage.context_pct == 9.0 and usage.note


def test_an_old_capture_keeps_its_numbers_with_their_time_and_drops_the_context(tmp_path):
    # Changed 2026-09-26. A capture
    # older than ten minutes used to be thrown away whole; its numbers are the last known ones
    # (a window's percent only rises), so they stay, stamped with when they were written, and
    # the screens print the age. The context window is one session's and goes.
    import os
    folder = _statusline_dir(tmp_path, "statusline.json")
    written = _STATUSLINE_CLOCK - 3600
    os.utime(folder / "statusline.json", (written, written))
    usage = sources.claude_from_statusline(folder, now=_STATUSLINE_CLOCK)
    assert usage.source == "statusline"
    assert usage.five_hour_pct == 42.0 and usage.seven_day_pct == 61.0
    assert usage.five_hour_as_of == sources._iso(written) == usage.reported_at
    assert usage.context_pct is None
    assert sources.claude_from_statusline(tmp_path / "nope").source == "unknown"


def test_statusline_for_reads_one_sessions_file_by_id(tmp_path):
    """step 4/6: the supervisor toolbar looks up exactly one session's capture, not the
    account-wide scan `claude_from_statusline` does."""
    folder = _statusline_dir(tmp_path, "statusline.json")
    (folder / "0f2b7c1e-4a3d-4f8b-9c11-abcdef012345.json").write_text(
        (FIXTURES / "statusline.json").read_text(encoding="utf-8"), encoding="utf-8"
    )
    data = sources.statusline_for(folder, "0f2b7c1e-4a3d-4f8b-9c11-abcdef012345")
    assert data["model"]["display_name"] == "Opus 5"
    assert sources.statusline_for(folder, "no-such-session") == {}
    assert sources.statusline_for(folder, None) == {}
    assert sources.statusline_for(tmp_path / "nope", "any") == {}


def test_statusline_line_is_short_and_marks_what_is_missing():
    assert sources.statusline_line(_load("statusline.json")) == (
        "5h 42% · wk 61% · ctx 37% · $1.20 · Opus 5"
    )
    # Changed 2026-09-26: a number nobody reported is left out, not printed as `?`.
    assert sources.statusline_line({}) == "no usage reading yet"


def test_capture_saves_the_payload_and_prints_the_line(cfg, monkeypatch):
    monkeypatch.setattr(sources, "_now_epoch", lambda: _STATUSLINE_CLOCK)  # before the resets
    payload = _load("statusline.json")
    printed = capture.capture(json.dumps(payload), cfg)
    saved = Path(cfg.statusline_dir) / f"{payload['session_id']}.json"
    assert saved.exists() and json.loads(saved.read_text(encoding="utf-8")) == payload
    assert printed.startswith("5h 42%")


def test_capture_refuses_to_write_outside_its_folder(cfg):
    payload = _load("statusline.json")
    payload["session_id"] = "../../escape"
    capture.capture(json.dumps(payload), cfg)
    assert list(Path(cfg.statusline_dir).glob("*.json")) and not (
        Path(cfg.state_dir).parent / "escape.json"
    ).exists()


# ---- 2. ccusage -----------------------------------------------------------

def test_claude_from_ccusage_has_no_percent_and_says_from_history(cfg):
    usage = sources.claude_from_ccusage(
        cfg, blocks=_load("ccusage_blocks_active.json"), daily=_load("ccusage_daily.json")
    )
    assert usage.source == "ccusage" and usage.note == "from history"
    assert usage.five_hour_pct is None and usage.seven_day_pct is None
    assert abs(usage.burn_cost_per_hour - 20.645116493210462) < 1e-9
    assert usage.five_hour_resets_at == "2026-09-02T01:00:00.000Z"


def test_claude_from_ccusage_takes_todays_cost_only(cfg, monkeypatch):
    monkeypatch.setattr(sources, "_today_local", lambda: "2026-09-01")
    usage = sources.claude_from_ccusage(
        cfg, blocks=_load("ccusage_blocks_active.json"), daily=_load("ccusage_daily.json")
    )
    # Claude's own share of the day, NOT the 90.80 that ccusage reports for both agents.
    assert usage.cost_today_usd == 77.1522 and usage.tokens_today == 116200874
    monkeypatch.setattr(sources, "_today_local", lambda: "2026-09-09")
    later = sources.claude_from_ccusage(
        cfg, blocks=_load("ccusage_blocks_active.json"), daily=_load("ccusage_daily.json")
    )
    assert later.cost_today_usd is None  # yesterday's cost is never shown as today's


def test_a_combined_daily_row_is_refused_rather_than_double_counted(cfg, monkeypatch):
    """Without --by-agent, ccusage adds Claude and Codex together. Better to show `?`."""
    monkeypatch.setattr(sources, "_today_local", lambda: "2026-09-01")
    combined = {"daily": [{"agent": "all", "period": "2026-09-01", "totalCost": 90.8017}]}
    usage = sources.claude_from_ccusage(
        cfg, blocks=_load("ccusage_blocks_active.json"), daily=combined
    )
    assert usage.cost_today_usd is None


def test_daily_rows_are_read_under_either_key_name(cfg):
    """`ccusage daily` says period/totalCost; `ccusage codex` says date/costUSD."""
    period_row = {"period": "2026-09-01", "totalCost": 12.5, "totalTokens": 100}
    date_row = {"date": "2026-09-01", "costUSD": 12.5, "totalTokens": 100}
    for row in (period_row, date_row):
        assert sources._today_row([row], today="2026-09-01") is row
        assert sources._row_cost_and_tokens(row) == (12.5, 100)


def test_codex_from_ccusage_reads_todays_row(cfg, monkeypatch):
    monkeypatch.setattr(sources, "_today_local", lambda: "2026-09-01")
    usage = sources.codex_from_ccusage(cfg, payload=_load("ccusage_codex.json"))
    assert usage.provider == "codex" and usage.cost_today_usd == 13.6495244
    assert usage.tokens_today == 22018758


def test_codex_from_ccusage_says_zero_when_nothing_ran_today(cfg, monkeypatch):
    monkeypatch.setattr(sources, "_today_local", lambda: "2026-09-09")
    usage = sources.codex_from_ccusage(cfg, payload=_load("ccusage_codex.json"))
    assert usage.cost_today_usd == 0.0 and usage.note == "nothing run today"


def test_a_failed_command_never_raises(cfg, monkeypatch):
    monkeypatch.setattr(sources, "run_json", lambda *a, **k: None)
    usage = sources.claude_from_ccusage(cfg)
    assert usage.source == "unknown" and usage.cost_today_usd is None
    assert sources.codex_from_ccusage(cfg).source == "unknown"


def test_run_json_survives_a_missing_program(cfg):
    assert sources.run_json(["definitely-not-a-program-xyz"], timeout=5) is None


# ---- 3. codex session logs ------------------------------------------------

def _codex_home(tmp_path, *names):
    folder = tmp_path / "codex" / "sessions" / "2026" / "09" / "01"
    folder.mkdir(parents=True, exist_ok=True)
    for name in names:
        shutil.copy(FIXTURES / name, folder / name)
    return tmp_path / "codex"


# The fixture's 5-hour window resets at epoch 1788304273; the tests
# pin the clock so they read the same way after that moment has passed for real.
_BEFORE_RESET = 1788300000.0
_AFTER_RESET = 1788304273.0 + 60


def test_codex_rate_limits_reads_the_newest_reading(tmp_path):
    home = _codex_home(tmp_path, "codex_session_with_limits.jsonl")
    usage = sources.codex_rate_limits(home, now=_BEFORE_RESET)
    assert usage.source == "codex-log"
    assert usage.five_hour_pct == 81.0 and usage.seven_day_pct == 29.0
    assert usage.five_hour_resets_at and usage.seven_day_resets_at
    # The screen says `as of HH:MM` from the log line's own time, not from when Pantheon read it.
    assert usage.reported_at and usage.reported_at.startswith("2026-09-01T")


def test_codex_rate_limits_drops_a_window_whose_reset_has_passed(tmp_path):
    # The user's phone, 2026-09-02 17:00: `Codex 5h 50%` from a window that had reset nine hours
    # earlier. Past its reset time the 5-hour number goes; the weekly one, still running, stays.
    home = _codex_home(tmp_path, "codex_session_with_limits.jsonl")
    usage = sources.codex_rate_limits(home, now=_AFTER_RESET)
    assert usage.source == "codex-log" and usage.reported_at
    assert usage.five_hour_pct is None and usage.five_hour_resets_at is None
    assert usage.seven_day_pct == 29.0


def test_codex_rate_limits_says_unknown_when_the_log_has_none(tmp_path):
    home = _codex_home(tmp_path, "codex_session_no_limits.jsonl")
    usage = sources.codex_rate_limits(home)
    assert usage.source == "unknown" and usage.five_hour_pct is None
    assert usage.note  # a sentence, not a blank


def test_codex_rate_limits_with_no_logs_at_all(tmp_path):
    usage = sources.codex_rate_limits(tmp_path / "nothing-here")
    assert usage.source == "unknown" and usage.five_hour_pct is None


# ---- merge, write, read ---------------------------------------------------

def test_collect_merges_live_percentages_with_history_cost(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(sources, "_today_local", lambda: "2026-09-01")
    folder = Path(cfg.statusline_dir)
    folder.mkdir(parents=True, exist_ok=True)
    shutil.copy(FIXTURES / "statusline.json", folder / "statusline.json")
    _codex_home(tmp_path, "codex_session_with_limits.jsonl")

    calls = {}

    def fake_run_json(argv, env=None, timeout=60):
        calls[argv[2]] = True
        return {
            "blocks": _load("ccusage_blocks_active.json"),
            "daily": _load("ccusage_daily.json"),
            "codex": _load("ccusage_codex.json"),
        }[argv[2]]

    monkeypatch.setattr(sources, "run_json", fake_run_json)
    monkeypatch.setattr(sources, "_now_epoch", lambda: _STATUSLINE_CLOCK)  # before the fixtures' resets
    picture = sources.collect(cfg)

    # `slow_fetched_at` / `writer` added 2026-09-26.
    assert set(picture) == {"fetched_at", "slow_fetched_at", "writer", "claude", "codex", "history"}
    assert picture["writer"] == "collector"
    # Percent from the live session, cost and burn from the logs -- the point of the merge.
    assert picture["claude"]["five_hour_pct"] == 42.0
    assert picture["claude"]["cost_today_usd"] == 77.1522
    assert picture["claude"]["source"] == "statusline"
    assert picture["codex"]["five_hour_pct"] == 81.0
    assert picture["codex"]["cost_today_usd"] == 13.6495244
    assert picture["history"]["claude_pct"] == [42.0]
    assert Path(cfg.hud_file).exists()
    assert sources.read_hud(cfg)["claude"]["five_hour_pct"] == 42.0


def test_collect_falls_back_to_history_when_nothing_is_live(cfg, monkeypatch):
    monkeypatch.setattr(sources, "_today_local", lambda: "2026-09-01")

    def fake_run_json(argv, env=None, timeout=60):
        return {
            "blocks": _load("ccusage_blocks_active.json"),
            "daily": _load("ccusage_daily.json"),
            "codex": _load("ccusage_codex.json"),
        }[argv[2]]

    monkeypatch.setattr(sources, "run_json", fake_run_json)
    picture = sources.collect(cfg)
    assert picture["claude"]["source"] == "ccusage"
    assert picture["claude"]["note"] == "from history"
    assert picture["claude"]["five_hour_pct"] is None
    assert picture["history"]["claude_pct"] == []


def test_history_keeps_the_last_thirty_readings():
    previous = {"history": {"claude_samples": [[f"2026-09-01T00:{i:02d}:00Z", i] for i in range(40)]}}
    history = sources._history(previous, 99.0, "2026-09-01T01:00:00Z")
    assert len(history["claude_pct"]) == 30 and history["claude_pct"][-1] == 99.0


def test_write_atomic_leaves_no_temp_files(tmp_path):
    target = tmp_path / "deep" / "hud.json"
    sources.write_atomic(target, '{"ok": true}')
    assert json.loads(target.read_text(encoding="utf-8")) == {"ok": True}
    assert list(target.parent.iterdir()) == [target]


def test_read_hud_survives_a_corrupt_file(cfg):
    Path(cfg.hud_file).parent.mkdir(parents=True, exist_ok=True)
    Path(cfg.hud_file).write_text("{not json", encoding="utf-8")
    assert sources.read_hud(cfg) == {}


# ---- the one-line tmux HUD ------------------------------------------------

def test_hud_line_is_short_and_readable():
    picture = _load("hud.json")
    now = datetime(2026, 9, 1, 23, 51, tzinfo=timezone.utc)
    text = line.render(picture, "unicode", now=now)
    assert len(text) <= line.MAX_CHARS
    assert text.startswith("C 5h 42%") and "wk 61%" in text and "X today $13.65" in text


def test_hud_line_ascii_mode_has_no_unicode():
    picture = _load("hud.json")
    now = datetime(2026, 9, 1, 23, 51, tzinfo=timezone.utc)
    text = line.render(picture, "ascii", now=now)
    assert all(ord(c) < 128 for c in text)


def test_hud_line_says_how_old_rather_than_showing_old_numbers():
    # Changed 2026-09-26: plain words instead of `C ? . X ? . stale`.
    picture = _load("hud.json")
    now = datetime(2026, 9, 2, 1, 0, tzinfo=timezone.utc)
    assert line.render(picture, "ascii", now=now) == "usage not updated for 70m"
    assert line.render({}, "ascii") == "usage: no reading yet"


def test_hud_line_says_no_reading_once_instead_of_question_marks():
    picture = {"fetched_at": "2026-09-01T23:50:00.000Z", "claude": {}, "codex": {}}
    now = datetime(2026, 9, 1, 23, 51, tzinfo=timezone.utc)
    text = line.render(picture, "ascii", now=now)
    assert text.startswith("C no reading") and "?" not in text


def test_hud_line_ages_an_old_claude_reading():
    picture = _load("hud.json")
    picture["claude"]["five_hour_as_of"] = "2026-09-01T23:37:00Z"
    now = datetime(2026, 9, 1, 23, 51, tzinfo=timezone.utc)
    assert line.render(picture, "ascii", now=now).startswith("C 5h 42% 14m ago . wk 61%")
