"""Resolve a task's `source` field to a real file in the vault. Read-only, like every other `tasks/*` module: nothing here
ever creates, renames, writes, or deletes anything -- it only looks.

A task's `source` is one of two things (`pantheon/models.py` `TaskRow.source`):

  * a wikilink, `"[[Some note]]"` -- Obsidian's own cross-reference syntax, which also comes as
    `"[[Some note|shown text]]"` (a display alias) and `"[[Some note#Heading]]"` /
    `"[[Some note#^block]]"` (a heading or block reference) -- all four forms name the same FILE;
    the `|`/`#` suffix only names a location inside it. Obsidian resolves a wikilink by matching
    the title against a FILENAME anywhere in the vault, not a path, so this module does the same:
    strip the brackets (`obsidian_base.strip_wikilink`), cut off anything from the first `|` or
    `#` onward, and search for `<title>.md` by exact, case-insensitive filename;
  * a plain URL (`https://...` or `http://...`) -- there is nothing to resolve; the caller shows
    it as-is (`is_url` names this case so the queue pane never tries to search the vault for it).

The search looks in `<vault>/Projects` first, then falls back to the whole vault, skipping `.trash`
and `.obsidian` either way -- both hold copies or internal bookkeeping, never a note the user wants
opened. The first match wins, the same "one note, one title" assumption Obsidian itself makes when
it resolves a wikilink.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from .obsidian_base import strip_wikilink

SKIP_DIRS = {".trash", ".obsidian"}


def is_url(source: Optional[str]) -> bool:
    """True for a plain URL source -- nothing to resolve, the caller shows it as-is."""
    text = (source or "").strip()
    return text.startswith("http://") or text.startswith("https://")


def _wikilink_title(stripped: str) -> str:
    """`Title|shown text` -> `Title`; `Title#Heading` -> `Title`; `Title#^block` -> `Title`;
    `Title#Heading|shown text` -> `Title`. Cuts at whichever of `|` or `#` comes first -- both
    name a location INSIDE the note, never the note itself -- so every one of Obsidian's wikilink
    forms resolves to the same file. `stripped` has already had its outer `[[`/`]]` removed
    (`obsidian_base.strip_wikilink`)."""
    cut = len(stripped)
    for sep in ("|", "#"):
        idx = stripped.find(sep)
        if idx != -1:
            cut = min(cut, idx)
    return stripped[:cut].strip()


def _first_match(root: Path, filename_lower: str) -> Optional[Path]:
    """The first `*.md` file under `root` (recursive) whose filename matches case-insensitively,
    skipping `.trash` and `.obsidian` at any depth. `Path.rglob` does not sort, which is fine
    here: a real vault holds at most one note with a given title."""
    try:
        candidates = root.rglob("*.md")
    except OSError:
        return None
    for path in candidates:
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.name.lower() == filename_lower:
            return path
    return None


def resolve(vault: Path, source: Optional[str]) -> Optional[Path]:
    """The real file a task's `source` wikilink points to, or `None` when the source is blank, a
    URL (use `is_url` to tell those apart first), or names no file anywhere in the vault. Never
    writes anything."""
    stripped = strip_wikilink(source)
    if not stripped or is_url(stripped):
        return None
    title = _wikilink_title(stripped)
    if not title:
        return None
    filename_lower = f"{title.lower()}.md"
    vault = Path(vault)
    projects = vault / "Projects"
    if projects.is_dir():
        found = _first_match(projects, filename_lower)
        if found is not None:
            return found
    if vault.is_dir():
        return _first_match(vault, filename_lower)
    return None
