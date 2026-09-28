"""The written record that makes the governor safe.

`state/limits/pending.json` -- one entry per session with a grace clock running: it was told to
wind down and has not parked yet. `state/limits/parked.jsonl` -- append-only, one line per park,
resume, or refusal; never rewritten in place, so a crash mid-write loses at most the last line.
`state/limits/governor-dryrun.jsonl` -- everything the governor would have done, `would_`
prefixed, while `dry_run = true` (section 8 point 4).

Every write here is atomic where it matters (pending.json, which the runner rewrites every
tick) and append-only where it matters (the two `.jsonl` logs), so a governor or deck restart
picks up exactly where it left off with no manual step.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

PARKED_FILE = "parked.jsonl"
PENDING_FILE = "pending.json"
DRYRUN_FILE = "governor-dryrun.jsonl"


def _limits_dir(cfg) -> Path:
    return Path(cfg.state_dir) / "limits"


def parked_path(cfg) -> Path:
    return _limits_dir(cfg) / PARKED_FILE


def pending_path(cfg) -> Path:
    return _limits_dir(cfg) / PENDING_FILE


def dryrun_path(cfg) -> Path:
    return _limits_dir(cfg) / DRYRUN_FILE


# --------------------------------------------------------------------------- low-level I/O


def _write_atomic(path: Path, text: str) -> None:
    """Write via a temp file in the same folder, then rename -- a reader never sees a half
    write (same pattern as `hud/sources.write_atomic`)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _append_line(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def _read_lines(path: Path) -> list[dict[str, Any]]:
    """Every parseable JSON object line. A corrupt line is skipped, never fatal."""
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip().lstrip("﻿")
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(d, dict):
                out.append(d)
    return out


# --------------------------------------------------------------------------- pending.json


def read_pending(cfg) -> dict[str, dict[str, Any]]:
    """session_id -> its wind-down/grace record. Missing or unreadable file -> `{}`, never
    raised -- a lost grace clock just means those sessions never park automatically; the user
    still has the wind-down message on his screen."""
    path = pending_path(cfg)
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def write_pending(cfg, pending: dict[str, dict[str, Any]]) -> None:
    _write_atomic(pending_path(cfg), json.dumps(pending, indent=2, sort_keys=True))


def set_pending(cfg, session_id: str, entry: dict[str, Any]) -> None:
    """Start (or update) one session's grace clock, held across a governor restart."""
    pending = read_pending(cfg)
    pending[session_id] = dict(entry)
    write_pending(cfg, pending)


def clear_pending(cfg, session_id: str) -> None:
    pending = read_pending(cfg)
    if pending.pop(session_id, None) is not None:
        write_pending(cfg, pending)


# --------------------------------------------------------------------------- parked.jsonl


@dataclass(frozen=True)
class Parked:
    """One `park` line."""

    session_id: str
    project: Optional[str] = None
    cwd: Optional[str] = None
    window_name: Optional[str] = None
    parked_at: str = ""
    reason: str = ""  # "checkpointed" | "grace_expired"
    resume_after: str = ""  # ISO-8601 UTC

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "park",
            "session_id": self.session_id,
            "project": self.project,
            "cwd": self.cwd,
            "window_name": self.window_name,
            "parked_at": self.parked_at,
            "reason": self.reason,
            "resume_after": self.resume_after,
        }


def append_park(cfg, parked: Parked) -> None:
    _append_line(parked_path(cfg), parked.to_dict())


def append_resumed(cfg, session_id: str, by: str, resumed_at: Optional[str] = None) -> None:
    """`by`: `"the user"` | `"claude-code-builtin"` | `"handoff"` | `"governor-rung-<N>"`."""
    from ..models import utcnow_iso

    _append_line(parked_path(cfg), {
        "kind": "resumed", "session_id": session_id, "resumed_at": resumed_at or utcnow_iso(), "by": by,
    })


def append_park_refused(cfg, session_id: str, parked_count: int, max_parked: int, at: Optional[str] = None) -> None:
    from ..models import utcnow_iso

    _append_line(parked_path(cfg), {
        "kind": "park_refused", "session_id": session_id, "at": at or utcnow_iso(),
        "parked_count": parked_count, "max_parked": max_parked,
    })


def open_parks(cfg) -> dict[str, dict[str, Any]]:
    """session_id -> its `park` line, for every session with no LATER `resumed` line for that
    id -- "still parked". The whole file is read every tick on purpose: a park
    line with no resumed line after it in file order is the only signal that matters, and that
    is cheap to recompute rather than to keep in sync by hand."""
    open_: dict[str, dict[str, Any]] = {}
    for line in _read_lines(parked_path(cfg)):
        kind = line.get("kind", "park")
        sid = line.get("session_id")
        if not sid:
            continue
        if kind == "park":
            open_[sid] = line
        elif kind == "resumed":
            open_.pop(sid, None)
    return open_


# --------------------------------------------------------------------------- dry-run log


def append_dryrun(cfg, record: dict[str, Any]) -> None:
    """One line the governor would have written, or typed, or run -- `event` already carries
    the `would_` prefix. Nothing here is ever read back by the runner;
    it exists so the user can read a night of decisions before he trusts the real thing."""
    _append_line(dryrun_path(cfg), record)


# --------------------------------------------------------------------------- HUD summary


def summary(cfg) -> dict[str, Any]:
    """Counts for the HUD's governor attention line: how many sessions are
    still winding down (a grace clock running in `pending.json`) and how many are parked (an
    open line in `parked.jsonl`), the nearest resume time among them, and whether the toml has
    `dry_run` on. Never raises -- a missing or unreadable file just means zero of that count."""
    pending = read_pending(cfg)
    parks = open_parks(cfg)
    resets = [e.get("resume_after") for e in pending.values() if isinstance(e, dict) and e.get("resume_after")]
    resets += [p.get("resume_after") for p in parks.values() if p.get("resume_after")]
    governor_cfg = getattr(cfg, "governor", None) or {}
    return {
        "winding_down": len(pending),
        "parked": len(parks),
        "five_hour_resets_at": min(resets) if resets else None,  # ISO-8601 strings sort chronologically
        "dry_run": bool(governor_cfg.get("dry_run", True)),
    }
