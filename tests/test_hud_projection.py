"""part (a), acceptance 3: the BUDGET card's `at reset` projection. The collector folds
`planning.block_projection` into each provider's usage; the card draws it beside the burn and as a
faint stretch of the 5-hour gauge -- and draws nothing while there is no burn."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from pantheon import config
from pantheon.hud import app as hud_app
from pantheon.hud import sources
from pantheon.models import ProviderUsage


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def widget_text(widget) -> str:
    content = widget.content
    return getattr(content, "plain", None) or str(content)


def _usage(**over) -> dict:
    now = datetime.now(timezone.utc)
    usage = {
        "provider": "claude", "source": "statusline", "five_hour_pct": 40.0, "seven_day_pct": 20.0,
        "five_hour_resets_at": iso(now + timedelta(hours=2)), "five_hour_as_of": iso(now),
        "burn_pct_per_hour": 6.0,
        "projection": {"pct": 52.0, "low": 48.0, "high": 56.0, "readings": 3, "caution": False,
                       "by": iso(now + timedelta(hours=2))},
    }
    usage.update(over)
    return usage


def _mount(usage, hide=None, size=(90, 12)):
    async def _go():
        cfg = config.Config(appearance={"hide": hide or []})

        class _Host(hud_app.App):
            def compose(self):
                yield hud_app.ProviderCard("claude", usage, cfg, id="c")

        app = _Host()
        async with app.run_test(size=size):
            foot1 = widget_text(app.query_one("#c-foot1"))
            gauge = app.query_one("#c-5h").content
        return foot1, gauge

    return asyncio.run(_go())


def test_card_shows_the_projection_with_its_range_and_readings():
    foot1, gauge = _mount(_usage())
    assert "at reset 52% (48-56%) · 3 readings" in foot1
    # the faint stretch: more cells than the filled 40% are coloured past the fill
    assert gauge.plain.count("░") >= 1


def test_no_projection_while_burn_is_unknown():
    foot1, _ = _mount(_usage(burn_pct_per_hour=None))
    assert "at reset" not in foot1


def test_no_projection_on_an_old_reading():
    old = iso(datetime.now(timezone.utc) - timedelta(minutes=30))
    foot1, _ = _mount(_usage(five_hour_as_of=old))
    assert "at reset" not in foot1


def test_hide_projection():
    foot1, _ = _mount(_usage(), hide=["projection"])
    assert "at reset" not in foot1 and "burn" in foot1


# ---- the collector: three refreshes with a live session burning (acceptance 3) ----------------

def _collect_three(monkeypatch, cfg, readings):
    """Run `sources.collect` once per (minutes-from-start, percent) reading, with a fake live
    Claude capture and no ccusage. Returns each pass's Claude usage."""
    start = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
    reset = iso(start + timedelta(hours=4))
    clock = {"now": start}

    def fake_claude(directory, *a, **k):
        u = ProviderUsage(provider="claude", source="statusline")
        u.five_hour_pct = clock["pct"]
        u.five_hour_resets_at = reset
        u.five_hour_as_of = iso(clock["now"])
        return u

    monkeypatch.setattr(sources, "claude_from_statusline", fake_claude)
    monkeypatch.setattr(sources, "codex_rate_limits", lambda *a, **k: ProviderUsage(provider="codex"))
    monkeypatch.setattr(sources, "claude_from_ccusage", lambda *a, **k: ProviderUsage(provider="claude"))
    monkeypatch.setattr(sources, "codex_from_ccusage", lambda *a, **k: ProviderUsage(provider="codex"))
    monkeypatch.setattr(sources, "_now_epoch", lambda: clock["now"].timestamp())
    out = []
    for minutes, pct in readings:
        clock["now"] = start + timedelta(minutes=minutes)
        clock["pct"] = pct
        out.append(sources.collect(cfg)["claude"])
    return out


def test_projection_follows_the_gauge_over_three_refreshes(tmp_path, monkeypatch):
    cfg = config.Config(state_dir=str(tmp_path / "state"))
    config.ensure_state_dirs(cfg)
    passes = _collect_three(monkeypatch, cfg, [(0, 10.0), (10, 12.0), (20, 16.0)])
    assert passes[0]["burn_pct_per_hour"] is None and passes[0]["projection"] is None
    first, second = passes[1]["projection"], passes[2]["projection"]
    assert first and second
    assert second["pct"] > first["pct"]                        # the bar sped up; so did the landing
    assert first["low"] <= first["pct"] <= first["high"]
    assert second["readings"] == 3


def test_collector_projects_nothing_without_a_burn(tmp_path, monkeypatch):
    cfg = config.Config(state_dir=str(tmp_path / "state"))
    config.ensure_state_dirs(cfg)
    passes = _collect_three(monkeypatch, cfg, [(0, 10.0), (10, 10.0), (20, 10.0)])
    assert passes[-1]["burn_pct_per_hour"] == 0.0
    proj = passes[-1]["projection"]
    assert proj["pct"] == 10.0 and proj["low"] == 10.0 and proj["high"] > 10.0


def test_text_block_carries_the_projection_too():
    cfg = config.Config()
    lines = hud_app.render_block(_usage(), cfg, width=120)
    assert any("at reset 52% (48-56%) · 3 readings" in line for line in lines)
    lines = hud_app.render_block(_usage(burn_pct_per_hour=None), cfg, width=120)
    assert not any("at reset" in line for line in lines)
