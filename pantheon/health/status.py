"""The deck's health line: what `state/health.json`, written by the
freeze watcher, says -- shown only for warn or bad, silent when healthy. A file the
watcher has not refreshed for STALE_SECONDS (it stopped) says nothing either."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

HEALTH_FILE = "health.json"
STALE_SECONDS = 600


def health_path(cfg) -> Path:
    """`state/health.json` for the live session; inside that session's own health folder for any
    other, so a throwaway deck's watcher never writes the live deck's status line."""
    from . import sample as sample_mod

    folder = sample_mod.health_dir(cfg)
    return Path(cfg.state_dir) / HEALTH_FILE if folder == Path(cfg.state_dir) / "health" else folder / HEALTH_FILE


def health_message(cfg, now: Optional[datetime] = None) -> tuple[str, str]:
    """(message, level): ("", "ok") unless the watcher's latest word is warn or bad."""
    try:
        data = json.loads(health_path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "", "ok"
    if not isinstance(data, dict):
        return "", "ok"
    level = str(data.get("level") or "ok")
    message = str(data.get("message") or "")
    if level not in ("warn", "bad") or not message:
        return "", "ok"
    try:
        written = datetime.fromisoformat(str(data.get("written_at")).replace("Z", "+00:00"))
    except ValueError:
        return "", "ok"
    if written.tzinfo is None:
        written = written.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    if (now - written).total_seconds() > STALE_SECONDS:
        return "", "ok"
    return message, level
