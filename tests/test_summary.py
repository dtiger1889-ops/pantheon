"""The compact-tile summary logic (pantheon/session_view/summary.py): status + what it's doing,
and the latest message -. Pure functions, no terminal."""
from __future__ import annotations

from pantheon.session_view import models as m
from pantheon.session_view import summary as s


def entry(**over) -> m.SessionEntry:
    kw = dict(session_id="x", group=m.LIVE, project="proj", cwd="C:/x/proj", title="t",
              status_text="working")
    kw.update(over)
    return m.SessionEntry(**kw)


def test_status_word_prefers_needs_you_then_the_label_then_idle():
    assert s.status_word(entry(needs_human=True)) == "needs you"
    assert s.status_word(entry(status_text="running")) == "running"
    assert s.status_word(entry(status_text="")) == "idle"


def test_status_token_maps_state_to_a_colour_role():
    assert s.status_token(entry(needs_human=True)) == "warning"
    assert s.status_token(entry(style="error")) == "error"
    assert s.status_token(entry(style="muted")) == "muted"
    assert s.status_token(entry()) == "accent"


def test_activity_detail_names_a_running_tool_then_thinking_then_nothing():
    running = m.Conversation(session_id="s", path="p", items=(
        m.Item(kind=m.TOOL, uuid="t",
               tool=m.ToolCall(id="1", name="Bash", summary="Bash x", result_text=None)),))
    assert s.activity_detail(running) == "running Bash x"

    thinking = m.Conversation(session_id="s", path="p", items=(m.Item(kind=m.THINKING, text="hmm"),))
    assert s.activity_detail(thinking) == "thinking"

    msg = m.Conversation(session_id="s", path="p", items=(m.Item(kind=m.ASSISTANT, text="done"),))
    assert s.activity_detail(msg) == ""
    assert s.activity_detail(None) == ""


def test_latest_exchange_pairs_the_last_ask_with_the_reply():
    conv = m.Conversation(session_id="s", path="p", items=(
        m.Item(kind=m.USER, text="first"),
        m.Item(kind=m.ASSISTANT, text="reply one"),
        m.Item(kind=m.USER, text="second"),
        m.Item(kind=m.ASSISTANT, text="reply two"),
    ))
    you, it = s.latest_exchange(conv)
    assert you == "second" and it == "reply two"


def test_latest_exchange_shows_the_ask_alone_when_no_reply_has_come_yet():
    conv = m.Conversation(session_id="s", path="p", items=(
        m.Item(kind=m.ASSISTANT, text="reply one"),
        m.Item(kind=m.USER, text="waiting on this"),
    ))
    you, it = s.latest_exchange(conv)
    assert you == "waiting on this" and it is None


def test_latest_exchange_is_empty_for_no_conversation():
    assert s.latest_exchange(None) == (None, None)
    assert s.latest_exchange(m.Conversation(session_id="s", path="p")) == (None, None)


def test_clip_trims_on_a_word_boundary_with_an_ellipsis():
    assert s.clip("short", 100) == "short"
    out = s.clip("one two three four five", 12)
    assert out.endswith("…") and len(out) <= 12 and " " in out
