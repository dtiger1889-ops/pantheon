"""The briefing: the text an agent gets as its first prompt when the user presses "work this" on a
queue row. Every placeholder is filled from the row; nothing is invented.

Two things live here and nowhere else:
  * `is_ready(row, cfg)` -- the gate, in code not prose. It runs before any window opens.
  * `build(row, cfg)`   -- the template, filled in.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..models import TaskRow

TEMPLATE = """\
Tracker id: {tracker_id} (one stable name for this piece of work; use it in the dispatch log and any CHECKPOINT entry).
You are working the Sprint "{summary}" for project {project} (row file: {row_path}).
Context the row carries: tier {tier}, effort {complexity}, est_context {est_context}, status {status}.
Claude's last reply on the row (may hold a suggested default): {reply}.
Note on the row: {note}.
Source note: {source} (read it from the vault if you need it; the vault is at {vault}).
Rules: orient from this project's CLAUDE.md and CHECKPOINT.md first, as any session would. Do the work. Record completion in the project CHECKPOINT and the source note. Do NOT edit the Sprint row file itself; Pantheon and the vault sweeps own it. When finished, say DONE and one line of what changed."""

_SLUG_RE = re.compile(r"[^A-Za-z0-9]+")


def slug(text: Optional[str], limit: int = 40) -> str:
    """`Rebuild the photo archive index` -> `rebuild-the-photo-archive-index`."""
    s = _SLUG_RE.sub("-", str(text or "")).strip("-").lower()
    return s[:limit].rstrip("-") or "row"


def tracker_id(row: TaskRow) -> str:
    """`<project>-<row file stem>`, one stable name per piece of work."""
    return f"{row.project or 'none'}-{slug(row.id)}"


def project_dir(row: TaskRow, cfg) -> Path:
    return Path(cfg.projects_root) / str(row.project or "")


def is_ready(row: Optional[TaskRow], cfg) -> tuple[bool, str]:
    """(ok, reason). The reason is a sentence for the footer that says what to do."""
    if row is None:
        return False, "no task selected"
    if not (row.summary or "").strip():
        return False, "this row has no summary, so there is nothing to brief; fill it in Obsidian first"
    if not (row.project or "").strip():
        return False, "this row has no project, so there is no folder to work in; set one in Obsidian first"
    folder = project_dir(row, cfg)
    if not folder.is_dir():
        return False, f"no project folder for '{row.project}'; open it by hand"
    return True, ""


def _or(value, fallback: str = "none") -> str:
    text = str(value if value is not None else "").strip().replace("\n", " ")
    return text or fallback


def build(row: TaskRow, cfg) -> str:
    return TEMPLATE.format(
        tracker_id=tracker_id(row),
        summary=_or(row.summary, "(no summary)"),
        project=_or(row.project),
        row_path=_or(row.path, f"{row.id}.md"),
        tier=_or(row.tier, "n/a"),
        complexity=_or(row.complexity, "n/a"),
        est_context=_or(row.est_context, "n/a"),
        status=_or(row.status, "n/a"),
        reply=_or(row.reply),
        note=_or(row.note),
        source=_or(row.source),
        vault=cfg.vault,
    )


def one_line(text: str) -> str:
    """The briefing as one paragraph, for typing into a prompt box: a raw newline would submit
    the message early (tmux `send-keys` sends bytes, and Enter is a byte)."""
    return " ".join(str(text or "").split())


def write(row: TaskRow, cfg, now: Optional[datetime] = None, text: Optional[str] = None) -> Path:
    """Save `state/dispatch/<UTC>-<slug>.md` and return its path. The file keeps the line breaks;
    the typed prompt is `one_line` of the same text.

    `text`, when given, is written verbatim instead of `build(row, cfg)` -- the governor's
    hand-off writes its own briefing but still wants the file to land in the usual place, under the usual name.
    """
    now = now or datetime.now(timezone.utc)
    folder = Path(cfg.dispatch_dir)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{now.strftime('%Y%m%dT%H%M%SZ')}-{slug(row.id)}.md"
    content = text if text is not None else build(row, cfg)
    path.write_text(content + "\n", encoding="utf-8", newline="\n")
    return path
