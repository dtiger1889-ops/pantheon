"""The deck's rotating tip line (widgets/tips.py): it names only keys that are really bound and
enabled where the keys go right now, never one the footer already shows at this width, and it
takes no row under 120 columns."""
from __future__ import annotations

import dataclasses

from textual.binding import Binding
from textual.widgets._footer import FooterKey

from pantheon import config as config_mod
from pantheon.deck import app as deck_app_mod
from pantheon.widgets import tips as tips_mod

from .test_deck_app import _app, drives_the_screen


def _footer_shown(app) -> set[str]:
    """Keys whose real `FooterKey` sits wholly inside the footer -- what the user can read."""
    right = app.query_one("PillFooter").region.right
    return {k.key for k in app.query(FooterKey) if k.region.width and k.region.right <= right}


def _live_keys(app) -> dict[str, bool]:
    return {key: enabled for key, (_n, _b, enabled, _t) in app.screen.active_bindings.items()}


@drives_the_screen
async def test_desk_tips_are_real_live_keys_the_footer_does_not_show(tmp_path):
    app = _app(tmp_path)
    for width in (200, 150, 120):
        async with app.run_test(size=(width, 45)) as pilot:
            await pilot.pause()
            await pilot.pause()
            assert app.tip_bar.display
            tips = app.tip_bar.tips
            assert tips, f"no tips at {width}"
            live = _live_keys(app)
            displays = {app.get_key_display(b): key
                        for key, (_n, b, _e, _t) in app.screen.active_bindings.items()}
            shown = _footer_shown(app)
            for display, words in tips:
                if display == "1-9":
                    continue   # the SESSIONS list's numbers, only while it has the keys
                key = displays[display]
                assert live[key], f"{display} is advertised but disabled"
                assert key not in shown, f"{display} is advertised but already on the footer"
                assert words
            # the arithmetic footer layout agrees with the footer Textual actually drew
            entries = [(b.key, app.get_key_display(b), b.description, b.action)
                       for _n, b, _e, _t in app.screen.active_bindings.values() if b.show]
            assert tips_mod.footer_visible_keys(entries, width) == shown
        app = _app(tmp_path / str(width))


@drives_the_screen
async def test_a_footer_key_cut_off_at_the_edge_becomes_a_tip(tmp_path):
    """At 120 columns the strip runs out before `R` and `q`; at 200 it shows them."""
    app = _app(tmp_path)
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        await pilot.pause()
        keys = [k for k, _w in app.tip_bar.tips]
        assert "q" not in _footer_shown(app) and "q" in keys
    app = _app(tmp_path / "wide")
    async with app.run_test(size=(200, 45)) as pilot:
        await pilot.pause()
        await pilot.pause()
        keys = [k for k, _w in app.tip_bar.tips]
        assert "q" in _footer_shown(app) and "q" not in keys
        assert "P" in keys and "U" in keys   # deck keys the footer never shows


@drives_the_screen
async def test_phone_tier_has_no_tip_row(tmp_path):
    app = _app(tmp_path)
    async with app.run_test(size=(119, 30)) as pilot:
        await pilot.pause()
        assert app.tip_bar.display is False
        await pilot.resize_terminal(180, 45)
        await pilot.pause()
        await pilot.pause()
        assert app.tip_bar.display and app.tip_bar.tips
        await pilot.resize_terminal(65, 26)
        await pilot.pause()
        assert app.tip_bar.display is False


@drives_the_screen
async def test_tips_false_turns_the_line_off(tmp_path):
    app = _app(tmp_path)
    app.cfg = dataclasses.replace(app.cfg, deck={"tips": False})
    app._tips_on = app.cfg.deck_settings().tips
    async with app.run_test(size=(200, 45)) as pilot:
        await pilot.pause()
        assert app.tip_bar.display is False


@drives_the_screen
async def test_the_line_turns_to_the_next_tip(tmp_path):
    app = _app(tmp_path)
    async with app.run_test(size=(200, 45)) as pilot:
        await pilot.pause()
        await pilot.pause()
        first = app.tip_bar.current
        app._rotate_tip()
        assert app.tip_bar.current != first
        assert app.tip_bar.current in app.tip_bar.tips


def test_deck_tips_config():
    assert config_mod.Config().deck_settings().tips is True
    for off in (False, "false", "off", "no", "0"):
        assert config_mod.Config(deck={"tips": off}).deck_settings().tips is False
    assert config_mod.Config(deck={"tips": True}).deck_settings().tips is True


def test_compute_tips_only_from_worded_enabled_hidden_bindings():
    class Node:
        pass

    def entry(binding, enabled=True):
        return (Node(), binding, enabled, "")

    active = [
        entry(Binding("q", "quit_deck", "quit window")),                     # on the footer
        entry(Binding("P", "toggle_pit", "pit", show=False)),                # hidden: a tip
        entry(Binding("U", "open_planning", "usage plan", show=False), enabled=False),
        entry(Binding("ctrl+q", "quit", "quit", show=False)),                # no words: never
        entry(Binding("escape", "close_keys", "close keys", show=False)),    # skipped key
        entry(Binding("1", "jump_number(1)", show=False)),
        entry(Binding("2", "jump_number(2)", show=False)),
    ]
    tips = tips_mod.compute_tips(active, lambda b: b.key, 200)
    assert tips == [("P", tips_mod.WORDS["toggle_pit"]), ("1-2", tips_mod.NUMBER_WORDS)]
    # too narrow for even `q quit window`: the footer key is cut off, so it is a tip too
    narrow = tips_mod.compute_tips(active, lambda b: b.key, 5)
    assert ("q", tips_mod.WORDS["quit_deck"]) in narrow


def test_every_hidden_deck_key_has_words():
    """A deck-wide key added with `show=False` and no words would silently never be a tip."""
    missing = [b.action for b in deck_app_mod.DeckApp.BINDINGS
               if isinstance(b, Binding) and b.key not in tips_mod.SKIP_KEYS
               and b.action not in tips_mod.NOT_TIPS and not tips_mod.WORDS.get(b.action)]
    assert not any(a in tips_mod.WORDS for a in tips_mod.NOT_TIPS)
    assert not missing, f"add plain words to widgets/tips.py WORDS for: {missing}"
