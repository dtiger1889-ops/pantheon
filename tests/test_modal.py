"""The one modal shape: `Confirm` and `Pick`. Driven headlessly
at both a phone size and a desk size, per  -- a click, an
accelerator key, and Escape must each produce the right dismiss value, and Escape must always
cancel even though `ModalScreen` has no default binding for it (T7).
"""
from __future__ import annotations

import asyncio
import functools

from textual.app import App, ComposeResult
from textual.widgets import Label

from pantheon.widgets.modal import Confirm, Pick, PickOption, TextPrompt, numbered


def drives_the_screen(test):
    @functools.wraps(test)
    def wrapper(*args, **kwargs):
        return asyncio.run(test(*args, **kwargs))
    return wrapper


class _Harness(App):
    """An empty screen a modal can be pushed onto, at whatever size the test picks."""

    def compose(self) -> ComposeResult:
        yield Label("host")


SIZES = [(65, 26), (120, 40)]


# ---------------------------------------------------------------- Confirm


@drives_the_screen
async def test_confirm_click_yes_returns_true():
    for size in SIZES:
        app = _Harness()
        seen = {}
        async with app.run_test(size=size) as pilot:
            app.push_screen(Confirm("close window 3 (loom-os)?"), lambda v: seen.__setitem__("v", v))
            await pilot.pause()
            assert "close window 3" in str(app.screen.query_one("#question", expect_type=Label).content)
            await pilot.click("#yes")
            await pilot.pause()
        assert seen["v"] is True


@drives_the_screen
async def test_confirm_click_no_returns_false():
    for size in SIZES:
        app = _Harness()
        seen = {}
        async with app.run_test(size=size) as pilot:
            app.push_screen(Confirm("kill it?"), lambda v: seen.__setitem__("v", v))
            await pilot.pause()
            await pilot.click("#no")
            await pilot.pause()
        assert seen["v"] is False


@drives_the_screen
async def test_confirm_accelerator_keys_y_and_n():
    app = _Harness()
    seen = []
    async with app.run_test(size=(80, 24)) as pilot:
        app.push_screen(Confirm("q1?"), seen.append)
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()
        app.push_screen(Confirm("q2?"), seen.append)
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
    assert seen == [True, False]


@drives_the_screen
async def test_confirm_escape_cancels_with_none_and_pops_the_screen():
    app = _Harness()
    seen = []
    async with app.run_test(size=(80, 24)) as pilot:
        assert len(app.screen_stack) == 1
        app.push_screen(Confirm("q?"), seen.append)
        await pilot.pause()
        assert len(app.screen_stack) == 2
        await pilot.press("escape")
        await pilot.pause()
        assert len(app.screen_stack) == 1
    assert seen == [None]


@drives_the_screen
async def test_confirm_no_is_focused_by_default_so_the_safe_answer_is_the_easy_hit():
    from textual.widgets import Button

    app = _Harness()
    async with app.run_test(size=(80, 24)) as pilot:
        app.push_screen(Confirm("q?"), lambda v: None)
        await pilot.pause()
        assert app.screen.query_one("#no", Button).has_focus


# ---------------------------------------------------------------- Confirm: danger (copy review
# "interaction issue 1" -- red is reserved for a confirm whose Yes is actually destructive)


@drives_the_screen
async def test_confirm_yes_is_primary_by_default_not_red():
    from textual.widgets import Button

    app = _Harness()
    async with app.run_test(size=(80, 24)) as pilot:
        app.push_screen(Confirm("start an agent on it anyway?"), lambda v: None)
        await pilot.pause()
        assert app.screen.query_one("#yes", Button).variant == "primary"


@drives_the_screen
async def test_confirm_danger_true_makes_yes_the_red_error_variant():
    from textual.widgets import Button

    app = _Harness()
    async with app.run_test(size=(80, 24)) as pilot:
        app.push_screen(Confirm("close window 3 (loom-os)?", danger=True), lambda v: None)
        await pilot.pause()
        assert app.screen.query_one("#yes", Button).variant == "error"
        # No stays primary either way -- only Yes carries the danger tier.
        assert app.screen.query_one("#no", Button).variant == "primary"


# ---------------------------------------------------------------- Pick


@drives_the_screen
async def test_pick_enter_on_the_highlighted_option():
    options = numbered([("claude", "Claude in tmux"), ("codex", "Codex headless job")])
    for size in SIZES:
        app = _Harness()
        seen = {}
        async with app.run_test(size=size) as pilot:
            app.push_screen(Pick("who works it", options), lambda v: seen.__setitem__("v", v))
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
        assert seen["v"] == "claude"


