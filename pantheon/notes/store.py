"""The scratchpad's store: tabs ARE files.

Every tab is a real file in `state/notes/<slug>.md` -- there is no in-memory buffer separate from
disk, so "the content" and "the tab list" collapse into "the folder" and there is nothing to
reconcile after a crash. `_session.json` in the same folder holds only the tab order and which one
was focused (never any text), so a crash mid-write loses at most the last debounce interval of ONE
tab's content, never the set of tabs.

Pure filesystem functions, no Textual, no tmux -- the widget (`notes/app.py`) is the only caller,
and every function here is trivially unit-testable with a `tmp_path` (`tests/test_notes_store.py`).
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Optional

SESSION_FILE = "_session.json"
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(title: str) -> str:
    """`"Codex handoff notes"` -> `"codex-handoff-notes"`. Never empty -- `"untitled"` is the
    fallback for a title with no letters or digits at all (an emoji-only title, say)."""
    t = _SLUG_RE.sub("-", (title or "").strip().lower()).strip("-")
    return t or "untitled"


def path_for(dir_: Path, slug: str) -> Path:
    return Path(dir_) / f"{slug}.md"


def _existing_slugs(dir_: Path) -> set[str]:
    dir_ = Path(dir_)
    if not dir_.is_dir():
        return set()
    return {p.stem for p in dir_.glob("*.md")}


def _unique_slug(dir_: Path, base: str, exclude: Optional[str] = None) -> str:
    """`base`, or `base-2`, `base-3`, ... the first one not already a file. `exclude` lets `rename` keep a slug that is only "taken" by the file
    being renamed."""
    existing = _existing_slugs(dir_)
    existing.discard(exclude)
    if base not in existing:
        return base
    n = 2
    while f"{base}-{n}" in existing:
        n += 1
    return f"{base}-{n}"


def list_tabs(dir_: Path) -> list[str]:
    """Every tab's slug, newest-first by mtime. No folder yet -> no tabs."""
    dir_ = Path(dir_)
    if not dir_.is_dir():
        return []
    files = sorted(dir_.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    return [p.stem for p in files]


def read(dir_: Path, slug: str) -> str:
    try:
        return path_for(dir_, slug).read_text(encoding="utf-8")
    except OSError:
        return ""


def write(dir_: Path, slug: str, text: str) -> None:
    """Atomic: write `<slug>.md.tmp`, then `os.replace` -- a crash mid-write leaves
    the old content in place, never a half-written file."""
    dir_ = Path(dir_)
    dir_.mkdir(parents=True, exist_ok=True)
    target = path_for(dir_, slug)
    tmp = target.parent / (target.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, target)


def create(dir_: Path, title: str) -> str:
    """A new empty tab, titled `title` (collision-suffixed). Returns its slug."""
    dir_ = Path(dir_)
    slug = _unique_slug(dir_, slugify(title))
    write(dir_, slug, "")
    return slug


def rename(dir_: Path, slug: str, title: str) -> str:
    """Renames the file backing `slug` to match `title`'s slug (collision-suffixed against every
    OTHER tab). Returns the new slug -- unchanged when the title slugifies to the same thing."""
    dir_ = Path(dir_)
    new_base = slugify(title)
    if new_base == slug:
        return slug
    new_slug = _unique_slug(dir_, new_base, exclude=slug)
    old_path = path_for(dir_, slug)
    new_path = path_for(dir_, new_slug)
    if old_path.exists():
        os.replace(old_path, new_path)
    else:
        write(dir_, new_slug, "")
    return new_slug


def delete(dir_: Path, slug: str) -> None:
    try:
        path_for(dir_, slug).unlink()
    except OSError:
        pass


def read_session(dir_: Path) -> dict:
    """`{tabs: [...], focused: slug|None}` -- never any note content. Missing/corrupt file ->
    an empty session, same as a first run."""
    p = Path(dir_) / SESSION_FILE
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"tabs": [], "focused": None}
    if not isinstance(data, dict):
        return {"tabs": [], "focused": None}
    tabs = data.get("tabs")
    return {
        "tabs": list(tabs) if isinstance(tabs, list) else [],
        "focused": data.get("focused"),
    }


def write_session(dir_: Path, tabs: list[str], focused: Optional[str]) -> None:
    dir_ = Path(dir_)
    dir_.mkdir(parents=True, exist_ok=True)
    p = dir_ / SESSION_FILE
    tmp = p.parent / (p.name + ".tmp")
    tmp.write_text(json.dumps({"tabs": list(tabs), "focused": focused}, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


def restore(dir_: Path) -> tuple[list[str], Optional[str]]:
    """The tab order + focus to open with: the saved session's tab list,
    filtered to slugs that still have a file, with any tab a session never recorded (a file that
    somehow appeared without one -- should not happen in normal use, but a crash mid-`create`
    could leave one) appended at the end, newest first. Falls back to `list_tabs`'s own order
    with nothing focused when there is no session file at all -- e.g. the very first run, or a
    session file lost to something outside this module."""
    on_disk = list_tabs(dir_)
    session = read_session(dir_)
    ordered = [s for s in session["tabs"] if s in on_disk]
    ordered += [s for s in on_disk if s not in ordered]
    focused = session["focused"] if session["focused"] in ordered else (ordered[0] if ordered else None)
    return ordered, focused
