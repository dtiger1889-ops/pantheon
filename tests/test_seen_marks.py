"""THE PIT's done/idle marks (`pantheon/supervisor/seen.py`)."""
from datetime import datetime, timedelta, timezone

from pantheon.models import AgentState, AgentStatus
from pantheon.supervisor.seen import SeenMarks

JUMP = datetime(2026, 10, 19, 12, 0, 0, tzinfo=timezone.utc)


def _row(status, at):
    return AgentState(session_id="s-1", provider="claude", status=status,
                      last_event_ts=at.strftime("%Y-%m-%dT%H:%M:%S.000Z"))


def test_the_idle_notice_after_the_viewed_turn_still_counts_as_seen(tmp_path):
    marks = SeenMarks(str(tmp_path))
    marks.mark("s-1", JUMP)
    assert marks.seen(_row(AgentStatus.IDLE, JUMP + timedelta(seconds=60)))


def test_a_new_turn_that_ran_and_finished_inside_the_allowance_is_not_seen(tmp_path):
    marks = SeenMarks(str(tmp_path))
    marks.mark("s-1", JUMP)
    marks.saw_unfinished(_row(AgentStatus.WORKING, JUMP + timedelta(seconds=10)))
    assert not marks.seen(_row(AgentStatus.IDLE, JUMP + timedelta(seconds=60)))
    marks.mark("s-1", JUMP + timedelta(seconds=70))     # looking again clears it
    assert marks.seen(_row(AgentStatus.IDLE, JUMP + timedelta(seconds=60)))
