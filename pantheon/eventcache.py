"""One parsed copy of `events.jsonl` shared across a tick's several readers.

`events.read_events` already tolerates the file changing under it; this module
only avoids re-opening and re-`json.loads`-ing the same unchanged bytes from the several call
sites that read the same `events.jsonl` on their own timers: the supervisor pane, the queue pane,
and the standalone governor and notify runners. Keyed on `(path, size, mtime_ns)`: an append
always grows the file, so a size match alone already proves nothing was added; the mtime is kept
too as a cheap second check for the (untested-in-practice) case of a same-size rewrite. A process
only ever reads one configured `events.jsonl`, so one cached entry is enough -- the path is part
of the key only so a test that points at a different tmp_path is never served the wrong file's
result by coincidence.

Callers must treat the returned list as read-only -- it is the SAME object handed to every caller
until the file changes, not a copy.
"""
from __future__ import annotations

import os
from pathlib import Path

from .events import read_events
from .models import Event

_Fingerprint = tuple[str, int, int]
_cache: tuple[_Fingerprint, tuple[list[Event], int]] | None = None


def _fingerprint(path: Path) -> _Fingerprint:
    try:
        st = path.stat()
    except OSError:
        return (str(path), -1, -1)  # missing/unreadable: never equals a real previous fingerprint
    mtime_ns = getattr(st, "st_mtime_ns", None)
    if mtime_ns is None:  # pragma: no cover - every supported platform has st_mtime_ns
        mtime_ns = int(st.st_mtime * 1e9)
    return (str(path), st.st_size, mtime_ns)


def read_events_cached(path: str | os.PathLike) -> tuple[list[Event], int]:
    """Same contract as `events.read_events`: `(events, parse_error_count)`, `([], 0)` when the
    file is missing. Returns the previous call's result unchanged when the file's size and mtime
    have not moved since. Tests that need a fresh read reset the module state directly:
    `pantheon.eventcache._cache = None`."""
    global _cache
    p = Path(path)
    fp = _fingerprint(p)
    if _cache is not None and _cache[0] == fp:
        return _cache[1]
    result = read_events(p)
    _cache = (fp, result)
    return result
