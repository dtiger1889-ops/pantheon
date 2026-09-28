"""The supervisor state machine, with no tmux and no clock of its own.

Every rule in  gets a case here: what each hook event does to a row, how a
subagent line is not mistaken for the session finishing, how a Codex job moves from queued to a
non-zero exit, how rows are matched to live tmux windows, when a finished row disappears, and the
order they end up in.
"""
import json
from datetime import datetime, timedelta, timezone

from pantheon.events import read_events
from pantheon.models import AgentState, AgentStatus, Event, TmuxWindow
from pantheon.supervisor import state as st

NOW = datetime(2026, 9, 1, 20, 30, 0, tzinfo=timezone.utc)
ROOT = "C:/Home/x/Documents/Projects"
HINTFORGE = r"C:\Home\x\Documents\Projects\hiking_log_v2"
PMBOT = r"C:\Home\x\Documents\Projects\Plumb"


def ts(minutes_ago: float) -> str:
    t = NOW - timedelta(minutes=minutes_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def ev(**kw) -> Event:
    kw.setdefault("source", "claude")
    return Event.from_dict(kw)


def win(index, name, command, path, pane_id, session="pantheon") -> TmuxWindow:
    return TmuxWindow(index, name, command, path, pane_id, session)


def only(rows, session_id) -> AgentState:
    matches = [r for r in rows if r.session_id == session_id]
    assert matches, f"no row for {session_id} in {[r.session_id for r in rows]}"
    return matches[0]


# Twelve-plus fixture events covering every transition the spec names.
FIXTURE = [
    # s1: start -> tool -> permission prompt (still blocked at the end)
    ev(ts=ts(9), event="SessionStart", session_id="s1", cwd=HINTFORGE, tmux_pane="%3", detail="startup"),
    ev(ts=ts(8), event="PostToolUse", session_id="s1", cwd=HINTFORGE, tool_name="Read", tmux_pane="%3"),
    ev(ts=ts(2), event="Notification", session_id="s1", cwd=HINTFORGE, tmux_pane="%3",
       notification_type="permission_prompt", message="Claude needs your permission to use Bash"),
    # s2: stop, then a tool again -> back to working
    ev(ts=ts(7), event="Stop", session_id="s2", cwd=PMBOT),
    ev(ts=ts(6), event="PostToolUse", session_id="s2", cwd=PMBOT, tool_name="Edit"),
    # s3: idle at the prompt
    ev(ts=ts(5), event="Notification", session_id="s3", cwd=ROOT + "/dune_tracker",
       notification_type="idle_prompt", message="Claude is waiting for your input"),
    # s4: a subagent fired a Stop hook -- the parent session is still working
    ev(ts=ts(4), event="PostToolUse", session_id="s4", cwd=ROOT + "/Canvas", tool_name="Task"),
    ev(ts=ts(3), event="Stop", session_id="s4", cwd=ROOT + "/Canvas", agent_id="sub-7"),
    # s5: a usage-limit notice must not change what the agent was doing
    ev(ts=ts(11), event="PostToolUse", session_id="s5", cwd=ROOT + "/habit_notes", tool_name="Grep"),
    ev(ts=ts(10), event="Notification", session_id="s5", cwd=ROOT + "/habit_notes",
       notification_type="quota_auto_resume_scheduled", message="usage limit reached"),
    # s6: an agent that asked for input by name
    ev(ts=ts(1), event="Notification", session_id="s6", cwd=ROOT + "/bread_recipe_box",
       notification_type="agent_needs_input", message="Which branch should I use?"),
    # s7: session ended a moment ago -- gone, still on screen
    ev(ts=ts(30), event="SessionStart", session_id="s7", cwd=ROOT + "/sandbox_try"),
    ev(ts=ts(3), event="SessionEnd", session_id="s7", cwd=ROOT + "/sandbox_try", detail="clear"),
    # s8: session ended long ago -- hidden
    ev(ts=ts(45), event="SessionEnd", session_id="s8", cwd=ROOT + "/Gazebo"),
    # s9: killed from the deck
    ev(ts=ts(2), event="kill", source="pantheon", session_id="s9", cwd=ROOT + "/Apps"),
    # j1: a Codex job that ran and failed
    ev(ts=ts(9), event="queued", source="codex", job_id="j1", project="Plumb"),
    ev(ts=ts(8), event="running", source="codex", job_id="j1", project="Plumb"),
    ev(ts=ts(6), event="done", source="codex", job_id="j1", project="Plumb", detail="exit 2"),
    # j2: a Codex job that ran clean
    ev(ts=ts(5), event="running", source="codex", job_id="j2", project="Canvas"),
    ev(ts=ts(4), event="done", source="codex", job_id="j2", project="Canvas", detail="exit 0"),
]

WINDOWS = [
    win(0, "deck", "python", "C:/Home/x/Documents/Projects/project_lanterns", "%0"),
    win(1, "hikinglog", "node", "C:/Home/x/Documents/Projects/hiking_log_v2", "%3"),
    win(2, "plumb", "node", "C:/Home/x/Documents/Projects/Plumb", "%4"),
    win(7, "stray", "claude", "C:/Home/x/Documents/Projects/pottery_studios", "%9", session="other"),
]


def folded(events=None, windows=None, now=NOW, statusline_mtimes=None):
    return st.fold(
        list(FIXTURE if events is None else events),
        list(WINDOWS if windows is None else windows),
        now,
        "pantheon",
        ROOT,
        statusline_mtimes=statusline_mtimes,
    )


def epoch_ago(minutes: float) -> float:
    """A `state/statusline/<id>.json` mtime, `minutes` before `NOW` (epoch seconds)."""
    return (NOW - timedelta(minutes=minutes)).timestamp()


# ------------------------------------------------------------------ state machine


def test_permission_prompt_blocks_and_keeps_the_message():
    row = only(folded(), "s1")
    assert row.status is AgentStatus.BLOCKED_PERMISSION
    assert row.status.label == "blocked - permission"
    assert row.needs_human
    assert row.last_action.startswith("Claude needs your permission")
    assert row.project == "hiking_log_v2"


def test_tool_after_stop_returns_to_working():
    row = only(folded(), "s2")
    assert row.status is AgentStatus.WORKING
    assert row.last_action == "Edit"


def test_stop_alone_waits_for_the_user():
    rows = folded(events=[FIXTURE[3]], windows=[])
    assert only(rows, "s2").status is AgentStatus.WAITING_INPUT
    assert only(rows, "s2").last_action == "turn done"


def test_session_start_is_working():
    # A live pane matching the recorded tmux_pane, so the new dead-pane presence rule (a recorded
    # pane with no live window disappears fast, DEAD_PANE_SECONDS) never fires here -- the pane
    # this session started in is right there, as it would be in reality.
    rows = folded(events=[FIXTURE[0]], windows=[win(1, "hikinglog", "node", HINTFORGE, "%3")])
    assert only(rows, "s1").status is AgentStatus.WORKING
    assert only(rows, "s1").last_action == "session started"


def test_idle_prompt_is_idle():
    assert only(folded(), "s3").status is AgentStatus.IDLE


def test_agent_needs_input_waits():
    row = only(folded(), "s6")
    assert row.status is AgentStatus.WAITING_INPUT
    assert row.last_action == "Which branch should I use?"


def test_subagent_line_keeps_the_session_working():
    row = only(folded(), "s4")
    assert row.status is AgentStatus.WORKING
    assert row.last_action.startswith("subagent:")


def test_usage_limit_notice_keeps_the_prior_state():
    row = only(folded(), "s5")
    assert row.status is AgentStatus.WORKING          # it was running Grep and still is
    assert row.last_action == "limit: auto resume scheduled"


def test_session_end_is_gone_then_hidden_after_ten_minutes():
    rows = folded()
    assert only(rows, "s7").status is AgentStatus.GONE   # ended 3 minutes ago, still shown
    assert not [r for r in rows if r.session_id == "s8"]  # ended 45 minutes ago, hidden


def test_deck_kill_marks_it_gone():
    row = only(folded(), "s9")
    assert row.status is AgentStatus.GONE
    assert row.last_action == "killed from the deck"


def test_deck_wind_down_park_and_dispatch():
    rows = folded(
        events=[
            ev(ts=ts(1), event="wind_down", source="pantheon", session_id="g1", cwd=ROOT + "/Plumb"),
            ev(ts=ts(1), event="park", source="pantheon", session_id="g2", cwd=ROOT + "/Plumb"),
            ev(ts=ts(1), event="dispatch", source="pantheon", session_id="g3", cwd=ROOT + "/Plumb"),
        ],
        windows=[],
    )
    assert only(rows, "g1").status is AgentStatus.WINDING_DOWN
    assert only(rows, "g2").status is AgentStatus.PARKED
    assert only(rows, "g3").status is AgentStatus.WORKING


def test_governor_resume_rung_four_is_resume_failed_needs_the_user():
    rows = folded(
        events=[
            ev(ts=ts(10), event="park", source="pantheon", session_id="g4", cwd=ROOT + "/Plumb"),
            ev(ts=ts(1), event="resume", source="pantheon", session_id="g4", cwd=ROOT + "/Plumb", rung=4),
        ],
        windows=[],
    )
    row = only(rows, "g4")
    assert row.status is AgentStatus.RESUME_FAILED
    assert row.status.label == "resume failed"
    assert row.needs_human is True


def test_governor_resume_below_rung_four_goes_back_to_working():
    rows = folded(
        events=[
            ev(ts=ts(10), event="park", source="pantheon", session_id="g5", cwd=ROOT + "/Plumb"),
            ev(ts=ts(1), event="resume", source="pantheon", session_id="g5", cwd=ROOT + "/Plumb", rung=1),
        ],
        windows=[],
    )
    assert only(rows, "g5").status is AgentStatus.WORKING


def test_governor_handoff_leaves_the_old_row_exactly_as_it_was():
    rows = folded(
        events=[
            ev(ts=ts(10), event="Notification", session_id="g6", cwd=ROOT + "/Plumb",
               notification_type="permission_prompt", message="needs your permission"),
            ev(ts=ts(1), event="handoff", source="pantheon", session_id="g6", cwd=ROOT + "/Plumb",
               message="claude is working in window 9"),
        ],
        windows=[],
    )
    row = only(rows, "g6")
    assert row.status is AgentStatus.BLOCKED_PERMISSION
    assert row.last_action == "needs your permission"  # the hand-off's own message never overwrites it


def test_codex_job_lifecycle_and_non_zero_exit():
    rows = folded()
    failed = only(rows, "j1")
    assert failed.status is AgentStatus.FAILED
    assert failed.provider == "codex"
    assert failed.last_action == "codex exec exit 2"
    assert failed.project == "Plumb"
    clean = only(rows, "j2")
    assert clean.status is AgentStatus.DONE
    assert clean.last_action == "codex exec exit 0"
    partial = folded(events=[FIXTURE[15], FIXTURE[16]], windows=[])
    assert only(partial, "j1").status is AgentStatus.RUNNING
    assert only(folded(events=[FIXTURE[15]], windows=[]), "j1").status is AgentStatus.QUEUED


# ------------------------------------------------------------------ windows and presence


def test_window_matched_by_pane_id_first():
    # s1 recorded pane %3; the decoy window shares the folder but a different pane id.
    decoy = win(5, "decoy", "node", "C:/Home/x/Documents/Projects/hiking_log_v2", "%8")
    row = only(folded(windows=WINDOWS + [decoy]), "s1")
    assert row.window_index == 1
    assert row.tmux_pane == "%3"
    assert row.in_pantheon is True


def test_a_claude_session_with_no_pane_is_never_matched_by_folder():
    """s2's events carry no tmux_pane, only a cwd: a Claude session outside tmux (the Desktop
    app). The node window in the same folder is some other session's, so s2 gets no window and
    that window keeps its own (pre-hook) row."""
    rows = folded()
    row = only(rows, "s2")
    assert row.window_index is None
    assert only(rows, "pane:%4").window_index == 2


def test_a_codex_job_with_no_pane_is_matched_by_folder():
    job = [ev(ts=ts(1), event="running", source="codex", session_id="cx1", job_id="cx1",
              cwd="C:/Home/x/Documents/Projects/Plumb")]
    row = only(folded(events=job), "cx1")
    assert row.window_index == 2
    assert row.in_pantheon is True


def test_row_outside_the_pantheon_session_is_flagged():
    outside = [ev(ts=ts(1), event="PostToolUse", session_id="x1", tmux_pane="%9",
                  cwd="C:/Home/x/Documents/Projects/pottery_studios", tool_name="Read")]
    row = only(folded(events=outside), "x1")
    assert row.window_index == 7
    assert row.in_pantheon is False


def test_a_desktop_session_in_the_workspace_folder_never_takes_the_assistant_window():
    """Live 2026-09-27 01:08: Desktop-app session f95f1d79 (cwd = the workspace root, no pane,
    SessionEnd `other`) was drawn as `gone · workspace · pantheon:5 · session ended` -- pantheon:5
    is the pinned Assistant (pane %17, session 5f40ea10, alive) -- and, being the newest event on
    that window, took the window away from the Assistant's own row."""
    ws = r"C:\Home\x\Documents\Projects"
    events = [
        ev(ts=ts(90), event="SessionStart", session_id="5f40ea10", cwd=ws, tmux_pane="%17", detail="startup"),
        ev(ts=ts(80), event="Stop", session_id="5f40ea10", cwd=ws, tmux_pane="%17"),
        ev(ts=ts(20), event="PostToolUse", session_id="f95f1d79", cwd=ws, tool_name="Read", tmux_pane=None),
        ev(ts=ts(2), event="SessionEnd", session_id="f95f1d79", cwd=ws, detail="other", tmux_pane=None),
    ]
    windows = [
        win(0, "DECK", "bash", "C:/Home/x/Documents/Projects/project_lanterns", "%0"),
        win(4, "loom-os", r"C:\Home\x\.local\bin\claude.exe", "C:/Home/x/Documents/Projects/loom-os", "%7"),
        win(5, "ASSISTANT", r"C:\Home\x\.local\bin\claude.exe", "/c/Home/x/Documents/Projects", "%17"),
    ]
    rows = folded(events=events, windows=windows)
    desktop = only(rows, "f95f1d79")
    assert desktop.window_index is None and desktop.tmux_session is None
    assert desktop.status is AgentStatus.GONE
    assistant = only(rows, "5f40ea10")
    assert assistant.window_index == 5 and assistant.status is not AgentStatus.GONE
    assert not [r for r in rows if r.session_id == "pane:%17"]   # no second row for the Assistant


def test_the_folder_fallback_never_lands_on_a_reserved_or_taken_pane():
    from pantheon.supervisor.state import match_window

    ws = "C:/Home/x/Documents/Projects"
    assistant = win(5, "ASSISTANT", "claude.exe", ws, "%17")
    other = win(6, "Claude", "node", ws, "%21")
    assert match_window(ws, None, [assistant], "pantheon", provider="codex") is None
    assert match_window(ws, None, [assistant, other], "pantheon", provider="codex", taken={"%21"}) is None
    assert match_window(ws, None, [assistant, other], "pantheon", provider="codex") is other
    assert match_window(ws, None, [assistant, other], "pantheon", provider="claude") is None
    # A Codex job event from the reserved pane list is kept off it too (fold's `reserved_panes`).
    job = [ev(ts=ts(1), event="running", source="codex", session_id="cx2", job_id="cx2", cwd=ws)]
    rows = st.fold(job, [other], NOW, "pantheon", ROOT, reserved_panes={"%21"})
    assert only(rows, "cx2").window_index is None


def test_unclaimed_agent_pane_becomes_an_unknown_row():
    extra = win(6, "codexjob", "codex", "C:/Home/x/Documents/Projects/comic_strip_drafts", "%6")
    rows = folded(windows=WINDOWS + [extra])
    row = only(rows, "pane:%6")
    assert row.status is AgentStatus.UNKNOWN
    assert row.provider == "codex"
    assert row.project == "comic_strip_drafts"
    assert row.last_action == "no events yet"
    # The python window running the deck itself is not an agent, so it gets no row.
    assert not [r for r in rows if r.session_id == "pane:%0"]


def test_stale_session_with_a_live_pane_is_still_present_but_goes_quiet():
    old = [ev(ts=ts(90), event="PostToolUse", session_id="s1", cwd=HINTFORGE,
              tool_name="Read", tmux_pane="%3")]
    row = only(folded(events=old), "s1")
    # 90 minutes quiet: the pane being alive keeps it on screen (not `gone`), but 90 minutes with
    # no event is well past QUIET_AFTER_SECONDS, so it is no longer counted as `working` either.
    assert row.status is AgentStatus.QUIET


def test_stale_session_with_no_pane_disappears():
    old = [ev(ts=ts(90), event="PostToolUse", session_id="zz", cwd=ROOT + "/nowhere", tool_name="Read")]
    assert folded(events=old, windows=[]) == []


# ------------------------------------------------------------------ dead-pane vs. desktop presence
#
# A tmux window killed outside the deck writes no SessionEnd, so before this fix a row that
# recorded a tmux_pane but had no live window matching it stayed on screen as its last real status
# ("working") for up to the full 30-minute PRESENCE_SECONDS window -- CHECKPOINT's "leftovers"
# thread names this as "desktop. working" for up to 30 minutes. The fix: a row that DID record a
# pane goes `gone` after DEAD_PANE_SECONDS (3 minutes) with no live window and no fresh event; a
# row that never had a pane at all (a true `desktop` session, e.g. Claude Desktop) keeps the old
# 30-minute rule, extended by a fresh statusline capture the same way a hook event would be.


def test_dead_pane_row_goes_gone_quickly_when_the_window_is_not_there():
    stale = [ev(ts=ts(5), event="PostToolUse", session_id="deadwin", cwd=HINTFORGE,
                tool_name="Read", tmux_pane="%9")]
    row = only(folded(events=stale, windows=[]), "deadwin")
    assert row.status is AgentStatus.GONE          # the window died 5 minutes ago; no live pane


def test_dead_pane_row_still_shows_its_real_status_within_three_minutes():
    fresh = [ev(ts=ts(2), event="PostToolUse", session_id="justdied", cwd=HINTFORGE,
                tool_name="Read", tmux_pane="%9")]
    row = only(folded(events=fresh, windows=[]), "justdied")
    assert row.status is AgentStatus.WORKING       # inside the DEAD_PANE_SECONDS grace period


def test_dead_pane_row_is_hidden_once_past_ten_minutes():
    very_stale = [ev(ts=ts(15), event="PostToolUse", session_id="longdead", cwd=HINTFORGE,
                     tool_name="Read", tmux_pane="%9")]
    assert folded(events=very_stale, windows=[]) == []   # gone, and past HIDE_GONE_AFTER_SECONDS


def test_desktop_row_with_no_tmux_pane_keeps_the_thirty_minute_presence_rule_but_goes_quiet():
    aging = [ev(ts=ts(25), event="PostToolUse", session_id="desk1", cwd=ROOT + "/Plumb", tool_name="Read")]
    row = only(folded(events=aging, windows=[]), "desk1")
    # No tmux pane at all -- the old 30-minute rule keeps it on screen (not `gone`) -- but 25
    # minutes of silence is well past QUIET_AFTER_SECONDS, so it is `quiet`, not `working`.
    assert row.status is AgentStatus.QUIET


def test_a_fresh_statusline_capture_counts_as_presence_for_a_desktop_row():
    """A Claude Desktop session is never in tmux (no `tmux_pane`), so its hook events are the only
    other sign of life -- except it rewrites its statusline file on every turn, which
    this fixture simulates as far newer than the last hook event."""
    old = [ev(ts=ts(35), event="PostToolUse", session_id="desk2", cwd=ROOT + "/Plumb", tool_name="Read")]
    mtimes = {"desk2": epoch_ago(1)}
    row = only(folded(events=old, windows=[], statusline_mtimes=mtimes), "desk2")
    assert row.status is AgentStatus.WORKING       # 35 minutes of hook silence, but the statusline
                                                    # was rewritten a minute ago


def test_a_statusline_capture_older_than_the_last_event_does_not_help():
    old = [ev(ts=ts(35), event="PostToolUse", session_id="desk3", cwd=ROOT + "/Plumb", tool_name="Read")]
    mtimes = {"desk3": epoch_ago(40)}              # even staler than the last hook event
    assert folded(events=old, windows=[], statusline_mtimes=mtimes) == []


# ------------------------------------------------------------------ quiet tier
#
# Three bugs from one live screenshot: (1) a session that fired `SessionStart` and nothing since
# shows `working` forever, inflating the "N working" pill; (2) a routine Desktop-app session that
# finished (`Stop`) holds the amber "needs you" pill forever, even though there is no tmux window
# to jump to or answer; (3) the command bar and the NEEDS YOU card disagreed because the card
# never actually saw the agent rows. All three are fixed in `state.fold`'s new `_apply_quiet`
# step plus wiring the card to the same rows the pill uses (`tests/test_deck_app.py` covers (3)).


def test_session_start_with_nothing_since_goes_quiet_after_ten_minutes():
    """The exact bug: `SessionStart` fires, nothing else ever does."""
    stuck = [ev(ts=ts(24), event="SessionStart", session_id="stuck", cwd=HINTFORGE, tmux_pane="%3")]
    row = only(folded(events=stuck, windows=[win(1, "hikinglog", "node", HINTFORGE, "%3")]), "stuck")
    assert row.status is AgentStatus.QUIET
    assert row.status.label == "quiet - no activity"
    assert row.last_action == "session started"   # what happened is still visible, just not "working"
    assert not row.needs_human


def test_session_start_within_ten_minutes_still_reads_working():
    fresh = [ev(ts=ts(4), event="SessionStart", session_id="fresh", cwd=HINTFORGE, tmux_pane="%3")]
    row = only(folded(events=fresh, windows=[win(1, "hikinglog", "node", HINTFORGE, "%3")]), "fresh")
    assert row.status is AgentStatus.WORKING


def test_quiet_rows_are_excluded_from_the_working_count():
    stuck = [ev(ts=ts(24), event="SessionStart", session_id="stuck", cwd=HINTFORGE, tmux_pane="%3")]
    rows = folded(events=stuck, windows=[win(1, "hikinglog", "node", HINTFORGE, "%3")])
    working, needing = st.counts(rows)
    assert working == 0 and needing == 0


def test_finished_desktop_run_ages_out_of_needs_you_after_ten_minutes():
    """The user: "a routine yolo run finished" and the amber pill never let go -- a `Stop` with no
    tmux window at all (a Desktop-app session, `where == "desktop"`) is nothing the deck can jump
    to or answer, so it cannot need the user forever."""
    finished = [ev(ts=ts(24), event="Stop", session_id="desktop_done", cwd=ROOT + "/Plumb")]
    row = only(folded(events=finished, windows=[]), "desktop_done")
    assert row.status is AgentStatus.QUIET
    assert not row.needs_human


def test_finished_desktop_run_still_needs_you_inside_ten_minutes():
    fresh = [ev(ts=ts(4), event="Stop", session_id="desktop_fresh", cwd=ROOT + "/Plumb")]
    row = only(folded(events=fresh, windows=[]), "desktop_fresh")
    assert row.status is AgentStatus.WAITING_INPUT
    assert row.needs_human


def test_finished_run_with_a_real_window_needs_you_with_no_expiry():
    """The other half of the same rule: a `Stop` on a row the deck CAN jump to (a live tmux
    window) is a real needs-you and must never age out, however long it sits."""
    finished = [ev(ts=ts(90), event="Stop", session_id="s2", cwd=PMBOT, tmux_pane="%4")]
    row = only(folded(events=finished, windows=[win(2, "plumb", "node", PMBOT, "%4")]), "s2")
    assert row.status is AgentStatus.WAITING_INPUT
    assert row.needs_human


def test_quiet_sorts_below_working_but_above_idle():
    rows = folded(
        events=[
            ev(ts=ts(24), event="SessionStart", session_id="quiet1", cwd=HINTFORGE, tmux_pane="%3"),
            ev(ts=ts(1), event="PostToolUse", session_id="busy", cwd=PMBOT, tool_name="Edit", tmux_pane="%4"),
            ev(ts=ts(1), event="Notification", session_id="idler", cwd=ROOT + "/Canvas",
               notification_type="idle_prompt", message="waiting"),
        ],
        windows=[win(1, "hikinglog", "node", HINTFORGE, "%3"), win(2, "plumb", "node", PMBOT, "%4")],
    )
    order = [r.session_id for r in rows]
    assert order.index("busy") < order.index("quiet1") < order.index("idler")


# ------------------------------------------------------------------ order and footer


def test_sort_puts_blocked_first_then_waiting_then_working():
    order = [r.session_id for r in folded()]
    assert order[0] == "s1"                     # blocked on a permission
    assert order[1] == "s6"                     # waiting, most recent of the waiting rows
    assert order.index("j1") < order.index("s2")   # a crashed job outranks a working agent
    assert order.index("s2") < order.index("s3")   # working outranks idle
    assert order[-1] == "s7"                       # gone sinks to the bottom
    # Within the working group the newest event comes first.
    working = [r for r in folded() if r.status is AgentStatus.WORKING]
    stamps = [r.last_event_ts for r in working]
    assert stamps == sorted(stamps, reverse=True)


def test_counts_feed_the_header():
    working, needing = st.counts(folded())
    assert needing == 2                          # s1 blocked, s6 waiting
    assert working == len([r for r in folded() if r.status is AgentStatus.WORKING])


def test_corrupt_lines_are_counted_and_reach_the_footer(tmp_path):
    p = tmp_path / "events.jsonl"
    with open(p, "w", encoding="utf-8") as fh:
        for e in FIXTURE[:3]:
            fh.write(json.dumps(e.to_dict()) + "\n")
        fh.write("{ not json at all\n")
        fh.write("[1,2,3]\n")
    parsed, errors = read_events(p)
    assert len(parsed) == 3 and errors == 2
    rows = st.fold(parsed, [], NOW, "pantheon", ROOT)
    assert only(rows, "s1").status is AgentStatus.BLOCKED_PERMISSION
    assert st.footer_status(errors, True) == "parse errors: 2  (live: tmux)"
    assert st.footer_status(0, True) == "(live: tmux)"      # silent when there is nothing wrong
    assert st.footer_status(0, False) == "(no tmux)"


def test_folder_fallback_ignores_windows_that_are_not_running_an_agent():
    """Six old sessions were painted as `pantheon:0` (the deck's own window) on 2026-09-02."""
    from pantheon.models import TmuxWindow
    from pantheon.supervisor.state import match_window

    deck = TmuxWindow(0, "deck", "bash", "C:/Home/x/Documents/Projects/project_lanterns", "%1", "pantheon")
    agent = TmuxWindow(4, "project_lanterns", "node", "C:/Home/x/Documents/Projects/project_lanterns", "%7", "pantheon")
    cwd = "C:/Home/x/Documents/Projects/project_lanterns".replace("/", chr(92))  # backslashes, as the hook writes them
    assert match_window(cwd, None, [deck], "pantheon") is None
    assert match_window(cwd, None, [deck, agent], "pantheon") is agent
    assert match_window(cwd, "%7", [deck, agent], "pantheon") is agent  # an exact pane id to an agent pane wins
    # An exact pane id to a pane that is NOT running an agent does not win (the folder fallback
    # then applies); see the next test for why.
    assert match_window(cwd, "%1", [deck, agent], "pantheon") is agent


def test_pane_id_reused_by_a_restarted_server_does_not_match():
    """The user killed the tmux server from his phone on 2026-09-02. The new server handed out
    `%2` again, this time to the usage window; a dead loom-os session whose last event carried
    `%2` was painted as living at `pantheon:2`, and `j` would have jumped into the budget pane."""
    from pantheon.models import TmuxWindow
    from pantheon.supervisor.state import match_window

    hud = TmuxWindow(2, "hud", "python", "C:/Home/x/Documents/Projects/project_lanterns", "%2", "pantheon")
    assert match_window("C:/Home/x/Documents/Projects/loom-os", "%2", [hud], "pantheon") is None


def test_a_reused_pane_id_belongs_to_the_newest_session_only():
    """A killed session's pane id came back on a new window; the dead one must not look alive."""
    from datetime import timedelta

    from pantheon.models import Event, TmuxWindow

    cwd = "C:/Home/x/Documents/Projects/loom-os"
    old_ts = (NOW - timedelta(minutes=40)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    new_ts = (NOW - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    events = [
        Event(ts=old_ts, event="SessionStart", source="claude", session_id="old", cwd=cwd, tmux_pane="%3"),
        Event(ts=new_ts, event="SessionStart", source="claude", session_id="new", cwd=cwd, tmux_pane="%3"),
    ]
    window = TmuxWindow(3, "loom-os", "claude.exe", cwd, "%3", "pantheon")
    rows = st.fold(events, [window], NOW, "pantheon", ROOT)
    assert [r.session_id for r in rows] == ["new"]          # the old one is gone and hidden
    assert rows[0].where == "pantheon:3"


def test_a_full_windows_path_still_counts_as_an_agent_program():
    """tmux reported `C:\\...\\claude.exe` for a dispatched window, not `claude`."""
    from pantheon.models import TmuxWindow
    from pantheon.supervisor.state import _is_agent_window, command_name

    assert command_name("C:/Home/x/.local/bin/claude.exe".replace("/", chr(92))) == "claude"
    assert command_name("node") == "node" and command_name(None) == ""
    w = TmuxWindow(3, "loom-os", "C:/Home/x/.local/bin/claude.exe", "C:/x", "%3", "pantheon")
    assert _is_agent_window(w)


def test_a_finished_codex_job_stays_done_not_gone_when_its_pane_goes_stale():
    """The user's first phone-launched job showed `gone` five minutes after
    `done exit 0`: the recorded pane stopped matching a window, the 180s dead-pane rule declared
    the row not-present, and the demotion overwrote DONE. Terminal states must stick."""
    events = [
        ev(ts=ts(6), event="queued", source="codex", job_id="jd", tmux_pane="%9",
           cwd="C:/Home/x/Documents/Projects/Canvas"),
        ev(ts=ts(6), event="running", source="codex", job_id="jd", tmux_pane="%9",
           cwd="C:/Home/x/Documents/Projects/Canvas"),
        ev(ts=ts(5), event="done", source="codex", job_id="jd", tmux_pane="%9",
           detail="exit code 0", cwd="C:/Home/x/Documents/Projects/Canvas"),
    ]
    row = only(folded(events=events, windows=[]), "jd")
    assert row.status is AgentStatus.DONE
    assert "exit code 0" in row.last_action
    # ...and it still leaves the screen for good after the linger window.
    old = [ev(**{**e.__dict__, "ts": ts(15)}) for e in events]
    assert folded(events=old, windows=[]) == []


# ------------------------------------------------------- root-cwd project labels


def test_a_root_cwd_session_is_labeled_workspace():
    """CHECKPOINT open thread: a session whose cwd is `projects_root` itself
    (no subfolder) used to fall through `derive_project` to no project at all -- the Remote
    Control dispatch conversation showing as no-project/no-window in the pit. No transcript on
    disk for this fake session id, so `read_transcript` reports nothing known and the row stays
    the plain `workspace` label, not `dispatch`."""
    events = [ev(ts=ts(3), event="SessionStart", session_id="root1", cwd=ROOT, detail="startup")]
    row = only(folded(events=events, windows=[]), "root1")
    assert row.project == "workspace"


def test_a_root_cwd_session_with_the_dispatch_fingerprint_is_labeled_dispatch(monkeypatch):
    """The Remote Control conversation's transcript title comes out as the literal first message
    "claude rc" (verified against the real transcript, `transcripts.py` DISPATCH_TITLE docstring)
    -- refine `workspace` to `dispatch` only for that fingerprint, never for a root-cwd session
    with any other title (e.g. the "work the plate" orchestrator, also root-cwd)."""
    from pantheon import transcripts as transcripts_mod

    def fake_read_transcript(session_id, cwd, claude_home=None):
        return transcripts_mod.TranscriptInfo(title="claude rc")

    monkeypatch.setattr(st.transcripts_mod, "read_transcript", fake_read_transcript)
    events = [ev(ts=ts(3), event="SessionStart", session_id="root2", cwd=ROOT, detail="startup")]
    row = only(folded(events=events, windows=[]), "root2")
    assert row.project == "dispatch"


def test_a_root_cwd_orchestrator_session_stays_workspace_not_dispatch(monkeypatch):
    from pantheon import transcripts as transcripts_mod

    def fake_read_transcript(session_id, cwd, claude_home=None):
        return transcripts_mod.TranscriptInfo(title="Sprint delegation from Claude's plate")

    monkeypatch.setattr(st.transcripts_mod, "read_transcript", fake_read_transcript)
    events = [ev(ts=ts(3), event="SessionStart", session_id="root3", cwd=ROOT, detail="startup")]
    row = only(folded(events=events, windows=[]), "root3")
    assert row.project == "workspace"


def test_a_project_subfolder_cwd_is_unchanged_by_the_root_cwd_rule():
    """A regular project session (cwd under `projects_root`, e.g. hiking_log_v2) still resolves
    via `derive_project` alone -- the root-cwd special case never fires for it."""
    row = only(folded(), "s1")
    assert row.project == "hiking_log_v2"
