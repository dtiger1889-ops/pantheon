"""Reads the JSON Claude Code pipes to its statusLine command, files it away, prints one line.

`bin/statusline_capture` is the shell wrapper; this is the part that has to be careful.
It never raises: whatever happens, one line goes to stdout and the exit code is 0, because a
statusLine command that fails takes the status bar down with it.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from .. import config
from . import sources

FALLBACK = "pantheon hud: no data"


def capture(raw: str, cfg=None) -> str:
    """Save one statusline payload and return the line to print back."""
    cfg = cfg or config.load()
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("statusline payload was not an object")

    session_id = str(data.get("session_id") or "unknown")
    safe = "".join(c for c in session_id if c.isalnum() or c in "-_")[:64] or "unknown"
    target = Path(cfg.statusline_dir) / f"{safe}.json"
    sources.write_atomic(target, json.dumps(data, sort_keys=True))
    # The 5-hour and weekly numbers are the account's, not this session's: read them over every
    # capture (this one included), so an idle session's bar still shows the real figure. A
    # failure there must not cost the line -- fall back to this session's own payload.
    try:
        account = sources.claude_from_statusline(cfg.statusline_dir)
    except Exception:
        account = None
    return sources.statusline_line(data, account=account)


def main() -> int:
    try:
        line = capture(sys.stdin.read())
    except Exception:
        line = FALLBACK
    print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
