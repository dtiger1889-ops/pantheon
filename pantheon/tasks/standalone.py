"""The standalone task source: a plain folder of Markdown files carrying the
same frontmatter fields the vault Sprints notes use, for anyone who does not run Obsidian.

D7 ("never write to the vault") only ever bound the vault -- this folder is Pantheon's own store,
so `pantheon new` and the optional `Mark done` toggle may write here. Nothing in this module ever touches the vault.

Tabs come from `<folder>/pantheon-tabs.yaml` -- the gh-dash `{name, key, filters, sort, groupBy}`
shape (research B9), read by the same `basefilter` module the Obsidian Base source uses, so one
filter language and one `TaskSource` interface serve both adapters. A missing tabs file gets a
starter one written out on first run, copying the Sprints views' rules so a brand-new folder shows
something familiar immediately -- Obsidian never reads this file, so writing it is not a vault
write.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path

import frontmatter
import yaml

from ..models import TaskRow
from . import basefilter
from .base import TabSpec
from .obsidian_base import row_from_post

log = logging.getLogger("pantheon.tasks.standalone")

TABS_FILENAME = "pantheon-tabs.yaml"

# Mirrors the vault contract exactly, so a
# brand-new standalone folder starts out behaving the same as the Obsidian source does. Every tab
# also carries `done != true` -- there is no `status == "done"` convention required here the way
# the vault's Base always pairs the two, but a standalone note could still set one, so both checks
# apply for headroom.
STARTER_TABS = """\
# Pantheon's own tab list for this folder. Each tab is
# {name, key, filters, sort, groupBy} -- the same shape gh-dash uses for its own tabs -- read by
# pantheon/tasks/basefilter.py, the identical filter language the Obsidian Base source reads.
# Edit freely: add or remove tabs, reuse keys 1-9, change any filter. Obsidian never reads this
# file.
tabs:
  - name: Now
    key: "1"
    filters: { and: ["done != true", "status != \\"done\\"", "next == true"] }
    sort: [{ property: picked, direction: ASC }]
  - name: Decide
    key: "2"
    filters: { and: ["done != true", "status != \\"done\\"", "agent != true", "status == \\"blocked\\"", "tier != \\"someday\\""] }
    sort: [{ property: created, direction: ASC }]
  - name: Quick wins
    key: "3"
    filters: { and: ["done != true", "status != \\"done\\"", "agent != true", "complexity == \\"quick\\"", "status != \\"blocked\\"", "next != true", "tier != \\"someday\\""] }
    sort: [{ property: created, direction: ASC }]
  - name: Agent's plate
    key: "4"
    filters: { and: ["done != true", "status != \\"done\\"", "agent == true"] }
    groupBy: { property: est_context, direction: DESC }
  - name: Someday
    key: "5"
    filters: { and: ["done != true", "status != \\"done\\"", "tier == \\"someday\\""] }
    sort: [{ property: agent, direction: ASC }]
  - name: Notes
    key: "6"
    filters: { and: ["done != true", "status != \\"done\\""] }
  - name: By project
    key: "7"
    filters: { and: ["done != true", "status != \\"done\\""] }
    groupBy: { property: project, direction: ASC }
