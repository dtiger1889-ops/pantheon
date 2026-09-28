"""The drill-down source: a project's own `CHECKPOINT.md` `## Open threads`,
presented as queue rows and tabs -- the third `TaskSource`, beside `obsidian_base` and
`standalone`.

Unlike those two, this source is parameterized by the project the user drilled to; the build
package's own preference is a fresh instance per drill rather than a `set_target`
added to the `TaskSource` interface, so `pantheon/queue/pane.py` constructs one with `make(cfg,
project)` and throws it away on un-drill instead of mutating a shared instance.

Read-only, always: nothing in this module opens `CHECKPOINT.md` for writing.
Editing a thread is the `checkpoint` skill's job, run inside that project -- `open_for_edit` only
says WHERE that happens.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from ..models import TaskRow
from .base import TabSpec
from .checkpoint_threads import Thread, parse_open_threads

log = logging.getLogger("pantheon.tasks.checkpoint_source")


def _thread_to_row(thread: Thread, index: int, project: str, path: Path) -> TaskRow:
    """One parsed thread -> one `TaskRow`. `owner` has no field of its own on `TaskRow` (only the
    vault-native sources' fields live there), so it goes into `extra` the same way an unmapped
    frontmatter key does for the Obsidian source; `status` carries the gate text as the "status
    chip" the spec asks for, since the detail panel and header already know how to show `status`."""
    return TaskRow(
        id=f"thread-{index}",
        summary=thread.title,
        project=project,
        tier=thread.priority,
        status=thread.gates,
        body=thread.detail,
        path=str(path),
        extra={"owner": thread.owner, "gates": thread.gates},
    )


def _owner(row: TaskRow) -> str:
    return str(row.extra.get("owner", "unknown"))


def _build_tabs() -> list[TabSpec]:
    """The four tabs the spec settles on. Sorted by title so the list order is stable between reads."""
    return [
        TabSpec(name="All", key="1", filter=lambda r: True, sort_key=lambda r: r.summary or ""),
        TabSpec(name="Needs the user", key="2", filter=lambda r: _owner(r) == "the user",
                sort_key=lambda r: r.summary or ""),
        TabSpec(name="Agent", key="3", filter=lambda r: _owner(r) == "agent",
                sort_key=lambda r: r.summary or ""),
        TabSpec(name="Gated", key="4", filter=lambda r: bool(r.extra.get("gates")),
                sort_key=lambda r: r.summary or ""),
    ]


class CheckpointSource:
    """A project's `CHECKPOINT.md` `## Open threads`, read straight from the file."""

    name = "checkpoint"

    def __init__(self, cfg, project: str) -> None:
        self.cfg = cfg
        self.project = project
        self.path = Path(cfg.projects_root) / project / "CHECKPOINT.md"
        self.kind = f"CHECKPOINT: {project}"
        self.notice = ""
        self.load_error = ""
        self._rows: list[TaskRow] = []
        self._parse_errors = 0
        self._tabs = _build_tabs()
        self._last_mtime: Optional[float] = None
        self._read()

    # -- what the queue pane asks for ---------------------------------

    def rows(self) -> list[TaskRow]:
        return list(self._rows)

    def tabs(self) -> list[TabSpec]:
        return list(self._tabs)

    def flagged_tabs(self) -> set:
        return set()

    def parse_errors(self) -> int:
        return self._parse_errors

    def open_for_edit(self, row: TaskRow) -> str:
        """Where the user edits this thread: the project's own `CHECKPOINT.md` (never written here;
        the `checkpoint` skill, run inside that project, is the only thing that rewrites it)."""
        return str(self.path)

    def refresh(self) -> bool:
        """Re-read only when the file's mtime moved since the last read -- cheap to call on the
        same timer/`r`-key path every other source uses."""
        if not self._changed():
            return False
        return self._read()

    # -- reading --------------------------------------------------------

    def _changed(self) -> bool:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            return True   # let `_read()` produce the "not found" notice
        return mtime != self._last_mtime

    def _read(self) -> bool:
        if not self.path.exists():
            self.notice = f"no CHECKPOINT.md for {self.project} at {self.path}"
            changed = bool(self._rows)
            self._rows = []
            self._last_mtime = None
            return changed
        try:
            text = self.path.read_text(encoding="utf-8")
            mtime = self.path.stat().st_mtime
        except OSError as exc:
            log.warning("could not read %s: %s", self.path, exc)
            self.notice = f"could not read {self.path}: {exc}"
            self._parse_errors += 1
            return False
        try:
            threads = parse_open_threads(text)
        except Exception as exc:  # never fatal: keep the last good parse, count it, say why
            log.exception("could not parse Open threads in %s", self.path)
            self._parse_errors += 1
            self.notice = f"could not parse Open threads in {self.path}: {exc}"
            return False
        self.notice = "" if threads else f"no Open threads section in {self.path}"
        fresh = [_thread_to_row(t, i, self.project, self.path) for i, t in enumerate(threads)]
        changed = fresh != self._rows
        self._rows = fresh
        self._last_mtime = mtime
        self.load_error = ""
        return changed


def make(cfg, project: str) -> CheckpointSource:
    """Constructed directly by the drill action (`pantheon/queue/pane.py`), never through the
    `pantheon/tasks/__init__.py` registry -- that registry's `ctor(cfg)` shape has no room for the
    drilled project, and this source is never `pantheon.toml`'s `task_source`."""
    return CheckpointSource(cfg, project)
