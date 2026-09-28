"""The session tile and its conversation view, driven
headlessly by Textual's own pilot -- same shape as `test_rail.py` / `test_supervisor_app.py`.

Everything here is built by hand out of `session_view/models.py`: this package consumes the
seam, never the parser, so these tests stay green whatever the parser does.

The load-bearing assertions are the user's rejection criteria: "still looks like a
terminal table, not a chat" (every kind draws), "verbose bash/tool output leaks into view" (a
tool result is NOT on screen until its chip is opened).
"""
from __future__ import annotations

import asyncio
import html
import re

from textual.app import App, ComposeResult
from textual.widgets import Collapsible, Input, Static

from pantheon import tmuxctl
from pantheon.session_view import models as m
from pantheon.session_view.conversation import (
    NO_ITEMS_TEXT,
    RUNNING_TEXT,
    TILE_ITEMS_COMPACT,
    TILE_ITEMS_EXPANDED,
    ConversationView,
)
from pantheon.session_view.tile import (
    INPUT_PLACEHOLDER,
    NEEDS_YOU_TEXT,
    READ_ONLY_CODEX,
    READ_ONLY_DESKTOP,
    SessionTile,
    context_percent,
    header_caption,
)

NBSP = "\u00a0"
SIZE = (200, 50)

TOOL_RESULT = "ZZQQ_secret_bash_output_that_must_stay_folded"
ASSISTANT_TEXT = "Rebuilt the launcher and the smoke test passes."
USER_TEXT = "check the launcher"
THINKING_TEXT = "WWQQ_private_reasoning"
SYSTEM_LINE = "hook: pantheon_event"
SUBAGENT_LINE = "fix the launcher"


def screen_text(svg: str) -> str:
    """The words a person would read off the rendered screen, with the SVG markup taken away
    (the technique `tests/test_supervisor_app.py` established)."""
    runs = re.findall(r"<text[^>]*>(.*?)</text>", svg, flags=re.S)
    return html.unescape("".join(runs)).replace(NBSP, " ")


# ---------------------------------------------------------------- fixtures, built by hand

def tool_item(uuid="t1", summary="Bash git status --short", result=TOOL_RESULT, is_error=False):
    return m.Item(
        kind=m.TOOL,
        uuid=uuid,
        tool=m.ToolCall(
            id="toolu_" + uuid,
            name="Bash",
            summary=summary,
            input_text="git status --short",
            result_text=result,
            is_error=is_error,
        ),
    )


def every_kind() -> m.Conversation:
    return m.Conversation(
        session_id="aaa11111",
        path="/dev/null",
        title="Fix the launcher",
        model="claude-opus-5[1m]",
        effort="high",
        context_tokens=430_000,
        items=(
            m.Item(kind=m.USER, uuid="u1", text=USER_TEXT),
            m.Item(kind=m.THINKING, uuid="k1", text=THINKING_TEXT),
            tool_item(),
            m.Item(kind=m.ASSISTANT, uuid="a1", text=ASSISTANT_TEXT),
            m.Item(kind=m.SYSTEM, uuid="s1", text=SYSTEM_LINE, detail="PreToolUse ok"),
            m.Item(kind=m.SUBAGENT, uuid="g1", text=SUBAGENT_LINE, detail="ran 12 turns",
                   sidechain=True),
        ),
    )


def live_entry(**over) -> m.SessionEntry:
    kw = dict(
        session_id="aaa11111",
        group=m.LIVE,
        project="habit_notes",
        cwd="C:/Home/x/Documents/Projects/habit_notes",
        title="Fix the launcher",
        status_text="working",
        window_index=3,
        tmux_session="pantheon",
        transcript_path="/dev/null",
    )
    kw.update(over)
    return m.SessionEntry(**kw)


class _TileApp(App):
    def __init__(self, entry, conv, expanded):
        super().__init__()
        self.tile = SessionTile(entry, conv, expanded=expanded, id="tile")

    def compose(self) -> ComposeResult:
        yield self.tile


