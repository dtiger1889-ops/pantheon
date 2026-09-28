"""One Windows toast per startup-dialog appearance (`pantheon/dialogs.py`).

A Claude Code window stuck on a startup question fires no hook and Remote Control is not
connected yet, so the toast is the only thing that can reach the user while he is looking at Claude
Desktop or claude.ai instead of the deck. The supervisor pane calls `announce` on every tick it
sees a dialog; the memory file makes that "once per appearance", across ticks and across deck
restarts, and `forget_except` drops a key once the question is gone so a later one toasts again.

Settings: this lane follows only
`[notify.toast] enabled` plus its own `[notify] startup_dialogs = false` off switch. It does NOT
wait for `[notify] enabled`/`dry_run`, which gate the needs-you stream the user is still
dry-running: a window hung before it even started is the one case the brief says must reach him.
Every decision is also written to `state/notify/sent.jsonl` like the runner's.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional

from . import channels, store

MEMORY_FILE = "startup_dialogs.json"
TITLE = "Claude is waiting on a question"


def _memory_path(cfg) -> Path:
    return Path(cfg.state_dir) / "notify" / MEMORY_FILE


def _read(cfg) -> dict[str, str]:
    try:
        data = json.loads(_memory_path(cfg).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(cfg, data: dict[str, str]) -> None:
    store._write_atomic(_memory_path(cfg), json.dumps(data, indent=1, sort_keys=True))


def windows_path(path: Path) -> str:
    """`/c/Home/x` -> `C:/Home/x` (MSYS2 Python sees POSIX paths; pwsh.exe needs a drive)."""
    text = str(path).replace("\\", "/")
    m = re.match(r"^/([a-zA-Z])/(.*)$", text)
    if m:
        return f"{m.group(1).upper()}:/{m.group(2)}"
    if text.startswith("/"):
        return "C:/msys64" + text
    return text


def toast_script() -> str:
    return windows_path(Path(__file__).resolve().parents[2] / channels.TOAST_SCRIPT)


def enabled(cfg) -> bool:
    raw = dict(getattr(cfg, "notify", None) or {})
    if raw.get("startup_dialogs") is False:
        return False
    return bool(cfg.notify_settings().toast_enabled)


def announce(cfg, key: str, body: str, send_toast: Optional[Callable] = None,
             now: Optional[datetime] = None) -> bool:
    """Toast `body` unless `key` (pane id + dialog kind) was already announced. True = sent."""
    memory = _read(cfg)
    if key in memory:
        return False
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    memory[key] = stamp
    _write(cfg, memory)
    if enabled(cfg):
        ok, why = (send_toast or channels.toast)(TITLE, body, cfg.tools.pwsh, toast_script())
    else:
        ok, why = False, "toast switched off"
    store._append_line(cfg.notify_sent_file, {"ts": stamp, "kind": "startup_dialog", "key": key,
                                              "body": body, "sent": bool(ok), "why": why})
    return bool(ok)


def forget_except(cfg, live_keys: Iterable[str]) -> None:
    """Drop every remembered key whose question is no longer on screen."""
    live = set(live_keys)
    memory = _read(cfg)
    kept = {k: v for k, v in memory.items() if k in live}
    if kept != memory:
        _write(cfg, kept)
