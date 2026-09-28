from pantheon import config as config_mod
from pantheon import glyphs, theme


def test_bar_ascii_is_pure_ascii():
    s = glyphs.bar(45, 20, "ascii")
    assert len(s) == 20 and all(ord(c) < 128 for c in s)
    assert glyphs.bar(None, 5, "ascii") == "?????"
    assert glyphs.bar(100, 10, "unicode").count(glyphs.UNICODE["bar_full"]) == 10


def test_bar_has_band_ticks():
    s = glyphs.bar(10, 20, "ascii")
    assert s[14] == "|" and s[18] == "|"


def test_bar_blocks_style_swaps_fill_glyph_only():
    # `[appearance] gauge = "blocks"`: the fill glyph changes (a hairline-separated cell), the
    # bullet-graph semantics -- width, filled count, band ticks -- stay identical to "solid".
    solid = glyphs.bar(45, 20, "unicode", blocks=False)
    blocks = glyphs.bar(45, 20, "unicode", blocks=True)
    assert len(solid) == len(blocks) == 20
    assert blocks.count(glyphs.UNICODE["bar_block"]) == solid.count(glyphs.UNICODE["bar_full"])
    assert glyphs.UNICODE["bar_full"] not in blocks
    # empty cells and band ticks are unaffected by the style
    for i, (a, b) in enumerate(zip(solid, blocks)):
        if a not in (glyphs.UNICODE["bar_full"], glyphs.UNICODE["bar_block"]):
            assert a == b, f"cell {i} differs outside the fill glyph"


def test_bar_blocks_style_is_pure_ascii_in_ascii_mode():
    # ASCII fallback: "blocks" still degrades to the same "#" fill as "solid" in ascii
    # mode, since ASCII has no hairline glyph to spare.
    s = glyphs.bar(45, 20, "ascii", blocks=True)
    assert all(ord(c) < 128 for c in s)
    assert s == glyphs.bar(45, 20, "ascii", blocks=False)


def test_sparkline():
    assert glyphs.sparkline([], "ascii") == ""
    s = glyphs.sparkline([1, 2, 3, 4], "ascii")
    assert len(s) == 4 and s[0] == "_" and s[-1] == "@"
    assert len(glyphs.sparkline([1, 2, 3, 4, 5], "unicode", width=3)) == 3


def test_icon_table_is_the_plain_table_with_icons_off():
    # The default: every screen renders exactly as it did before
    # the setting existed.
    assert glyphs.icon_table("unicode", False) == glyphs.UNICODE
    assert glyphs.icon_table("ascii", False) == glyphs.ASCII


def test_icon_table_layers_nerd_glyphs_over_the_plain_table_when_on():
    merged = glyphs.icon_table("unicode", True)
    assert merged["ok"] == glyphs.NERD["ok"] != glyphs.UNICODE["ok"]
    assert merged["fail"] == glyphs.NERD["fail"]
    # Never touches the data-layer bar/sparkline glyphs -- `NERD` has no entry for them, so the
    # fallback table's own value survives untouched.
    assert merged["bar_full"] == glyphs.UNICODE["bar_full"]
    assert merged["spark"] == glyphs.UNICODE["spark"]


def test_bar_and_sparkline_never_draw_nerd_glyphs():
    # `bar()`/`sparkline()` take no `icons` argument at all -- they can only ever draw from
    # `UNICODE`/`ASCII` via `table()`, regardless of `[appearance] icons`.
    s = glyphs.bar(50, 10, "unicode")
    assert all(c not in glyphs.NERD.values() for c in s)


def test_tiers():
    assert theme.tier_for_percent(None) == "unknown"
    assert theme.tier_for_percent(10) == "silent"
    assert theme.tier_for_percent(70) == "caution"
    assert theme.tier_for_percent(90) == "warning"
    assert theme.role_for_tier("silent") is None and theme.role_for_tier("warning") == "error"


# ---------------------------------------------------------------- appearance / themes


def test_apply_registers_all_three_themes_and_defaults_to_pantheon():
    from textual.app import App

    app = App()
    theme.apply(app, config_mod.Config())
    assert app.theme == "pantheon"
    assert {"pantheon", "high-contrast", "light"} <= set(app.available_themes)


def test_apply_picks_the_theme_named_in_appearance():
    from textual.app import App

    app = App()
    theme.apply(app, config_mod.Config(appearance={"theme": "high-contrast"}))
    assert app.theme == "high-contrast"


def test_apply_falls_back_to_pantheon_on_an_unknown_theme_name():
    from textual.app import App

    app = App()
    theme.apply(app, config_mod.Config(appearance={"theme": "not-a-real-theme"}))
    assert app.theme == "pantheon"


def test_apply_with_no_cfg_argument_reads_app_cfg():
    """Every existing call site says `theme.apply(self)` with no second argument; `cfg` must
    fall back to the app's own `.cfg` attribute so none of them need to change."""
    from textual.app import App

    class _Host(App):
        def __init__(self) -> None:
            super().__init__()
            self.cfg = config_mod.Config(appearance={"theme": "light"})

    app = _Host()
    theme.apply(app)
    assert app.theme == "light"


def test_light_theme_is_not_dark():
    light = theme.make_light_theme()
    assert light.dark is False


def test_high_contrast_theme_has_a_pure_white_foreground():
    hc = theme.make_high_contrast_theme()
    assert hc.foreground.upper() == "#FFFFFF"