def run_tile(entry=None, conv=None, expanded=True, drive=None, size=SIZE, inspect=None):
    """Mount one tile, optionally run `drive(pilot, app)`, and hand back what it drew.

    `inspect(app)` runs while the app is still alive and its return value lands in `out["seen"]`
    -- a widget query after `run_test()` has exited finds nothing, because Textual tears the
    node tree down on shutdown.
    """
    app = _TileApp(entry or live_entry(), conv if conv is not None else every_kind(), expanded)
    out = {}

    async def go():
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            if drive is not None:
                await drive(pilot, app)
                await pilot.pause()
            out["screen"] = screen_text(app.export_screenshot())
            out["seen"] = inspect(app) if inspect is not None else None
            out["classes"] = set(app.tile.classes)

    asyncio.run(go())
    return out


# ---------------------------------------------------------------- the pure helpers

def test_context_percent_measures_claude_against_the_1m_window():
    # Every Claude model id's statusline reports a 1M window on this box; the old
    # 200k default turned a 624k-token claude-opus-5-5 turn into `ctx 312%`.
    small = m.Conversation(session_id="s", path="p", model="claude-sonnet-4-6",
                           context_tokens=100_000)
    opus55 = m.Conversation(session_id="s", path="p", model="claude-opus-5-5",
                            context_tokens=624_000)
    assert context_percent(small) == 10
    assert context_percent(opus55) == 62
    assert context_percent(m.Conversation(session_id="s", path="p")) is None


def test_context_percent_prefers_the_sessions_own_statusline_window():
    conv = m.Conversation(session_id="s", path="p", model="claude-opus-5-5", context_tokens=100_000)
    statusline = {"context_window": {"context_window_size": 200_000, "used_percentage": 49}}
    assert context_percent(conv, statusline) == 50
    # no usage in the transcript yet -> the statusline's own percentage
    empty = m.Conversation(session_id="s", path="p", model="claude-opus-5-5")
    assert context_percent(empty, statusline) == 49


def test_context_never_prints_over_100_percent_it_shows_tokens_instead():
    from pantheon.session_view import context as context_mod
    # a Codex window is ~258k: 300k tokens means the guess is wrong for this model
    assert context_mod.context_percent(300_000, "gpt-6-astra") is None
    assert context_mod.context_caption(300_000, "gpt-6-astra") == "ctx 300k"
    assert context_mod.context_caption(1_250_000, "claude-opus-5-5") == "ctx 1.2M"
    assert context_mod.context_caption(624_000, "claude-opus-5-5") == "ctx 62%"
    assert context_mod.context_caption(0, "claude-opus-5-5") is None


def test_header_caption_drops_what_it_does_not_know():
    entry = live_entry()
    assert header_caption(entry, None) == "habit_notes"
    caption = header_caption(entry, every_kind())
    assert caption == "habit_notes · claude-opus-5[1m] · high · ctx 43%"


# ---------------------------------------------------------------- what reaches the screen

def test_every_kind_of_item_renders():
    screen = run_tile()["screen"]
    assert "you › " + USER_TEXT in screen          # his own message, in its band
    assert ASSISTANT_TEXT in screen                # the assistant's prose
    assert "Bash git status --short" in screen     # the tool call, as a one-line chip
    assert "Thinking" in screen                    # folded thinking
    assert SYSTEM_LINE in screen                   # the dim hook receipt one-liner
    assert "subagent: " + SUBAGENT_LINE in screen  # the sidechain, folded
    assert "Fix the launcher" in screen            # the header title
    assert "habit_notes" in screen and "ctx 43%" in screen


def test_tool_result_is_hidden_until_the_chip_is_opened():
    """The user's rejection criterion: "verbose bash/tool output leaks into view"."""
    before = run_tile()["screen"]
    assert TOOL_RESULT not in before
    assert THINKING_TEXT not in before

    async def open_them(pilot, app):
        for col in app.query(Collapsible):
            col.collapsed = False

    after = run_tile(drive=open_them)["screen"]
    assert TOOL_RESULT in after
    assert THINKING_TEXT in after


