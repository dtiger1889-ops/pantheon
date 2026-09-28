"""One interface for every task system.

A source produces rows and tabs; the queue pane only renders. Read-only in v1: `open_for_edit`
returns WHERE the user edits, it never writes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Protocol, runtime_checkable

from ..models import TaskRow


@dataclass
class TabSpec:
    """One tab in the queue pane. `key` is the number key; `filter` and `sort_key` are pure functions."""

    name: str                                   # "Now", "Decide", ...
    key: str                                    # "1".."9"
    filter: Callable[[TaskRow], bool]
    sort_key: Callable[[TaskRow], object] = lambda r: r.created or ""
    group_by: Callable[[TaskRow], str] | None = None   # e.g. est_context for "Agent's plate"
    group_order: list[str] = field(default_factory=list)


@runtime_checkable
class TaskSource(Protocol):
    name: str          # "obsidian_base" | "standalone" | ...
    kind: str          # plain words for the header, e.g. "Obsidian Base: Sprints"
    notice: str        # one sentence for the queue's status line, or "" when there is nothing to say
    """Set once, when the source builds its tabs: e.g. "Base file not found at ...; showing the
    built-in tabs". The queue pane shows it at startup and never invents a fallback of its own."""

    def rows(self) -> list[TaskRow]: ...
    """All rows, already parsed. Never raises on a bad file: keep the last good parse and count it."""

    def tabs(self) -> list[TabSpec]: ...

    def flagged_tabs(self) -> set[str]: ...
    """Tab names whose filter rule could not be fully reproduced; the pane marks them with a `*`."""

    def refresh(self) -> bool: ...
    """Re-read what changed; return True if any row changed."""

    def parse_errors(self) -> int: ...

    def open_for_edit(self, row: TaskRow) -> str: ...
    """A folder or file path the deck may open in a new tmux window. Never writes."""
