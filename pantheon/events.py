"""The event log: `state/agents/events.jsonl`. Append-only, one JSON object per line, UTF-8.

Hooks (PowerShell) and the Codex runner write it; the deck reads it. Corrupt lines are skipped
and counted, never fatal. Rotation at 5 MB.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from .models import Event, utcnow_iso

MAX_BYTES = 5 * 1024 * 1024


def read_events(path: str | os.PathLike) -> tuple[list[Event], int]:
    """Return (events, parse_error_count). Missing file -> ([], 0)."""
    p = Path(path)
    if not p.exists():
        return [], 0
    events: list[Event] = []
    errors = 0
    with open(p, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip().lstrip("﻿")
            if not line:
                continue
            try:
                d = json.loads(line)
                if not isinstance(d, dict):
                    raise ValueError("not an object")
                events.append(Event.from_dict(d))
            except (ValueError, TypeError):
                errors += 1
    return events, errors


def append_event(path: str | os.PathLike, event: Event | dict[str, Any]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    d = event.to_dict() if isinstance(event, Event) else dict(event)
    d.setdefault("ts", utcnow_iso())
    d.setdefault("source", "pantheon")
    with open(p, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(d, ensure_ascii=False) + "\n")


def rotate_if_large(path: str | os.PathLike, max_bytes: int = MAX_BYTES) -> Optional[Path]:
    """Rename to `events-<UTC>.jsonl` when over the cap; return the archived path or None."""
    p = Path(path)
    if not p.exists() or p.stat().st_size <= max_bytes:
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = p.with_name(f"{p.stem}-{stamp}{p.suffix}")
    p.rename(target)
    return target


def norm_path(s: Optional[str]) -> str:
    """Normalize a path for comparison: forward slashes, no trailing slash, lower-case."""
    if not s:
        return ""
    t = s.replace("\\", "/").rstrip("/")
    if t.startswith("/c/") or t.startswith("/C/"):
        t = "C:" + t[2:]
    if len(t) >= 2 and t[1] == ":":
        t = t[0].upper() + t[1:]
    return t.lower()


def derive_project(cwd: Optional[str], projects_root: str) -> Optional[str]:
    """First path segment under `projects_root`, e.g. `.../Claude/hiking_log_v2/x` -> `hiking_log_v2`."""
    c, r = norm_path(cwd), norm_path(projects_root)
    if not c or not r or not c.startswith(r + "/"):
        return None
    # Keep the folder's real case (Plumb, not plumb): slice the un-lowercased form, same length.
    raw = (cwd or "").replace("\\", "/").rstrip("/")
    if raw[:3].lower() == "/c/":
        raw = "C:" + raw[2:]
    rest = raw[len(r) + 1 :]
    return rest.split("/")[0] or None


def by_session(events: Iterable[Event]) -> dict[str, list[Event]]:
    out: dict[str, list[Event]] = {}
    for e in events:
        key = e.session_id or e.job_id or "?"
        out.setdefault(key, []).append(e)
    return out