def test_a_tool_still_running_says_so_instead_of_showing_nothing():
    conv = m.Conversation(session_id="s", path="p",
                          items=(tool_item(result=None),))

    async def open_them(pilot, app):
        for col in app.query(Collapsible):
            col.collapsed = False

    screen = run_tile(conv=conv, drive=open_them)["screen"]
    assert RUNNING_TEXT in screen


def test_a_failed_tool_says_the_word_error_not_just_a_colour():
    """S-UX R5: colour is never the only signal."""
    conv = m.Conversation(session_id="s", path="p",
                          items=(tool_item(summary="Bash pytest -q", is_error=True),))
    out = run_tile(conv=conv,
                   inspect=lambda app: app.query_one(Collapsible).has_class("cv-tool-error"))
    assert "error" in out["screen"]
    assert out["seen"] is True


def test_an_empty_conversation_says_so():
    conv = m.Conversation(session_id="s", path="p")
    assert NO_ITEMS_TEXT in run_tile(conv=conv)["screen"]


# ---------------------------------------------------------------- the message box

def test_enter_in_the_send_box_types_into_that_sessions_tmux_window(monkeypatch):
    calls = []
    monkeypatch.setattr(tmuxctl, "send_text",
                        lambda target, text, **kw: calls.append((target, text)) or True)

    async def type_and_send(pilot, app):
        box = app.query_one("#tile-input", Input)
        box.focus()
        await pilot.pause()
        await pilot.press(*"hello")
        await pilot.press("enter")

    out = run_tile(drive=type_and_send,
                   inspect=lambda app: app.query_one("#tile-input", Input).value)
    assert calls == [("pantheon:3", "hello")]
    assert out["seen"] == ""


def test_a_desktop_session_gets_a_read_only_line_and_no_send_box():
    entry = live_entry(window_index=None)
    out = run_tile(entry=entry, inspect=lambda app: len(app.query(Input)))
    assert READ_ONLY_DESKTOP in out["screen"]
    assert out["seen"] == 0


def test_a_codex_job_names_itself_in_the_read_only_line():
    entry = live_entry(window_index=None, provider="codex")
    assert READ_ONLY_CODEX in run_tile(entry=entry)["screen"]


def test_a_compact_tile_hides_the_send_box():
    out = run_tile(expanded=False)
    assert "compact" in out["classes"]
    assert INPUT_PLACEHOLDER not in out["screen"]


def test_needs_you_band_shows_only_when_the_entry_says_so():
    assert NEEDS_YOU_TEXT not in run_tile()["screen"]
    assert NEEDS_YOU_TEXT in run_tile(entry=live_entry(needs_human=True))["screen"]


# ---------------------------------------------------------------- the refresh diff

def test_appending_one_item_mounts_exactly_one_new_child():
    conv = every_kind()
    counts = {}

    async def append_one(pilot, app):
        view = app.query_one("#tile-conv", ConversationView)
        counts["before"] = len(view.children)
        grown = m.Conversation(
            session_id=conv.session_id, path=conv.path, title=conv.title, model=conv.model,
            effort=conv.effort, context_tokens=conv.context_tokens,
            items=conv.items + (m.Item(kind=m.ASSISTANT, uuid="a2", text="and pushed it."),),
        )
        app.tile.set_conversation(grown)
        await pilot.pause()
        counts["after"] = len(view.children)

    out = run_tile(conv=conv, drive=append_one)
    assert counts["after"] == counts["before"] + 1
    assert "and pushed it." in out["screen"]


