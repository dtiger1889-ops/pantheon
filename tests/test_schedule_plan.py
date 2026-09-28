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


def test_codex_reads_its_own_picture(tmp_path):
    hud = _hud(92.0, resets_at=NOW + timedelta(hours=2), provider="codex")
    assert plan.warn_for(NOW + timedelta(hours=1), _cfg(tmp_path), provider="claude", now=NOW, hud=hud) is None
    assert "92%" in plan.warn_for(NOW + timedelta(hours=1), _cfg(tmp_path), provider="codex", now=NOW, hud=hud)
