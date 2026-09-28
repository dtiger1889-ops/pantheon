"""The budget window's `r` key after the collector moved into its own process: the
window never shells out itself; `r` writes the poke file the collector process watches."""
from __future__ import annotations

import asyncio

from pantheon import config
from pantheon.hud import app as hud_app
from pantheon.hud import collector


def _cfg(tmp_path):
    return config.Config(state_dir=str(tmp_path / "state"))


def _press_r(cfg, start_refresher: bool) -> str:
    async def _go():
        application = hud_app.HudApp(cfg, start_refresher=start_refresher)
        async with application.run_test() as pilot:
            await pilot.press("r")
            await pilot.pause()
            content = application.query_one("#hud-message").content
            return getattr(content, "plain", None) or str(content)

    return asyncio.run(_go())


def test_r_asks_the_collector_for_fresh_numbers(tmp_path):
    cfg = _cfg(tmp_path)
    message = _press_r(cfg, start_refresher=True)
    assert collector.poke_path(cfg).exists()
    assert "usage" in message


def test_without_a_collector_r_only_rereads_the_file(tmp_path):
    cfg = _cfg(tmp_path)
    message = _press_r(cfg, start_refresher=False)
    assert not collector.poke_path(cfg).exists()
    assert "saved" in message


def test_the_window_never_collects_on_its_own(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(hud_app.sources, "collect", lambda c: called.append(1))
    _press_r(_cfg(tmp_path), start_refresher=True)
    assert called == []
