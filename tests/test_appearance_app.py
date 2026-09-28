"""acceptance 5 (off-Windows / WT absent) plus a mount smoke test for the Settings screen
itself. `pantheon/appearance/wt.py` and `palette.py` have their own focused suites
(`test_wt_writer.py`, `test_palette.py`); this file covers `app.py`'s pure functions and the
Textual screen, the same pattern as `test_notes_app.py`."""
from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from pantheon import config as config_mod
from pantheon.appearance import app as appearance_app, wt

FIXTURE_TOML = Path(__file__).parent / "fixtures" / "appearance" / "pantheon.toml"


def _cfg(tmp_path):
    return config_mod.Config(state_dir=str(tmp_path / "state"))


def _toml_copy(tmp_path):
    dst = tmp_path / "pantheon.toml"
    shutil.copy2(FIXTURE_TOML, dst)
    return dst


# ---------------------------------------------------------------- acceptance 5


def test_snapshot_shows_the_honest_message_when_wt_is_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(wt, "find_settings", lambda *a, **k: None)
    cfg = _cfg(tmp_path)
    toml_path = _toml_copy(tmp_path)
    snap = appearance_app.snapshot(cfg, toml_path=toml_path)
    assert snap["font"]["source"] == "none"
    assert snap["font"]["message"] == wt.NOT_FOUND_MESSAGE


def test_apply_font_refuses_and_writes_nothing_when_wt_is_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(wt, "find_settings", lambda *a, **k: None)
    cfg = _cfg(tmp_path)
    toml_path = _toml_copy(tmp_path)
    before = toml_path.read_text(encoding="utf-8")

    result = appearance_app.apply_font(cfg, toml_path=toml_path, face="JetBrains Mono", size=12)

    assert result == {"ok": False, "message": wt.NOT_FOUND_MESSAGE}
    assert toml_path.read_text(encoding="utf-8") == before
    assert not (tmp_path / "state" / "appearance").exists()


def test_apply_font_writes_wt_and_remembers_in_pantheon_toml(tmp_path, monkeypatch):
    wt_path = tmp_path / "settings.json"
    shutil.copy2(Path(__file__).parent / "fixtures" / "appearance" / "wt_settings.jsonc", wt_path)
    monkeypatch.setattr(wt, "find_settings", lambda *a, **k: wt_path)
    cfg = _cfg(tmp_path)
    toml_path = _toml_copy(tmp_path)

    result = appearance_app.apply_font(
        cfg, toml_path=toml_path, profile_name="Pantheon", size=13, cell_width=1.1,
    )

    assert result["ok"] is True
    assert result["profile"] == "matched"
    assert Path(result["backup"]).exists()
    cfg2 = config_mod.load(toml_path)
    a = cfg2.appearance_settings()
    assert a.font_size == 13.0
    assert a.cell_width == 1.1


# ---------------------------------------------------------------- screen mount smoke test


def test_settings_screen_mounts_and_shows_phone_message_off_desk(tmp_path, monkeypatch):
    monkeypatch.setattr(wt, "find_settings", lambda *a, **k: None)
    cfg = _cfg(tmp_path)

    async def drive():
        app = appearance_app.AppearanceApp(cfg)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            body = app.query_one("#font-body")
            assert wt.NOT_FOUND_MESSAGE in str(body.content)
            # PALETTE section drew every token row without crashing.
            for token in appearance_app.palette.TOKEN_NAMES:
                app.query_one(f"#tok-{token}")

    asyncio.run(drive())


def test_mounting_the_screen_never_writes_pantheon_toml(tmp_path, monkeypatch):
    """Regression: mounting used to fire `RadioSet.Changed` for the pre-selected preset button
    (Textual's own default selection, not a click) straight through to `apply_preset_action`,
    which wrote `[theme] preset = "pantheon"` into `config_mod.DEFAULT_TOML` -- the real
    project's `pantheon.toml` -- the instant the screen opened, no click involved. Guards that
    `_syncing_ui` (see `AppearanceApp.__init__`) actually blocks it, without going anywhere near
    the real file: a decoy `DEFAULT_TOML` is substituted for the duration of the test."""
    monkeypatch.setattr(wt, "find_settings", lambda *a, **k: None)
    decoy = tmp_path / "decoy-pantheon.toml"
    monkeypatch.setattr(config_mod, "DEFAULT_TOML", decoy)
    cfg = _cfg(tmp_path)

    async def drive():
        app = appearance_app.AppearanceApp(cfg)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            await pilot.pause()

    asyncio.run(drive())
    assert not decoy.exists()


def test_settings_screen_switching_preset_repaints_live(tmp_path, monkeypatch):
    monkeypatch.setattr(wt, "find_settings", lambda *a, **k: None)
    cfg = _cfg(tmp_path)
    # Point the app at a throwaway pantheon.toml so this never touches the real one.
    monkeypatch.setattr(appearance_app, "_toml_path", lambda c: _toml_copy(tmp_path))

    async def drive():
        app = appearance_app.AppearanceApp(cfg)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            result = appearance_app.apply_preset_action("high-contrast", cfg, toml_path=appearance_app._toml_path(cfg))
            app._apply_live_theme(result["resolved"], "high-contrast")
            await pilot.pause()
            assert app.theme == "pantheon-custom"
            registered = app.get_theme("pantheon-custom")
            assert registered.background == appearance_app.palette.PRESETS["high-contrast"]["background"]

    asyncio.run(drive())
