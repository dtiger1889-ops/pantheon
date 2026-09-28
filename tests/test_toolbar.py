"""The row toolbar: a
sibling `Horizontal` of real buttons, never inside the table (T1), collapsing to one
`Actions… (Enter)` button under 80 columns. Driven headlessly; no pane is needed, just the widget
and its `dispatch` callback.
"""
from __future__ import annotations

import asyncio
import functools

from textual.app import App, ComposeResult
from textual.widgets import Button

from pantheon.widgets.toolbar import Action, RowToolbar


def drives_the_screen(test):
    @functools.wraps(test)
    def wrapper(*args, **kwargs):
        return asyncio.run(test(*args, **kwargs))
    return wrapper


ROW_ACTIONS = [
    Action("j", "Jump", "jump", "primary"),
    Action("k", "Kill", "kill", "error"),
    Action("m", "Model…", "model"),
]


class _Harness(App):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []
        self.bar = RowToolbar(self.calls.append)

    def compose(self) -> ComposeResult:
        yield self.bar


@drives_the_screen
async def test_no_actions_hides_the_whole_bar():
    app = _Harness()
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.bar.display is False
        app.bar.set_actions(ROW_ACTIONS)
        await pilot.pause()
        assert app.bar.display is True


@drives_the_screen
async def test_button_labels_carry_their_key():
    app = _Harness()
    async with app.run_test() as pilot:
        app.bar.set_actions(ROW_ACTIONS)
        await pilot.pause()
        labels = [str(b.label) for b in app.query(Button)]
        assert "Jump (j)" in labels and "Kill (k)" in labels and "Model… (m)" in labels


@drives_the_screen
async def test_clicking_a_button_dispatches_its_action_name():
    app = _Harness()
    async with app.run_test() as pilot:
        app.bar.set_actions(ROW_ACTIONS)
        await pilot.pause()
        await pilot.click("Button#act-kill")
        await pilot.pause()
    assert app.calls == ["kill"]


@drives_the_screen
async def test_a_disabled_button_still_shows_but_does_not_dispatch():
    app = _Harness()
    async with app.run_test() as pilot:
        disabled = [Action("h", "Hand to Codex…", "hand_off", "error", enabled=False,
                           tooltip="waiting for the governor (S8)")]
        app.bar.set_actions(disabled)
        await pilot.pause()
        btn = app.query_one("Button#act-hand_off", Button)
        assert btn.disabled is True
        assert btn.tooltip == "waiting for the governor (S8)"
        await pilot.click("Button#act-hand_off")
        await pilot.pause()
    assert app.calls == []


@drives_the_screen
async def test_narrow_width_collapses_to_one_actions_button():
    app = _Harness()
    async with app.run_test(size=(65, 26)) as pilot:
        app.bar.set_actions(ROW_ACTIONS, collapsed=True)
        await pilot.pause()
        buttons = list(app.query(Button))
        assert len(buttons) == 1
        assert str(buttons[0].label) == "Actions… (Enter)"
        await pilot.click(buttons[0])
        await pilot.pause()
    assert app.calls == ["__actions__"]


@drives_the_screen
async def test_switching_rows_rebuilds_the_button_set():
    app = _Harness()
    async with app.run_test() as pilot:
        app.bar.set_actions(ROW_ACTIONS)
        await pilot.pause()
        assert len(list(app.query(Button))) == 3
        app.bar.set_actions([Action("c", "Copy id", "copy_id")])
        await pilot.pause()
        buttons = list(app.query(Button))
        assert len(buttons) == 1 and str(buttons[0].label) == "Copy id (c)"
        app.bar.set_actions([])
        await pilot.pause()
        assert app.bar.display is False
