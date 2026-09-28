"""The 2026-09-26 usage-truth fixes: the Claude card said `5h ?  week ?` while Claude Code was
running, and the Codex card never had a burn rate or a trend. Shapes below are copied from the
real captures in `state/statusline/` that day (Claude Code 2.1.282/2.1.283)."""
import json
import os
from pathlib import Path

import pytest

from pantheon import config
from pantheon.hud import calc, capture, sources

NOW = 1790467200.0          # 2026-09-27T00:00:00Z
FIVE_RESET = 1790472600     # 2026-09-27T01:30:00Z, the live window that evening
WEEK_RESET = 1790823600     # 2026-10-01T03:00:00Z


@pytest.fixture()
def cfg(tmp_path):
    return config.Config(state_dir=str(tmp_path / "state"), codex_home=str(tmp_path / "codex"))


def _capture(folder: Path, sid: str, written: float, five=None, week=None, ctx=None,
             five_reset=FIVE_RESET, week_reset=WEEK_RESET):
    limits = {}
    if five is not None:
        limits["five_hour"] = {"used_percentage": five, "resets_at": five_reset}
    if week is not None:
        limits["seven_day"] = {"used_percentage": week, "resets_at": week_reset}
    data = {"session_id": sid, "rate_limits": limits, "model": {"display_name": "Fable 5.1"},
            "cost": {"total_cost_usd": 27.18}}
    if ctx is not None:
        data["context_window"] = {"used_percentage": ctx}
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{sid}.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    os.utime(path, (written, written))
    return data


# ---- root cause: an idle session re-renders without its 5-hour block ------------------------

def test_an_idle_session_without_five_hour_does_not_hide_another_sessions_reading(tmp_path):
    # garden_plans (idle since the day before) re-rendered at a reset time with only a stale
    # weekly 29%; loom-os had replied 5 minutes earlier with 61% / 44%.
    folder = tmp_path / "statusline"
    _capture(folder, "idle", NOW - 30, week=29, ctx=32)
    _capture(folder, "busy", NOW - 300, five=61, week=44, ctx=66)
    usage = sources.claude_from_statusline(folder, now=NOW)
    assert usage.five_hour_pct == 61.0 and usage.seven_day_pct == 44.0
    assert usage.five_hour_as_of == sources._iso(NOW - 300)
    assert usage.context_pct == 32.0          # the newest session's own, still fresh


def test_no_session_newer_than_ten_minutes_still_shows_the_last_reading(tmp_path):
    # The 2026-09-26 20:07 local picture: the only capture with numbers was 14 minutes old
    # (an orchestrator waiting on its workers re-renders nothing), so everything went `?`.
    folder = tmp_path / "statusline"
    _capture(folder, "busy", NOW - 14 * 60, five=56, week=43, ctx=66)
    usage = sources.claude_from_statusline(folder, now=NOW)
    assert usage.source == "statusline"
    assert usage.five_hour_pct == 56.0 and usage.seven_day_pct == 43.0
    assert calc.stale_age_words(usage.five_hour_as_of, NOW) == "14m ago"
    assert usage.context_pct is None


def test_a_reading_from_before_the_reset_is_dropped_not_aged(tmp_path):
    folder = tmp_path / "statusline"
    _capture(folder, "old", NOW - 7200, five=97, week=44, five_reset=int(NOW - 60))
    usage = sources.claude_from_statusline(folder, now=NOW)
    assert usage.five_hour_pct is None and usage.seven_day_pct == 44.0


def test_inside_one_window_the_higher_percent_wins_even_from_an_older_capture(tmp_path):
    folder = tmp_path / "statusline"
    _capture(folder, "stale-weekly", NOW - 10, week=29)
    _capture(folder, "live", NOW - 120, five=61, week=44)
    assert sources.claude_from_statusline(folder, now=NOW).seven_day_pct == 44.0


# ---- the Claude Code status line itself -------------------------------------------------------

def test_the_status_line_prints_the_account_numbers_not_the_idle_sessions_own(tmp_path):
    folder = tmp_path / "statusline"
    own = _capture(folder, "idle", NOW - 5, week=29, ctx=32)
    _capture(folder, "busy", NOW - 60, five=75, week=46)
    account = sources.claude_from_statusline(folder, now=NOW)
    line = sources.statusline_line(own, account=account, now=NOW)
    assert line == "5h 75% · wk 46% · ctx 32% · $27.18 · Fable 5.1"


def test_the_status_line_ages_an_old_reading_and_never_prints_a_question_mark(tmp_path):
    folder = tmp_path / "statusline"
    own = _capture(folder, "busy", NOW - 20 * 60, five=56, week=43)
    account = sources.claude_from_statusline(folder, now=NOW)
    line = sources.statusline_line(own, account=account, now=NOW)
    assert line.startswith("5h 56% 20m ago · wk 43% 20m ago")
    assert "?" not in line


def test_capture_prints_the_account_reading(cfg, monkeypatch):
    monkeypatch.setattr(sources, "_now_epoch", lambda: NOW)
    folder = Path(cfg.statusline_dir)
    _capture(folder, "busy", NOW - 60, five=75, week=46)
    idle = {"session_id": "idle", "rate_limits": {"seven_day": {"used_percentage": 29,
                                                                "resets_at": WEEK_RESET}}}
    printed = capture.capture(json.dumps(idle), cfg)
    assert printed.startswith("5h 75% · wk 46%")


# ---- Codex: a series of its own, so it gets a burn rate and a trend -------------------------