"""


class StandaloneSource:
    """A folder of `*.md` files plus its own tabs file, presented as rows and tabs."""

    name = "standalone"

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        settings = cfg.standalone_settings()
        self.folder = Path(settings.folder)
        self.kind = f"folder: {self.folder.name}"
        self.tabs_path = self.folder / TABS_FILENAME
        self._rows: dict[str, TaskRow] = {}
        self._parse_errors = 0
        self._error_files: dict[str, str] = {}
        self._tabs: list[TabSpec] = []
        self._flagged: set[str] = set()
        self._lock = threading.Lock()
        self.load_error: str = ""
        self.notice: str = ""   # one sentence for the queue's status line if the tabs file could not be read
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.warning("could not create the standalone folder %s: %s", self.folder, exc)
        #: dropped the folder watcher -- the task list re-reads on the timer
        # (`refresh_seconds` in pantheon.toml) or by hand with `r`, never live. These two track
        # whether the folder actually moved since the last read, so a re-read that finds nothing
        # new is cheap.
        self._last_seen_mtime = 0.0
        self._last_file_count = 0
        self._read_all()
        self._build_tabs()

    # -- what the queue pane asks for ---------------------------------

    def rows(self) -> list[TaskRow]:
        with self._lock:
            return list(self._rows.values())

    def tabs(self) -> list[TabSpec]:
        return list(self._tabs)

    def parse_errors(self) -> int:
        return self._parse_errors

    def error_files(self) -> dict[str, str]:
        with self._lock:
            return dict(self._error_files)

    def flagged_tabs(self) -> set[str]:
        return set(self._flagged)

    def open_for_edit(self, row: TaskRow) -> str:
        """The task's own file."""
        return row.path or str(self.folder)

    def refresh(self) -> bool:
        """Re-read the folder when its newest file (or the number of files) has moved since the
        last read. Safe to call often -- on the timer (`refresh_seconds` in pantheon.toml) or by
        hand with `r` -- since it skips the read entirely when nothing on disk changed."""
        if not self._folder_changed():
            return False
        return self._read_all()

    def _folder_changed(self) -> bool:
        try:
            paths = list(self.folder.glob("*.md"))
        except OSError:
            return True   # let _read_all() report the real error
        newest = max((p.stat().st_mtime for p in paths), default=0.0)
        return newest > self._last_seen_mtime or len(paths) != self._last_file_count

    # -- reading --------------------------------------------------------

    def _read_all(self) -> bool:
        if not self.folder.is_dir():
            self.load_error = (
                f"the standalone folder was not found at {self.folder}; fix it in pantheon.toml"
            )
            return False
        self.load_error = ""
        fresh: dict[str, TaskRow] = {}
        errors: dict[str, str] = {}
        newest = 0.0
        try:
            paths = sorted(p for p in self.folder.glob("*.md"))
        except OSError as exc:
            self.load_error = f"cannot read {self.folder}: {exc}"
            return False
        for path in paths:
            try:
                newest = max(newest, path.stat().st_mtime)
            except OSError:
                pass
            try:
                text = path.read_text(encoding="utf-8")
                post = frontmatter.loads(text)
                if not post.metadata and text.lstrip().startswith("---"):
                    raise ValueError("the details block at the top of the file is not finished")
                fresh[path.stem] = row_from_post(path, post)
            except Exception as exc:  # a half-written or unreadable file, never fatal
                errors[path.name] = str(exc).splitlines()[0][:200]
                previous = self._rows.get(path.stem)
                if previous is not None:
                    fresh[path.stem] = previous
        with self._lock:
            changed = fresh != self._rows
            self._rows = fresh
            self._error_files = errors
            self._parse_errors += len(errors)
        self._last_seen_mtime = newest
        self._last_file_count = len(paths)
        return changed

    # -- tabs -------------------------------------------------------------

    def _read_tabs_yaml(self) -> list[dict]:
        if not self.tabs_path.exists():
            try:
                self.tabs_path.parent.mkdir(parents=True, exist_ok=True)
                self.tabs_path.write_text(STARTER_TABS, encoding="utf-8")
                log.info("wrote a starter tabs file at %s", self.tabs_path)
            except OSError as exc:
                log.warning("could not write the starter tabs file %s: %s", self.tabs_path, exc)
                self.notice = f"could not write a starter tabs file at {self.tabs_path}: {exc}"
                return []
        try:
            data = yaml.safe_load(self.tabs_path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            log.warning("could not read the tabs file %s: %s", self.tabs_path, exc)
            self.notice = f"could not read {self.tabs_path}: {exc}"
            return []
        tabs = data.get("tabs") or []
        return [t for t in tabs if isinstance(t, dict) and t.get("name")]

    def _build_tabs(self) -> None:
        raw_tabs = self._read_tabs_yaml()
        built: list[TabSpec] = []
        for i, raw in enumerate(raw_tabs):
            view = basefilter.parse_view(raw)
            if view.unknown:
                self._flagged.add(view.name)
                for text in view.unknown:
                    log.warning("tab %r: could not reproduce the rule %r; ignoring it", view.name, text)
            tab = basefilter.to_tabspec(view)
            if not tab.key:
                tab.key = str(i + 1) if i < 9 else ""
            built.append(tab)
        self._tabs = built


def make(cfg) -> StandaloneSource:
    """The registry in `pantheon/tasks/__init__.py` calls this."""
    return StandaloneSource(cfg)
