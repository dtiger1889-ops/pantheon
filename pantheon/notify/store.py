"""Notify's persisted memory. `state/notify/last.json` holds
`prev_status` (session_id -> its status as of the last tick, so a restart does not see every
open row as a brand-new transition) and `history` (the dedupe/nudge memory `rules.decide`
reads); `state/notify/sent.jsonl` is the append-only record of every notification this runner
ever decided, one line each, whether it actually reached a channel, was skipped for quiet hours,
or was only a dry run. Same atomic-write-then-append discipline `governor/parked.py` uses, for
the same reason: a crash or a restart must pick up exactly where it left off."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def _write_atomic(path: Path, text: str) -> None:
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


def read_memory(cfg) -> tuple[dict[str, str], dict[str, dict]]:
    """(prev_status, history), both `{}` when `last.json` is missing or unreadable -- never
    raised. A lost memory only means the first tick after a crash may re-derive some open rows
    as "a transition from None"; `rules.decide()`'s own one-shot dedupe (`_once`, keyed off
    `history` alone) is what actually guarantees restart safety, so a lost `prev_status` costs
    at most a missed edge during the outage, never a duplicate."""
    path = Path(cfg.notify_last_file)
    if not path.exists():
        return {}, {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}, {}
    if not isinstance(data, dict):
        return {}, {}
    prev_status = data.get("prev_status")
    history = data.get("history")
    return (prev_status if isinstance(prev_status, dict) else {},
            history if isinstance(history, dict) else {})


def write_memory(cfg, prev_status: dict[str, str], history: dict[str, dict]) -> None:
    _write_atomic(Path(cfg.notify_last_file), json.dumps(
        {"prev_status": prev_status, "history": history}, indent=2, sort_keys=True))


def append_sent(cfg, record: dict[str, Any]) -> None:
    """One line per `Notification` this runner decided to fire, whether it actually reached a
    channel, was quiet-hours-suppressed, or was only a dry run. One line PER NOTIFICATION, not per channel -- `channels` carries the list -- so acceptance
    2's "exactly 1 line" for one permission-prompt notification holds even though the rules table
    sends that tier to two channels (toast + ntfy) at once."""
    _append_line(Path(cfg.notify_sent_file), record)