def test_history_keeps_a_codex_series_stamped_with_its_log_time():
    h = sources._history({}, 40.0, "2026-09-27T00:00:00Z", 70.0, "2026-09-26T23:50:00Z")
    h = sources._history({"history": h}, 41.0, "2026-09-27T00:01:00Z", 74.0, "2026-09-27T00:00:00Z")
    assert h["codex_pct"] == [70.0, 74.0]
    assert calc.burn_pct_per_hour([tuple(s) for s in h["codex_samples"]]) == pytest.approx(24.0)


def test_the_same_reading_read_twice_is_one_sample_not_a_flat_line():
    h = sources._history({}, 56.0, "2026-09-27T00:00:00Z")
    h = sources._history({"history": h}, 56.0, "2026-09-27T00:00:00Z")
    assert h["claude_pct"] == [56.0]


# ---- who writes hud.json when the budget window is closed -----------------------------------

def _stale_picture(cfg, fetched_at="2026-09-26T23:00:00Z"):
    picture = {"fetched_at": fetched_at, "slow_fetched_at": fetched_at,
               "claude": {"provider": "claude", "cost_today_usd": 94.82, "tokens_today": 5},
               "codex": {"provider": "codex", "cost_today_usd": 23.2}, "history": {}}
    Path(cfg.hud_file).parent.mkdir(parents=True, exist_ok=True)
    Path(cfg.hud_file).write_text(json.dumps(picture), encoding="utf-8")


def test_a_fresh_file_is_left_alone(cfg):
    _stale_picture(cfg, fetched_at=sources._iso(NOW - 30))
    assert sources.refresh_if_stale(cfg, now=NOW) is None


def test_an_unowned_file_gets_a_quick_pass_with_no_subprocess(cfg, monkeypatch):
    monkeypatch.setattr(sources, "_now_epoch", lambda: NOW)
    monkeypatch.setattr(sources, "_today_local", lambda: "2026-09-26")
    monkeypatch.setattr(sources, "run_json", lambda *a, **k: pytest.fail("quick pass ran ccusage"))
    _stale_picture(cfg, fetched_at="2026-09-26T23:00:00Z")
    _capture(Path(cfg.statusline_dir), "busy", NOW - 60, five=61, week=44)
    picture = sources.refresh_if_stale(cfg, now=NOW)
    assert picture is not None and picture["writer"] == "quick pass"
    assert picture["claude"]["five_hour_pct"] == 61.0
    # today's cost carried from the last full pass while it is still the same local day
    assert picture["claude"]["cost_today_usd"] == 94.82
    assert sources.read_hud(cfg)["writer"] == "quick pass"
    assert not Path(cfg.hud_file).with_name(sources.QUICK_LOCK).exists()


def test_a_reader_already_mid_pass_blocks_a_second_one(cfg):
    _stale_picture(cfg)
    lock = Path(cfg.hud_file).with_name(sources.QUICK_LOCK)
    lock.write_text("", encoding="utf-8")
    assert sources.refresh_if_stale(cfg) is None


def test_age_words_are_plain():
    assert calc.age_words(30) == "just now"
    assert calc.age_words(14 * 60) == "14m ago"
    assert calc.age_words(3 * 3600 + 5) == "3h ago"
    assert calc.age_words(3 * 86400) == "3d ago"
    assert calc.stale_age_words(None) is None


# ---- the cards: no `?` wall, no `clear`, a one-line NEEDS YOU when quiet ---------------------

def _mount(widget_factory, size=(120, 12)):
    import asyncio

    from pantheon.hud import app as hud_app

    async def _go():
        class _Host(hud_app.App):
            def compose(self):
                yield widget_factory()

        app = _Host()
        async with app.run_test(size=size):
            w = app.query_one("#w")
            texts = {}
            for child in w.query("Static"):
                if child.id:
                    content = child.content
                    texts[child.id] = getattr(content, "plain", None) or str(content)
            return w, texts, w.outer_size.height, str(w.border_subtitle or ""), str(w.border_title or "")

    return asyncio.run(_go())


def test_a_card_with_no_reading_says_so_once_and_shows_no_question_mark():
    from pantheon.hud import app as hud_app
    usage = {"provider": "claude", "source": "unknown", "note": "no live session",
             "cost_today_usd": 94.82, "burn_cost_per_hour": None}
    _w, texts, _h, subtitle, _t = _mount(lambda: hud_app.ProviderCard("claude", usage, id="w"))
    shown = "\n".join(texts.values())
    assert "?" not in shown and "no reading yet" in shown
    assert shown.count("no reading yet") == 1
    assert "burn" not in shown and "runs out" not in shown
    assert "today $94.82" in shown
    assert subtitle == ""                 # no `clear`


def test_an_old_card_reading_carries_its_age_in_the_corner():
    from datetime import datetime, timedelta, timezone

    from pantheon.hud import app as hud_app
    old = (datetime.now(timezone.utc) - timedelta(minutes=14, seconds=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    usage = {"provider": "claude", "source": "statusline", "five_hour_pct": 74.0,
             "seven_day_pct": 44.0, "five_hour_as_of": old}
    _w, texts, _h, subtitle, _t = _mount(lambda: hud_app.ProviderCard("claude", usage, id="w"))
    assert subtitle == "running low · last reading 14m ago"
    assert "74%" in texts["w-5h"]


def test_needs_you_is_one_frameless_line_when_quiet_and_grows_when_not():
    from pantheon.hud import app as hud_app
    _w, texts, height, _s, title = _mount(lambda: hud_app.NeedsYouCard([], [], id="w"))
    assert height == 1 and title == "" and texts["w-body"] == "nothing needs you"
    _w, texts, height, _s, title = _mount(
        lambda: hud_app.NeedsYouCard([], ["Claude: 94% of this 5-hour budget is used"], id="w"))
    assert title == "NEEDS YOU" and height >= 5
    assert "94% of this 5-hour budget" in texts["w-body"]
