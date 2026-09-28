"""Reads the task list straight out of an Obsidian vault, and never writes to it.

Two things come out of the vault:

* the notes -- every `*.md` under `Projects/Sprints/`, one note per task, with the details in a
  YAML block at the top of the file ("frontmatter");
* the tabs -- the same views Obsidian shows, read out of `Projects/Sprints.base`, so the deck
  cannot drift away from what the user sees on screen. Decision D17: any Base file with these
  fields works, which is why the tabs are read rather than written into this file.

Read-only is a hard rule here: nothing in this module opens a vault file for
writing, creates one, renames one, or deletes one.

A synced vault can change under the reader, so a file can be half-written the moment we read it. A note that
fails to parse is not an error the deck shows off about: we keep the copy that parsed last time,
add one to a counter, and try again on the next refresh.

The Base's own filter-expression language (the `and`/`or`/`not` trees, the six comparison
operators, `file.inFolder`/`file.hasTag`) is `pantheon/tasks/basefilter.py` -- this
module imports it rather than reading that grammar inline, so the standalone source
can read the same language out of its own tabs file with no code shared back the other way.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Optional

import frontmatter
import yaml

from ..models import TaskRow
from ..queue import filters as spec_filters
from . import basefilter
from .base import TabSpec

log = logging.getLogger("pantheon.tasks.obsidian_base")

# Frontmatter keys that land on TaskRow directly. Anything else goes into `extra`.
_KNOWN_KEYS = {
    "summary", "project", "tier", "status", "complexity", "est_context", "agent", "fable", "done",
    "next", "picked", "due", "created", "updated", "source", "note", "reply",
    "project_assignment_log",
}
_BOOL_KEYS = {"agent", "fable", "done", "next"}
_DATE_KEYS = {"picked", "due", "created", "updated"}


# ---------------------------------------------------------------- value tidying


def as_date(value: Any) -> Optional[str]:
    """Dates become plain `YYYY-MM-DD` text so sorting and comparing are boring and reliable."""
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()  # date / datetime from the YAML parser
    text = str(value).strip()
    return text or None


def as_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    text = str(value).strip()
    return [text] if text else []


def row_from_post(path: Path, post: "frontmatter.Post") -> TaskRow:
    meta = dict(post.metadata or {})
    extra = {k: v for k, v in meta.items() if k not in _KNOWN_KEYS}
    return TaskRow(
        id=path.stem,
        summary=as_text(meta.get("summary")) or path.stem,
        project=as_text(meta.get("project")),
        tier=as_text(meta.get("tier")),
        status=as_text(meta.get("status")),
        complexity=as_text(meta.get("complexity")),
        est_context=as_text(meta.get("est_context")),
        # `agent` since 2026-09-23; a stale copy of a row that still says `fable` reads the same.
        agent=basefilter.as_bool(meta["agent"] if "agent" in meta else meta.get("fable")),
        done=basefilter.as_bool(meta.get("done")),
        next=basefilter.as_bool(meta.get("next")),
        picked=as_date(meta.get("picked")),
        due=as_date(meta.get("due")),
        created=as_date(meta.get("created")),
        source=as_text(meta.get("source")),
        note=as_text(meta.get("note")),
        reply=as_text(meta.get("reply")),
        project_assignment_log=as_list(meta.get("project_assignment_log")),
        body=(post.content or "").strip(),
        path=str(path).replace("\\", "/"),
        extra=extra,
    )


def strip_wikilink(value: Optional[str]) -> str:
    """`"[[Some note]]"` -> `Some note`. Leaves plain text and URLs alone."""
    if not value:
        return ""
    return re.sub(r"^\[\[|\]\]$", "", value.strip()).strip()


def read_base_views(base_path: Path) -> list[dict]:
    """The `views:` list out of a `.base` file. A missing or broken file gives no views."""
    try:
        data = yaml.safe_load(base_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        log.warning("could not read the Base file %s: %s", base_path, exc)
        return []
    views = data.get("views") or []
    return [v for v in views if isinstance(v, dict) and v.get("name")]


# ---------------------------------------------------------------- the source


class ObsidianBaseSource:
    """The Sprints folder plus the Sprints Base, presented as rows and tabs."""

    name = "obsidian_base"

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self.base_path = Path(cfg.sprints_base)
        self.kind = f"Obsidian Base: {self.base_path.stem}"
        self._rows: dict[str, TaskRow] = {}
        self._parse_errors = 0
        self._error_files: dict[str, str] = {}
        self._tabs: list[TabSpec] = []
        self._flagged: set[str] = set()          # tabs with a rule we could not reproduce
        self.load_error: str = ""                # one plain sentence for the footer
        self._raw_views = read_base_views(self.base_path)
        self.folder = self._resolve_folder()
        #: dropped the folder watcher -- the task list re-reads on the timer
        # (`refresh_seconds` in pantheon.toml) or by hand with `r`, never live. These two track
        # whether the folder actually moved since the last read, so a re-read that finds nothing
        # new is cheap.
        self._last_seen_mtime = 0.0
        self._last_file_count = 0
        self._read_all()
        self._build_tabs()

    def _resolve_folder(self) -> Path:
        """`sprints_folder` is optional. When the user has not set it, the Base's own
        first `file.inFolder("...")` filter IS the vault-relative source of truth -- pointing
        `base_file` at a second Base with a different folder just works, no config edit needed.
        A Base with no such filter at all falls back to the historical default and says why."""
        if self.cfg.sprints_folder:
            return Path(self.cfg.vault) / self.cfg.sprints_folder
        derived = basefilter.first_in_folder(self._raw_views)
        if derived:
            return Path(self.cfg.vault) / derived
        self.load_error = (
            f"the Base at {self.base_path} has no file.inFolder filter; "
            f"set sprints_folder in pantheon.toml"
        )
        log.warning(self.load_error)
        return Path(self.cfg.vault) / "Projects/Sprints"

    # -- what the queue pane asks for ---------------------------------

    def rows(self) -> list[TaskRow]:
        return list(self._rows.values())

    def tabs(self) -> list[TabSpec]:
        return list(self._tabs)

    def parse_errors(self) -> int:
        return self._parse_errors

    def error_files(self) -> dict[str, str]:
        """File name -> the reason it would not parse. Shown in the `?` overlay, not the row list."""
        return dict(self._error_files)

    def flagged_tabs(self) -> set[str]:
        """Tabs whose Base rules we could not fully reproduce; their names get a `*`."""
        return set(self._flagged)

    def open_for_edit(self, row: TaskRow) -> str:
        """Where the user edits this task: the Sprints folder in Obsidian. Never writes anything."""
        return str(self.folder)

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

    # -- reading ------------------------------------------------------

    def _read_all(self) -> bool:
        """Read every note. A note that will not parse keeps whatever we read last time."""
        if not self.folder.is_dir():
            self.load_error = (
                f"vault folder not found at {self.folder}; fix it in pantheon.toml"
            )
            return False
        self.load_error = ""
        fresh: dict[str, TaskRow] = {}
        errors: dict[str, str] = {}
        newest = 0.0
        try:
            paths = sorted(self.folder.glob("*.md"))
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
                    # A file caught mid-save: the opening `---` is there but the closing one is
                    # not, so the reader quietly hands back an empty note instead of complaining.
                    raise ValueError("the details block at the top of the file is not finished")
                fresh[path.stem] = row_from_post(path, post)
            except Exception as exc:  # a half-written or unreadable file, never fatal
                errors[path.name] = str(exc).splitlines()[0][:200]
                previous = self._rows.get(path.stem)
                if previous is not None:
                    fresh[path.stem] = previous       # keep the last copy that read cleanly
        changed = fresh != self._rows
        self._rows = fresh
        self._error_files = errors
        self._parse_errors += len(errors)
        self._last_seen_mtime = newest
        self._last_file_count = len(paths)
        return changed

    # -- tabs ---------------------------------------------------------

    def _build_tabs(self) -> None:
        views = self._raw_views
        self.notice = ""   # one sentence for the queue's status line when the tabs are not the Base's
        if not views:
            # The deck keeps working on the tab rules written in the specs, and SAYS so -- the
            # every-state tests found this fallback silently swallowing a missing
            # Base file, so the "no Base file" state could never be seen on screen.
            if not self.base_path.exists():
                self.notice = f"Base file not found at {self.base_path}; showing the built-in tabs"
            else:
                self.notice = f"no views in {self.base_path}; showing the built-in tabs"
            log.warning("%s", self.notice)
            self._tabs = self._spec_tabs()
            return
        built: list[TabSpec] = []
        for raw in views:
            view = basefilter.parse_view(raw)
            if view.unknown:
                self._flagged.add(view.name)
                for text in view.unknown:
                    log.warning("tab %r: could not reproduce the rule %r; ignoring it", view.name, text)
            tab = basefilter.to_tabspec(view)
            # Grouping is the one thing a Base cannot spell out for us in a way we can follow --
            # it names a property but not the order of the headings. The specs do name that order
            # for the Agent's plate (small, medium, large, then anything unsized), and the order of
            # the rows inside each heading, so that one tab takes both from `queue/filters.py`.
            if "plate" in view.name.lower():
                tab.group_by = spec_filters.plate_group
                tab.group_order = list(spec_filters.PLATE_GROUPS)
                tab.sort_key = spec_filters.sort_plate
            built.append(tab)
        self._tabs = _assign_keys(built)

    def _spec_tabs(self) -> list[TabSpec]:
        """Used only when the Base file is missing: the rules as written in the specs."""
        return _assign_keys([
            TabSpec(
                name=name,
                key="",
                filter=spec_filters.FILTERS[name],
                sort_key=spec_filters.SORTS[name],
                group_by=spec_filters.GROUPS.get(name),
                group_order=spec_filters.GROUP_ORDER.get(name, []),
            )
            for name in spec_filters.TAB_ORDER
        ])

def _assign_keys(tabs: list[TabSpec]) -> list[TabSpec]:
    """Put the tabs in the order the specs and `bin/pantheon --keys` promise, then number them
    1..9. A view the specs never named keeps its
    place at the end rather than stealing a number the user already has in his fingers."""
    preferred = {name.lower(): i for i, name in enumerate(spec_filters.TAB_ORDER)}
    ordered = sorted(
        enumerate(tabs),
        key=lambda pair: (preferred.get(pair[1].name.lower(), 99), pair[0]),
    )
    out: list[TabSpec] = []
    for position, (_, tab) in enumerate(ordered):
        tab.key = str(position + 1) if position < 9 else ""
        out.append(tab)
    return out


def make(cfg) -> ObsidianBaseSource:
    """The registry in `pantheon/tasks/__init__.py` calls this."""
    return ObsidianBaseSource(cfg)
