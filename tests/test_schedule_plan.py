"""build B, the pure half: `parse_at` and the burned-window warning (acceptance 1 and 2)."""
from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timedelta, timezone

import pytest

from pantheon import config as config_mod
from pantheon.schedule import plan

LOCAL = timezone(timedelta(hours=-4))           # a fixed offset so the test never depends on the box
NOW = datetime(2026, 9, 26, 20, 0, tzinfo=LOCAL)


def _cfg(tmp_path):
    return dataclasses.replace(config_mod.Config(), state_dir=str(tmp_path / "state"))


def _iso(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _hud(pct, resets_at: datetime, fetched_at: datetime = NOW, provider="claude", weekly=None):
    picture = {
        "fetched_at": _iso(fetched_at),
        "five_hour_pct": pct,
        "five_hour_resets_at": _iso(resets_at),
        "seven_day_pct": weekly,
        "seven_day_resets_at": _iso(NOW + timedelta(days=3)) if weekly is not None else None,
    }
    return {"fetched_at": _iso(fetched_at), provider: picture}


# ---------------------------------------------------------------- acceptance 1


def test_parse_at_rolls_a_past_time_to_tomorrow():
    assert plan.parse_at("06:00", now=NOW) == datetime(2026, 9, 27, 6, 0, tzinfo=LOCAL)


def test_parse_at_keeps_a_later_time_today():
    assert plan.parse_at("23:00", now=NOW) == datetime(2026, 9, 26, 23, 0, tzinfo=LOCAL)


def test_parse_at_the_current_minute_means_tomorrow():
    assert plan.parse_at("20:00", now=NOW).day == 27


@pytest.mark.parametrize("text", ["6", "25:00", "06:61", "six", ""])
def test_parse_at_refuses_what_is_not_a_time(text):
    with pytest.raises(ValueError):
        plan.parse_at(text, now=NOW)


# ---------------------------------------------------------------- acceptance 2


def test_warns_when_the_start_lands_in_a_nearly_spent_window(tmp_path):
    planned = NOW + timedelta(hours=1)                      # 21:00
    hud = _hud(92.0, resets_at=NOW + timedelta(hours=2))    # resets 22:00, after the start
    text = plan.warn_for(planned, _cfg(tmp_path), now=NOW, hud=hud)
    assert text is not None
    assert "92%" in text and "22:00" in text and "21:00" in text and "five-hour" in text


def test_reads_the_real_file_when_no_picture_is_passed(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.hud_file.parent.mkdir(parents=True)
    cfg.hud_file.write_text(json.dumps(_hud(92.0, resets_at=NOW + timedelta(hours=2))), encoding="utf-8")
    assert "92%" in plan.warn_for(NOW + timedelta(hours=1), cfg, now=NOW)


def test_no_file_means_no_warning(tmp_path):
    assert plan.warn_for(NOW + timedelta(hours=1), _cfg(tmp_path), now=NOW) is None


def test_a_stale_file_means_no_warning(tmp_path):
    hud = _hud(92.0, resets_at=NOW + timedelta(hours=2), fetched_at=NOW - timedelta(hours=1))
    assert plan.warn_for(NOW + timedelta(hours=1), _cfg(tmp_path), now=NOW, hud=hud) is None


def test_an_unknown_percent_is_never_invented(tmp_path):
    hud = _hud(None, resets_at=NOW + timedelta(hours=2))
    assert plan.warn_for(NOW + timedelta(hours=1), _cfg(tmp_path), now=NOW, hud=hud) is None


def test_a_start_after_the_reset_is_clear(tmp_path):
    hud = _hud(99.0, resets_at=NOW + timedelta(hours=2))
    assert plan.warn_for(NOW + timedelta(hours=3), _cfg(tmp_path), now=NOW, hud=hud) is None


def test_below_the_governor_line_is_clear(tmp_path):
    hud = _hud(60.0, resets_at=NOW + timedelta(hours=2))
    assert plan.warn_for(NOW + timedelta(hours=1), _cfg(tmp_path), now=NOW, hud=hud) is None


def test_the_line_is_the_governors_own_setting(tmp_path):
    cfg = dataclasses.replace(_cfg(tmp_path), governor={"wind_down_at_percent": {"five_hour": 50}})
    hud = _hud(60.0, resets_at=NOW + timedelta(hours=2))
    assert "60%" in plan.warn_for(NOW + timedelta(hours=1), cfg, now=NOW, hud=hud)


def test_the_weekly_window_names_its_reset_day(tmp_path):
    hud = _hud(10.0, resets_at=NOW + timedelta(hours=2), weekly=95.0)
    text = plan.warn_for(NOW + timedelta(hours=10), _cfg(tmp_path), now=NOW, hud=hud)
    assert "weekly" in text and "95%" in text and "Tue" in text   # NOW + 3 days = Tuesday


# ---------------------------------------------------------------- `--at reset` / `--at weekly-reset`


def test_reset_is_the_five_hour_reset_plus_two_minutes(tmp_path):
    hud = _hud(40.0, resets_at=NOW + timedelta(hours=2))                 # resets 22:00
    planned = plan.resolve_reset("reset", _cfg(tmp_path), now=NOW, hud=hud)
    assert planned == NOW + timedelta(hours=2, minutes=2)


def test_reset_rounds_a_stray_second_up_never_early(tmp_path):
    hud = _hud(40.0, resets_at=NOW + timedelta(hours=2, seconds=30))
    planned = plan.resolve_reset("reset", _cfg(tmp_path), now=NOW, hud=hud)
    assert planned == NOW + timedelta(hours=2, minutes=3) and planned.second == 0


def test_reset_reads_the_real_file_when_no_picture_is_passed(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.hud_file.parent.mkdir(parents=True)
    cfg.hud_file.write_text(json.dumps(_hud(40.0, resets_at=NOW + timedelta(hours=1))), encoding="utf-8")
    assert plan.resolve_reset("reset", cfg, now=NOW) == NOW + timedelta(hours=1, minutes=2)


def test_weekly_reset_uses_the_seven_day_time(tmp_path):
    hud = _hud(40.0, resets_at=NOW + timedelta(hours=2), weekly=10.0)    # weekly resets NOW + 3 days
    assert plan.resolve_reset("weekly-reset", _cfg(tmp_path), now=NOW, hud=hud) == NOW + timedelta(days=3, minutes=2)


@pytest.mark.parametrize("hud, words", [
    (None, "no usage file"),
    ({"claude": {"fetched_at": _iso(NOW - timedelta(hours=1)),
                 "five_hour_resets_at": _iso(NOW + timedelta(hours=1))}}, "older than 10 minutes"),
    (_hud(40.0, resets_at=NOW - timedelta(minutes=5)), "already passed"),
    ({"fetched_at": _iso(NOW), "claude": {"fetched_at": _iso(NOW), "five_hour_pct": 3.0}}, "no five-hour reset"),
    ({"fetched_at": _iso(NOW), "codex": {"fetched_at": _iso(NOW)}}, "nothing for claude"),
])
def test_reset_says_why_it_cannot_and_never_guesses(tmp_path, hud, words):
    with pytest.raises(ValueError) as err:
        plan.resolve_reset("reset", _cfg(tmp_path), now=NOW, hud=hud)
    assert words in str(err.value) and "Nothing was scheduled" in str(err.value)


def test_weekly_reset_missing_from_the_file_is_a_clear_error(tmp_path):
    hud = _hud(40.0, resets_at=NOW + timedelta(hours=2))                 # no weekly number
    with pytest.raises(ValueError) as err:
        plan.resolve_reset("weekly-reset", _cfg(tmp_path), now=NOW, hud=hud)
    assert "no weekly reset" in str(err.value)


def test_is_reset_word():
    assert plan.is_reset_word("reset") and plan.is_reset_word(" Weekly-Reset ")
    assert not plan.is_reset_word("06:00") and not plan.is_reset_word(None)


@pytest.mark.parametrize("pct", ["unknown", [], {}, None])
def test_warning_ignores_unusable_percent(tmp_path, pct):
    hud = _hud(pct, resets_at=NOW + timedelta(hours=2))
    assert plan.warn_for(NOW + timedelta(hours=1), _cfg(tmp_path), now=NOW, hud=hud) is None


@pytest.mark.parametrize("age, warns", [(600, True), (601, False)])
def test_warning_freshness_boundary(tmp_path, age, warns):
    hud = _hud(92, NOW + timedelta(hours=2), fetched_at=NOW - timedelta(seconds=age))
    assert (plan.warn_for(NOW + timedelta(hours=1), _cfg(tmp_path), now=NOW, hud=hud) is not None) is warns


def test_warning_does_not_include_window_resetting_exactly_at_start(tmp_path):
    planned = NOW + timedelta(hours=1)
    assert plan.warn_for(planned, _cfg(tmp_path), now=NOW, hud=_hud(99, planned)) is None


@pytest.mark.parametrize("hud", [[], 42, "broken", {"claude": []}, {"claude": "broken"}])
def test_warning_ignores_non_object_usage_picture(tmp_path, hud):
    assert plan.warn_for(NOW + timedelta(hours=1), _cfg(tmp_path), now=NOW, hud=hud) is None


def test_reset_at_current_instant_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="already passed"):
        plan.resolve_reset("reset", _cfg(tmp_path), now=NOW, hud=_hud(40, NOW))


@pytest.mark.parametrize("pct", ["NaN", "Infinity"])
def test_warning_rejects_nonfinite_percent(tmp_path, pct):
    hud = _hud(pct, NOW + timedelta(hours=2))
    assert plan.warn_for(NOW + timedelta(hours=1), _cfg(tmp_path), now=NOW, hud=hud) is None


@pytest.mark.parametrize("reset", [42, "2026-09-27T00:00:00"])
def test_reset_rejects_malformed_timestamp_with_clear_error(tmp_path, reset):
    hud = _hud(40, NOW + timedelta(hours=1))
    hud["claude"]["five_hour_resets_at"] = reset
    with pytest.raises(ValueError, match="Cannot work out the reset time"):
        plan.resolve_reset("reset", _cfg(tmp_path), now=NOW, hud=hud)


def test_codex_reads_its_own_picture(tmp_path):
    hud = _hud(92.0, resets_at=NOW + timedelta(hours=2), provider="codex")
    assert plan.warn_for(NOW + timedelta(hours=1), _cfg(tmp_path), provider="claude", now=NOW, hud=hud) is None
    assert "92%" in plan.warn_for(NOW + timedelta(hours=1), _cfg(tmp_path), provider="codex", now=NOW, hud=hud)
