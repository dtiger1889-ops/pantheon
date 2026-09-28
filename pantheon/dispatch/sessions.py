"""The recent Claude Code sessions of a project folder, for the New-session "resume" step
.

Claude Code keeps one transcript per session under `~/.claude/projects/<slug>/<session_id>.jsonl`,
where the slug is the folder path with every character that is not a letter or digit turned into
`-` (`C:\\Users\\x\\Documents\\Projects\\habit_notes` -> `C--Users-x-Documents-Projects-habit-notes`).
A session's name is written into the transcript as `custom-title` records (a rename, or the
Desktop app's own name) and `ai-title` records, the last of each winning; without one the title
is the first thing the user really typed (`title_or_none`, rules in `session_view/titles.py`).
`claude --resume <session_id>` reopens exactly that conversation.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..session_view import titles as titles_mod

MAX_SESSIONS = 9
TITLE_CHARS = 44   # a title + key + "15m ago" must fit one line of the 76-column Pick box


@dataclass(frozen=True)
class SessionInfo:
    session_id: str
    title: str
    modified: datetime          # UTC, from the transcript file's mtime

    @property
    def when_text(self) -> str:
        seconds = (datetime.now(timezone.utc) - self.modified).total_seconds()
        if seconds < 3600:
            return f"{max(1, int(seconds // 60))}m ago"
        if seconds < 86400:
            return f"{int(seconds // 3600)}h ago"
        return f"{int(seconds // 86400)}d ago"


def slug_for(project_dir: str) -> str:
    """Claude Code's folder name for a project path (Windows or POSIX spelling)."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(project_dir))


def _clean(text: str) -> str:
    """Collapse whitespace and clip at a word boundary (kept for callers that import it)."""
    return titles_mod.clip(text, TITLE_CHARS)


# Bounded reads. Title records are re-appended near the END of a transcript every few turns (measured:
# the last one sits within ~25 lines of EOF on the 30 newest files), and the first thing the
# user typed is near the START -- so the head and the tail are read, never the middle.
HEAD_BYTES = 1_000_000   # enough for a first turn even with a pasted screenshot or two
HEAD_LINES = 120         # ...but never more than this many records
TAIL_BYTES = 256_000

# path -> (mtime, size, title-or-None). The head's first-user result is cached separately
# because the head of an append-only file never changes once it holds a strong candidate.
_title_cache: dict = {}
_head_cache: dict = {}   # path -> (size_when_read, best_title_record, first_user_candidate)


def _scan_head(path: Path) -> tuple[Optional[tuple], Optional[tuple]]:
    best_record: Optional[tuple] = None     # (title, rank)
    first_strong: Optional[tuple] = None
    first_weak: Optional[tuple] = None
    read = 0
    with open(path, "rb") as fh:
        for n, raw in enumerate(fh):
            read += len(raw)
            if n >= HEAD_LINES or read > HEAD_BYTES:
                break
            if b'"customTitle"' in raw or b'"aiTitle"' in raw or b'"summary"' in raw:
                try:
                    found = titles_mod.title_record_value(json.loads(raw))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    found = None
                if found and (best_record is None or found[1] >= best_record[1]):
                    best_record = found
                if found:
                    continue
            if first_strong is None and b'"type":"user"' in raw.replace(b" ", b""):
                try:
                    cand = titles_mod.user_record_candidate(json.loads(raw))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    cand = None
                if cand is None:
                    continue
                if cand[1] >= titles_mod.STRONG:
                    first_strong = cand
                elif first_weak is None:
                    first_weak = cand
    return best_record, (first_strong or first_weak)


def _scan_tail(path: Path, size: int) -> Optional[tuple]:
    best: Optional[tuple] = None
    with open(path, "rb") as fh:
        start = max(0, size - TAIL_BYTES)
        fh.seek(start)
        data = fh.read(TAIL_BYTES)
    lines = data.splitlines()
    if start > 0:
        lines = lines[1:]   # the first piece is a partial line
    for raw in lines:
        if b'"customTitle"' not in raw and b'"aiTitle"' not in raw:
            continue
        try:
            found = titles_mod.title_record_value(json.loads(raw))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if found and (best is None or found[1] >= best[1]):
            best = found
    return best


def title_or_none(transcript: Path) -> Optional[str]:
    """The session's readable title, or None when nothing better than its id exists.

    Order (`session_view/titles.py` has the why): the last `customTitle` (a rename, or the
    Desktop app's own name) > the last `aiTitle` > a `summary` record > the first thing the user
    really typed (skill bodies, command wrappers, hook output, AGENTS.md/CLAUDE.md injections and
    tool results skipped). Reads only the head and tail of the file, cached on (mtime, size)."""
    path = Path(transcript)
    try:
        stat = path.stat()
    except OSError:
        return None
    key = str(path)
    cached = _title_cache.get(key)
    if cached is not None and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
        return cached[2]
    try:
        head = _head_cache.get(key)
        head_settled = head is not None and head[2] is not None and head[2][1] >= titles_mod.STRONG
        if head is None or head[0] > stat.st_size or (not head_settled and head[0] < stat.st_size):
            record, first_user = _scan_head(path)
            head = (stat.st_size, record, first_user)
            _head_cache[key] = head
        tail = _scan_tail(path, stat.st_size)
    except OSError:
        return None
    candidates = [c for c in (tail, head[1]) if c]
    best = max(candidates, key=lambda c: c[1]) if candidates else None
    # the tail is later in the file, so on a tie of rank it wins (max keeps the first maximum)
    title = best[0] if best else (head[2][0] if head[2] else None)
    _title_cache[key] = (stat.st_mtime, stat.st_size, title)
    return title


_custom_cache: dict = {}   # path -> (mtime, size, custom title or None)


def custom_title(transcript: Path) -> Optional[str]:
    """The conversation's own NAME -- the last `customTitle` (a `/rename`, `--name`, or a rename
    Claude Code wrote locally) -- or None. Never an aiTitle or a first line: this answers "has it
    been named", not "what is it about". Head and tail only, cached on (mtime, size).
    Note: an app-side rename of a Remote Control session left NO record here, so None does not
    prove the conversation has no name anywhere."""
    path = Path(transcript)
    try:
        stat = path.stat()
    except OSError:
        return None
    key = str(path)
    cached = _custom_cache.get(key)
    if cached is not None and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
        return cached[2]
    try:
        head, _first = _scan_head(path)
        tail = _scan_tail(path, stat.st_size)
    except OSError:
        return None
    name = None
    for found in (tail, head):
        if found and found[1] >= 3:
            name = found[0]
            break
    _custom_cache[key] = (stat.st_mtime, stat.st_size, name)
    return name


def title_of(transcript: Path) -> str:
    """`title_or_none`, else `untitled` -- never the bare session id."""
    return title_or_none(transcript) or "untitled"


def list_sessions(project_dir: str, claude_home: Optional[Path] = None,
                  limit: int = MAX_SESSIONS) -> list[SessionInfo]:
    """Newest first. `claude_home` defaults to `~/.claude`; tests pass a temp folder."""
    home = Path(claude_home) if claude_home is not None else Path.home() / ".claude"
    folder = home / "projects" / slug_for(project_dir)
    if not folder.is_dir():
        return []
    try:
        files = [p for p in folder.iterdir() if p.is_file() and p.suffix == ".jsonl"]
    except OSError:
        return []
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    out: list[SessionInfo] = []
    for path in files[:limit]:
        modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        out.append(SessionInfo(path.stem, title_of(path), modified))
    return out
