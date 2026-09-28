"""palette presets and per-token overrides in `pantheon/appearance/palette.py`."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from pantheon import config as config_mod, theme as theme_mod
from pantheon.appearance import palette

FIXTURE = Path(__file__).parent / "fixtures" / "appearance" / "pantheon.toml"


@pytest.fixture()
def toml_copy(tmp_path):
    dst = tmp_path / "pantheon.toml"
    shutil.copy2(FIXTURE, dst)
    return dst


# ---- parity: every shipped preset covers every token theme.py defines -------------------------

def test_every_preset_covers_every_theme_token():
    for name in palette.PRESET_NAMES:
        assert set(palette.PRESETS[name].keys()) == set(theme_mod.TOKENS.keys()), name


def test_preset_names_match_appearance_theme_choices():
    # `[appearance] theme` and `[theme] preset` name the same three themes.
    assert set(palette.PRESET_NAMES) == set(theme_mod.THEME_FACTORIES.keys())


# ---- apply_preset --------------------------------------------------------------------------

def test_apply_preset_writes_valid_theme_tokens(toml_copy):
    resolved = palette.apply_preset(toml_copy, "high-contrast")
    assert resolved == palette.PRESETS["high-contrast"]
    loaded = palette.load_theme_toml(toml_copy)
    assert loaded.preset == "high-contrast"


def test_apply_preset_unknown_name_refused(toml_copy):
    with pytest.raises(palette.UnknownPresetError):
        palette.apply_preset(toml_copy, "neon-goth")
    # Nothing was written.
    loaded = palette.load_theme_toml(toml_copy)
    assert loaded.preset == "pantheon"


def test_apply_preset_leaves_the_rest_of_the_file_alone(toml_copy):
    before = toml_copy.read_text(encoding="utf-8")
    palette.apply_preset(toml_copy, "light")
    after = toml_copy.read_text(encoding="utf-8")
    for line in before.splitlines():
        if line.strip():
            assert line in after


# ---- override / reset -----------------------------------------------------------------------

def test_override_unknown_token_refused_with_token_list(toml_copy):
    with pytest.raises(palette.UnknownTokenError) as exc:
        palette.override(toml_copy, "bogus_token", "#FFFFFF")
    for name in palette.TOKEN_NAMES:
        assert name in str(exc.value)


def test_override_then_resolved_tokens_reflect_it(toml_copy):
    resolved = palette.override(toml_copy, "chrome", "#33E0FF")
    assert resolved["chrome"] == "#33E0FF"
    # Every other token is untouched from the active (default) preset.
    assert resolved["success"] == palette.PRESETS["pantheon"]["success"]


def test_reset_token_falls_back_to_the_preset_value(toml_copy):
    palette.apply_preset(toml_copy, "high-contrast")
    palette.override(toml_copy, "chrome", "#33E0FF")
    resolved = palette.reset_token(toml_copy, "chrome")
    assert resolved["chrome"] == palette.PRESETS["high-contrast"]["chrome"]


def test_reset_all_clears_every_override_keeps_preset(toml_copy):
    palette.apply_preset(toml_copy, "light")
    palette.override(toml_copy, "chrome", "#111111")
    palette.override(toml_copy, "success", "#222222")
    resolved = palette.reset_all(toml_copy)
    assert resolved == palette.PRESETS["light"]
    loaded = palette.load_theme_toml(toml_copy)
    assert loaded.preset == "light"
    assert loaded.overrides == {}


def test_reset_token_unknown_refused(toml_copy):
    with pytest.raises(palette.UnknownTokenError):
        palette.reset_token(toml_copy, "not_a_token")


# ---- resolved_tokens (pure) -------------------------------------------------------------------

def test_resolved_tokens_unknown_preset():
    with pytest.raises(palette.UnknownPresetError):
        palette.resolved_tokens("neon-goth", {})


def test_resolved_tokens_unknown_override_key():
    with pytest.raises(palette.UnknownTokenError):
        palette.resolved_tokens("pantheon", {"bogus": "#fff"})


def test_resolved_tokens_layers_overrides_on_preset():
    tokens = palette.resolved_tokens("pantheon", {"chrome": "#ABCDEF"})
    assert tokens["chrome"] == "#ABCDEF"
    assert tokens["muted"] == palette.PRESETS["pantheon"]["muted"]


# ---- make_theme / dark flag --------------------------------------------------------------------

def test_make_theme_builds_a_textual_theme():
    tokens = palette.resolved_tokens("pantheon")
    t = palette.make_theme("pantheon-custom", tokens, dark=True)
    assert t.name == "pantheon-custom"
    assert t.primary == tokens["chrome"]
    assert t.background == tokens["background"]
    assert t.dark is True


def test_preset_dark_matches_theme_factories():
    from textual.theme import Theme

    for name, factory in theme_mod.THEME_FACTORIES.items():
        built: Theme = factory()
        assert palette.PRESET_DARK[name] == built.dark


# ---- overriding a truecolor token never touches the
# 16-colour fallback map -- every state stays distinguishable by its printed label in TERM=xterm
# regardless of what the user has overridden in truecolor. ------------------------------------------

def test_overriding_tokens_never_touches_the_16_colour_fallback(toml_copy):
    before = dict(theme_mod.FALLBACK_16)
    palette.apply_preset(toml_copy, "high-contrast")
    palette.override(toml_copy, "chrome", "#33E0FF")
    palette.override(toml_copy, "error", "#FF00FF")
    assert theme_mod.FALLBACK_16 == before


# ---- write_appearance_font ----------------------------------------------------------------------

def test_write_appearance_font_round_trips(toml_copy):
    palette.write_appearance_font(toml_copy, {
        "font_face": "JetBrains Mono", "font_size": 12.0,
        "cell_height": 1.1, "cell_width": 0.95,
    })
    cfg = config_mod.load(toml_copy)
    appearance = cfg.appearance_settings()
    assert appearance.font_face == "JetBrains Mono"
    assert appearance.font_size == 12.0
    assert appearance.cell_height == 1.1
    assert appearance.cell_width == 0.95
    # theme = "pantheon" (untouched key already in the fixture) survives.
    assert appearance.theme == "pantheon"


def test_write_appearance_font_creates_appearance_table_if_missing(tmp_path):
    dst = tmp_path / "pantheon.toml"
    dst.write_text('vault = "C:/x"\n', encoding="utf-8")
    palette.write_appearance_font(dst, {"font_face": "Consolas"})
    cfg = config_mod.load(dst)
    assert cfg.appearance_settings().font_face == "Consolas"
