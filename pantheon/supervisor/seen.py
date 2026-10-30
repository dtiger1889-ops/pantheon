"""When the user last looked at each session, so THE PIT can turn `done` into `idle` the way herdr
does: `done` = it finished and you have not looked yet; once you jump to it, it reads `idle`.
Kept to a very low overhead.

Cost: one small JSON file, `state/seen.json` (`{session_id: UTC ISO time}`), read once when the
pane mounts and written only when a jump marks a session -- nothing on the 5-second tick. A row
counts as seen when the mark is newer than the row's own last event, so the next turn that
finishes reads `done` again without anyone clearing anything.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from ..models import AgentState, AgentStatus, parse_ts

FILE_NAME = "seen.json"
KEEP = 200   # newest marks kept; older sessions are long gone from the pit

# Claude Code sends its `idle_prompt` notice about a minute after a turn ends, which re-stamps the
# row; a jump in that minute still counts for the turn it was looking at.
IDLE_PROMPT_LAG = timedelta(seconds=90)


class SeenMarks:
    def __init__(self, state_dir: str) -> None:
        self.path = Path(state_dir) / FILE_NAME
        self._marks: dict[str, str] = {}
        # Sessions seen mid-turn (or asking something) since their mark: the finished turn that
        # follows is a new one, whatever the 90-second idle allowance says. In memory only; a deck restart falls back to the timestamps.
        self._worked_since_mark: set[str] = set()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self._marks = {str(k): str(v) for k, v in data.items()}
        except (OSError, ValueError):
            self._marks = {}

    def mark(self, session_id: Optional[str], when: Optional[datetime] = None) -> None:
        """The user looked at this session now. Never raises: a failed write only costs the mark
        surviving a deck restart."""
        if not session_id:
            return
        when = when or datetime.now(timezone.utc)
        self._worked_since_mark.discard(session_id)
        self._marks[session_id] = when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        if len(self._marks) > KEEP:
            newest = sorted(self._marks.items(), key=lambda kv: kv[1])[-KEEP:]
            self._marks = dict(newest)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self._marks), encoding="utf-8")
        except OSError:
            pass

    def saw_unfinished(self, row: AgentState) -> None:
        """The pit drew this row mid-turn or asking something: after its mark, that is new work."""
        if row.session_id and row.session_id in self._marks:
            self._worked_since_mark.add(row.session_id)

    def seen(self, row: AgentState) -> bool:
        """Has the user looked at this row since it last changed?"""
        mark = parse_ts(self._marks.get(row.session_id or ""))
        if mark is None or row.session_id in self._worked_since_mark:
            return False
        last = parse_ts(row.last_event_ts)
        if last is None:
            return True
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        if mark.tzinfo is None:
            mark = mark.replace(tzinfo=timezone.utc)
        if row.status is AgentStatus.IDLE:
            last = last - IDLE_PROMPT_LAG
        return mark >= last