@drives_the_screen
async def test_pick_click_a_later_option():
    options = numbered([("claude", "Claude in tmux"), ("codex", "Codex headless job")])
    app = _Harness()
    seen = {}
    async with app.run_test(size=(80, 24)) as pilot:
        app.push_screen(Pick("who works it", options), lambda v: seen.__setitem__("v", v))
        await pilot.pause()
        from textual.widgets import OptionList

        option_list = app.screen.query_one(OptionList)
        option_list.highlighted = 1
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
    assert seen["v"] == "codex"


@drives_the_screen
async def test_pick_accelerator_keys_match_the_old_inline_prompt():
    """The `x` -> choose-worker prompt used single letters (c/x/i/l); Pick keeps them working."""
    options = [
        PickOption("c", "claude", "Claude in tmux · the usual"),
        PickOption("x", "codex-headless", "Codex headless job"),
        PickOption("i", "codex-pc", "Codex in a PC window"),
        PickOption("l", "ollama", "local model", disabled=True),
    ]
    app = _Harness()
    seen = []
    async with app.run_test(size=(65, 26)) as pilot:
        app.push_screen(Pick("who works it", options), seen.append)
        await pilot.pause()
        await pilot.press("i")
        await pilot.pause()
    assert seen == ["codex-pc"]


@drives_the_screen
async def test_pick_disabled_accelerator_does_nothing():
    options = [PickOption("l", "ollama", "local model", disabled=True)]
    app = _Harness()
    seen = []
    async with app.run_test(size=(80, 24)) as pilot:
        app.push_screen(Pick("pick", options), seen.append)
        await pilot.pause()
        await pilot.press("l")
        await pilot.pause()
        assert len(app.screen_stack) == 2       # still open -- disabled options refuse
        await pilot.press("escape")
        await pilot.pause()
    assert seen == [None]


@drives_the_screen
async def test_pick_escape_cancels_with_none():
    options = numbered([("a", "Option A")])
    app = _Harness()
    seen = []
    async with app.run_test(size=(80, 24)) as pilot:
        app.push_screen(Pick("pick", options), seen.append)
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
    assert seen == [None]


# ---------------------------------------------------------------- desk sizing
#


@drives_the_screen
async def test_confirm_is_the_phone_width_under_80_columns():
    app = _Harness()
    async with app.run_test(size=(65, 26)) as pilot:
        app.push_screen(Confirm("close window 3 (loom-os)?"), lambda v: None)
        await pilot.pause()
        box = app.screen.query_one("#box")
        assert box.styles.width.value == 50
        assert box.styles.max_height.value == 12


@drives_the_screen
async def test_confirm_widens_at_80_columns_and_up():
    app = _Harness()
    async with app.run_test(size=(120, 40)) as pilot:
        app.push_screen(Confirm("close window 3 (loom-os)?"), lambda v: None)
        await pilot.pause()
        box = app.screen.query_one("#box")
        assert box.styles.width.value == 76
        assert box.styles.max_height.value == 20


@drives_the_screen
async def test_pick_widens_at_the_desk_too():
    options = numbered([("claude", "Claude in tmux"), ("codex", "Codex headless job")])
    app = _Harness()
    async with app.run_test(size=(120, 40)) as pilot:
        app.push_screen(Pick("who works it", options), lambda v: None)
        await pilot.pause()
        box = app.screen.query_one("#box")
        assert box.styles.width.value == 76
        assert box.styles.max_height.value == 20


def test_pick_marks_the_current_value():
    options = numbered([("low", "low"), ("high", "high")])
    p = Pick("effort", options, current="high")
    assert p._line(options[0]).startswith(" ")
    assert p._line(options[1]).startswith("●")


# ---------------------------------------------------------------- TextPrompt (the "first message"
# box, cut off on the right at phone width because it had no `_size_box` call -- see modal.py)


@drives_the_screen
async def test_text_prompt_is_the_phone_width_under_80_columns_and_stays_on_screen():
    app = _Harness()
    async with app.run_test(size=(60, 30)) as pilot:
        app.push_screen(
            TextPrompt("first message", placeholder="what should it start on?"), lambda v: None)
        await pilot.pause()
        box = app.screen.query_one("#box")
        assert box.styles.width.value == 50
        assert box.styles.max_height.value == 12
        region = box.region
        assert region.x >= 0
        assert region.x + region.width <= app.size.width


@drives_the_screen
async def test_text_prompt_widens_at_80_columns_and_up():
    app = _Harness()
    async with app.run_test(size=(120, 40)) as pilot:
        app.push_screen(
            TextPrompt("first message", placeholder="what should it start on?"), lambda v: None)
        await pilot.pause()
        box = app.screen.query_one("#box")
        assert box.styles.width.value == 76
        assert box.styles.max_height.value == 20
        region = box.region
        assert region.x + region.width <= app.size.width
