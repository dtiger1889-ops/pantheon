"""The tab rules, hand-copied from the written specs.

Why this file exists twice over: `pantheon/tasks/obsidian_base.py` reads the SAME rules straight
out of the Obsidian Base file, so any Base with these fields works. That reader is
clever and could drift. These functions are the dumb, literal copy of what the specs say, and the
tests compare the two on real notes. **If they ever disagree, this file wins** and the difference
gets written up, because this is what the user signed off on.

Everything here is a plain function of one task row -- no files read, no state, no surprises.
"""
from __future__ import annotations

from typing import Callable, Iterable

from ..models import TaskRow

# The order the tabs appear on screen and the number key that jumps to each one.
# Taken from  (Now, Decide) and  (the rest), and matched by `bin/pantheon --keys`.
TAB_ORDER = ["Now", "Decide", "Quick wins", "Agent's plate", "Someday", "Notes", "By project"]

# Group headings for the Agent's-plate tab, in the order the spec asks for.
PLATE_GROUPS = ["small", "medium", "large", "unsized"]

# Ranking used when a tab sorts by tier or by effort. Anything unrecognised sorts last.
_TIER_RANK = {"now": 0, "soon": 1, "someday": 2}
_COMPLEXITY_RANK = {"heavy": 0, "moderate": 1, "quick": 2}


# ---------------------------------------------------------------- helpers


def _text(value: object) -> str:
    """A string we can compare, whatever the note actually had in it."""
    return "" if value is None else str(value).strip()


def _has_text(value: object) -> bool:
    return _text(value) != ""


def is_open(row: TaskRow) -> bool:
    """Every tab hides finished work: `done != true AND status != "done"`."""
    return row.done is not True and _text(row.status).lower() != "done"


# ---------------------------------------------------------------- one function per tab


def now(row: TaskRow) -> bool:
    """Now -- what the user picked up. `next == true`."""
    return is_open(row) and row.next is True


def decide(row: TaskRow) -> bool:
    """Decide -- stuck and waiting on a call from the user.
    `agent != true AND status == "blocked" AND tier != "someday"`."""
    return (
        is_open(row)
        and row.agent is not True
        and _text(row.status).lower() == "blocked"
        and _text(row.tier).lower() != "someday"
    )


def quick_wins(row: TaskRow) -> bool:
    """Quick wins -- small jobs the user does himself.
    `agent != true AND complexity == "quick" AND status != "blocked" AND next != true
    AND tier != "someday"`."""
    return (
        is_open(row)
        and row.agent is not True
        and _text(row.complexity).lower() == "quick"
        and _text(row.status).lower() != "blocked"
        and row.next is not True
        and _text(row.tier).lower() != "someday"
    )


def plate(row: TaskRow) -> bool:
    """Agent's plate -- work handed to an agent. `agent == true`."""
    return is_open(row) and row.agent is True


def someday(row: TaskRow) -> bool:
    """Someday -- parked, not forgotten. `tier == "someday"`."""
    return is_open(row) and _text(row.tier).lower() == "someday"


def notes(row: TaskRow) -> bool:
    """Notes -- the message lane between the user and Claude. Every open row appears;
    the ones carrying a note from the user sort to the top."""
    return is_open(row)


def by_project(row: TaskRow) -> bool:
    """By project -- everything open, grouped by project. Exists for parity with the Base."""
    return is_open(row)


FILTERS: dict[str, Callable[[TaskRow], bool]] = {
    "Now": now,
    "Decide": decide,
    "Quick wins": quick_wins,
    "Agent's plate": plate,
    "Someday": someday,
    "Notes": notes,
    "By project": by_project,
}


# ---------------------------------------------------------------- sorting


def sort_now(row: TaskRow):
    """Oldest pick first (`picked` ascending); rows with no pick date go last."""
    return (row.picked is None, _text(row.picked), _text(row.summary))


def sort_created(row: TaskRow):
    """Oldest first (`created` ascending)."""
    return (row.created is None, _text(row.created), _text(row.summary))


def sort_plate(row: TaskRow):
    """Inside a plate group: tier first (now, soon, someday), then oldest."""
    return (_TIER_RANK.get(_text(row.tier).lower(), 9), _text(row.created), _text(row.summary))


def sort_someday(row: TaskRow):
    """The user's own rows before the agents' (`agent` ascending), then oldest."""
    return (bool(row.agent), _text(row.created), _text(row.summary))


def sort_notes(row: TaskRow):
    """Rows carrying a note from the user first, then by project."""
    return (not _has_text(row.note), _text(row.project).lower(), _text(row.summary))


def sort_by_project(row: TaskRow):
    """Project name, then biggest effort first."""
    return (
        _text(row.project).lower(),
        _COMPLEXITY_RANK.get(_text(row.complexity).lower(), 9),
        _text(row.summary),
    )


SORTS: dict[str, Callable[[TaskRow], object]] = {
    "Now": sort_now,
    "Decide": sort_created,
    "Quick wins": sort_created,
    "Agent's plate": sort_plate,
    "Someday": sort_someday,
    "Notes": sort_notes,
    "By project": sort_by_project,
}


def plate_group(row: TaskRow) -> str:
    """Which heading a plate row sits under. No size recorded -> `unsized`, shown last."""
    value = _text(row.est_context).lower()
    return value if value in ("small", "medium", "large") else "unsized"


def project_group(row: TaskRow) -> str:
    return _text(row.project) or "no project"


GROUPS: dict[str, Callable[[TaskRow], str]] = {
    "Agent's plate": plate_group,
    "By project": project_group,
}

GROUP_ORDER: dict[str, list[str]] = {"Agent's plate": list(PLATE_GROUPS)}


# ---------------------------------------------------------------- convenience


def select(rows: Iterable[TaskRow], tab: str) -> list[TaskRow]:
    """Rows for one tab, filtered and sorted the way the spec asks."""
    keep = FILTERS[tab]
    return sorted([r for r in rows if keep(r)], key=SORTS[tab])


def counts(rows: Iterable[TaskRow]) -> dict[str, int]:
    """How many rows each tab holds. Used by `bin/queue_counts` and the parity tests."""
    rows = list(rows)
    return {tab: sum(1 for r in rows if FILTERS[tab](r)) for tab in TAB_ORDER}
