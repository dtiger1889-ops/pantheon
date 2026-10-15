"""Restarting a killed session: which sessions count as killed, how SESSIONS draws them, and
the agents table's `s` button. No real tmux or transcript is touched."""
from __future__ import annotations

from pantheon.models import AgentState, AgentStatus, Event
from pantheon.session_view import models as session_models
from pantheon.session_view import recent as recent_mod
from pantheon.session_view.models import SessionEntry
from pantheon.supervisor import pane as pane_mod
from pantheon.supervisor.pane import toolbar_actions_for
from pantheon.supervisor.rail import KILLED_GLYPH
from tests.test_rail import CWD_A, run_sidebar


def ev(ts, event, sid, source="claude", detail=None):
    return Event(ts=ts, event=event, source=source, session_id=sid, detail=detail)


def test_k_and_a_dropped_window_both_count_as_killed_until_it_starts_again():
    events = [
        ev("2026-10-06T22:00:00Z", "SessionStart", "a"),
        ev("2026-10-06T22:01:00Z", "kill", "a", source="pantheon"),
        ev("2026-10-06T22:00:00Z", "SessionStart", "b"),
        ev("2026-10-06T22:23:27Z", "SessionEnd", "b", detail="other"),   # its tmux went away
        ev("2026-10-06T22:00:00Z", "SessionStart", "c"),
        ev("2026-10-06T22:05:00Z", "SessionEnd", "c", detail="clear"),   # /clear is not a kill
        ev("2026-10-06T22:00:00Z", "SessionStart", "d"),
        ev("2026-10-06T22:06:00Z", "kill", "d", source="pantheon"),
        ev("2026-10-06T22:06:01Z", "SessionEnd", "d", detail="other"),
        ev("2026-10-06T22:09:00Z", "SessionStart", "d"),                 # restarted since
    ]
    killed = recent_mod.killed_sessions(events)
    assert killed == {"a": "2026-10-06T22:01:00Z", "b": "2026-10-06T22:23:27Z"}


def test_a_claude_p_run_is_never_marked_killed(tmp_path):
    transcript = tmp_path / "x.jsonl"
    transcript.write_text('{"type":"user","entrypoint":"sdk-cli"}\n', encoding="utf-8")
    entry = SessionEntry(session_id="p1", group=session_models.RECENT, project="p", cwd=CWD_A,
                         title="t", transcript_path=str(transcript))
    assert recent_mod._mark_killed(entry, {"p1": "2026-10-06T22:00:00Z"}).killed_at is None
    transcript.write_text('{"type":"user","entrypoint":"cli"}\n', encoding="utf-8")
    recent_mod._entrypoint_cache.clear()
    assert recent_mod._mark_killed(entry, {"p1": "2026-10-06T22:00:00Z"}).killed_at == "2026-10-06T22:00:00Z"


def test_sessions_lists_a_killed_one_first_and_says_click_to_restart(tmp_path):
    entries = [
        SessionEntry(session_id=f"old{i}", group=session_models.RECENT, project="garden_notes", cwd=CWD_A,
                     title=f"finished {i}", modified_ts="2026-10-06T21:00:00.000Z")
        for i in range(4)
    ] + [SessionEntry(session_id="dead1", group=session_models.RECENT, project="garden_notes", cwd=CWD_A,
                      title="the one that died", modified_ts="2026-10-01T00:00:00.000Z",
                      killed_at="2026-10-06T22:23:27Z")]
    captured = run_sidebar(tmp_path, entries=entries)
    sidebar = captured["app"].sidebar
    sessions = [s[1].session_id for s in sidebar._items if s[0] == "session"]
    assert sessions[0] == "dead1"                    # first under its project, never under `…N more`
    text = sidebar._row_text(next(s for s in sidebar._items if s[0] == "session")).plain
    assert KILLED_GLYPH in text and "the one that died" in text and "click restarts" in text


def _gone(provider="claude", last_action="killed from the deck"):
    return AgentState(session_id="s1", provider=provider, status=AgentStatus.GONE, project="garden_notes",
                      cwd=CWD_A, last_action=last_action)


def test_a_killed_claude_row_offers_restart_killed():
    actions = toolbar_actions_for(_gone(), {"claude", "codex"})
    restart = next(a for a in actions if a.action_name == "restart_session")
    assert restart.key == "s" and restart.label == pane_mod.RESTART_KILLED_LABEL and restart.enabled


def test_an_ended_claude_row_offers_plain_restart_and_codex_gets_none():
    actions = toolbar_actions_for(_gone(last_action="session ended"), {"claude"})
    assert next(a for a in actions if a.action_name == "restart_session").label == "↻ Restart"
    codex = toolbar_actions_for(_gone(provider="codex"), {"claude", "codex"})
    assert not any(a.action_name == "restart_session" for a in codex)