def test_a_tool_result_arriving_updates_the_same_collapsible_in_place():
    conv = m.Conversation(session_id="s", path="p", items=(tool_item(result=None),))
    seen = {}

    async def deliver_result(pilot, app):
        view = app.query_one("#tile-conv", ConversationView)
        seen["before"] = len(view.children)
        col = app.query_one(Collapsible)
        col.collapsed = False
        app.tile.set_conversation(
            m.Conversation(session_id="s", path="p", items=(tool_item(result=TOOL_RESULT),))
        )
        await pilot.pause()
        seen["after"] = len(view.children)
        seen["same_widget"] = app.query_one(Collapsible) is col
        seen["still_open"] = not app.query_one(Collapsible).collapsed

    screen = run_tile(conv=conv, drive=deliver_result)["screen"]
    assert seen["after"] == seen["before"]       # no rebuild
    assert seen["same_widget"] and seen["still_open"]
    assert TOOL_RESULT in screen
    assert RUNNING_TEXT not in screen


def test_two_hundred_items_render_without_error():
    items = tuple(
        m.Item(kind=m.ASSISTANT, uuid=f"a{n}", text=f"turn {n}") for n in range(200)
    )
    out = run_tile(conv=m.Conversation(session_id="s", path="p", items=items),
                   inspect=lambda app: len(app.query_one("#tile-conv", ConversationView).children))
    assert "turn 199" in out["screen"]            # the tail is what a chat shows
    assert out["seen"] == 200


def test_a_compact_tile_shows_status_and_the_latest_message_not_the_transcript():
    """: the compact pit tile is a readable chat row -- status + what it's
    doing + the latest message -- NOT the verbose transcript (which he called worthless)."""
    screen = run_tile(expanded=False)["screen"]
    assert "working" in screen                     # the status word
    assert ASSISTANT_TEXT in screen                # the latest message, readable
    assert "you " in screen and USER_TEXT in screen  # the preceding ask, for context
    # the transcript's verbose chips are NOT in the compact card
    assert "Bash git status --short" not in screen
    assert "subagent:" not in screen
    assert "Thinking" not in screen


def test_a_compact_tile_names_what_it_is_doing_when_a_tool_is_running():
    conv = m.Conversation(session_id="s", path="p", items=(
        m.Item(kind=m.USER, uuid="u", text="go"),
        tool_item(result=None, summary="Bash pytest -q"),
    ))
    screen = run_tile(conv=conv, expanded=False)["screen"]
    assert "running" in screen and "Bash pytest -q" in screen


def test_a_compact_tile_with_no_messages_says_so():
    conv = m.Conversation(session_id="s", path="p")
    assert NO_ITEMS_TEXT in run_tile(conv=conv, expanded=False)["screen"]


def test_page_up_at_the_top_loads_earlier_items():
    # PageUp/Home loading earlier items lives in the EXPANDED transcript now (a compact tile has
    # no transcript to page through). 250 items > the expanded window, so there are earlier ones.
    items = tuple(
        m.Item(kind=m.ASSISTANT, uuid=f"a{n}", text=f"turn {n}") for n in range(250)
    )
    grown = {}

    async def page_up(pilot, app):
        view = app.query_one("#tile-conv", ConversationView)
        view.focus()
        await pilot.pause()
        view.scroll_home(animate=False)
        await pilot.pause()
        await pilot.press("home")
        await pilot.pause()
        grown["children"] = len(view.children)

    run_tile(conv=m.Conversation(session_id="s", path="p", items=items), expanded=True,
             drive=page_up)
    assert grown["children"] > TILE_ITEMS_EXPANDED


def test_the_header_is_the_title_left_and_the_caption_right():
    out = run_tile(inspect=lambda app: (
        str(app.query_one("#tile-title", Static).content),
        str(app.query_one("#tile-caption", Static).content),
    ))
    title, caption = out["seen"]
    assert title == "Fix the launcher"
    assert caption == "habit_notes · claude-opus-5[1m] · high · ctx 43%"


def test_context_window_knows_fable_and_codex_without_a_marker():
    from pantheon.session_view.tile import context_window_for
    assert context_window_for("claude-fable-5-1") == 1_000_000
    assert context_window_for("claude-opus-5[1m]") == 1_000_000
    assert context_window_for("claude-opus-5") == 1_000_000   # statusline says 1M
    assert context_window_for("claude-opus-5-5") == 1_000_000
    assert context_window_for("gpt-6-astra") == 258_400
    assert context_window_for(None) == 1_000_000
