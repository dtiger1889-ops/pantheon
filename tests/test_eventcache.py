"""`eventcache.read_events_cached`: same contract as
`events.read_events`, but serves the previous call's result -- the same list object -- while a
file's size and mtime have not moved, so several readers on the same tick do not each re-open and
re-parse the same bytes.
"""
from __future__ import annotations

import json
import os

from pantheon import eventcache as eventcache_mod
from pantheon import events as events_mod
from pantheon.eventcache import read_events_cached
from pantheon.models import Event


def _write(path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for d in lines:
            fh.write(json.dumps(d) + "\n")


def test_missing_file_matches_read_events(tmp_path):
    p = tmp_path / "events.jsonl"
    assert read_events_cached(p) == events_mod.read_events(p) == ([], 0)


def test_second_call_returns_the_same_object_when_the_file_has_not_changed(tmp_path):
    p = tmp_path / "events.jsonl"
    _write(p, [{"ts": "2026-09-02T00:00:00.000Z", "event": "SessionStart", "session_id": "s1"}])
    eventcache_mod._cache = None

    events1, errors1 = read_events_cached(p)
    events2, errors2 = read_events_cached(p)
    assert events2 is events1          # not merely equal -- the SAME list, no re-parse happened
    assert (events1, errors1) == (events2, errors2)


def test_an_append_is_always_seen_even_with_unlucky_mtime_resolution(tmp_path):
    """The cache key includes (size, mtime_ns): appending always changes the size, so a stale
    read is never served purely because two writes landed in the same mtime tick."""
    p = tmp_path / "events.jsonl"
    _write(p, [{"ts": "2026-09-02T00:00:00.000Z", "event": "SessionStart", "session_id": "s1"}])
    eventcache_mod._cache = None
    events1, _ = read_events_cached(p)
    assert len(events1) == 1
    before = os.stat(p)

    events_mod.append_event(p, Event(ts="2026-09-02T00:00:01.000Z", event="Stop", session_id="s1"))
    # Force the worst case: same mtime as before the append (only size can tell them apart).
    os.utime(p, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert os.stat(p).st_size != before.st_size   # sanity: the append really did grow the file

    events2, _ = read_events_cached(p)
    assert len(events2) == 2
    assert events2 is not events1


def test_matches_read_events_content_for_a_corrupt_line(tmp_path):
    p = tmp_path / "events.jsonl"
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": "2026-09-02T00:00:00.000Z", "event": "SessionStart"}) + "\n")
        fh.write("not json\n")
    eventcache_mod._cache = None
    events, errors = read_events_cached(p)
    ref_events, ref_errors = events_mod.read_events(p)
    assert errors == ref_errors == 1
    assert [e.to_dict() for e in events] == [e.to_dict() for e in ref_events]


def test_a_different_path_is_never_served_from_another_files_cache(tmp_path):
    """The fingerprint carries the path, so switching files -- as a test suite naturally does --
    is never confused with a same-size-and-mtime coincidence on the previous file."""
    p1 = tmp_path / "a" / "events.jsonl"
    p2 = tmp_path / "b" / "events.jsonl"
    _write(p1, [{"ts": "2026-09-02T00:00:00.000Z", "event": "SessionStart", "session_id": "s1"}])
    _write(p2, [{"ts": "2026-09-02T00:00:00.000Z", "event": "SessionStart", "session_id": "s2"}])
    events1, _ = read_events_cached(p1)
    events2, _ = read_events_cached(p2)
    assert events1[0].session_id == "s1"
    assert events2[0].session_id == "s2"


def test_resetting_the_module_cache_forces_a_fresh_read(tmp_path):
    p = tmp_path / "events.jsonl"
    _write(p, [{"ts": "2026-09-02T00:00:00.000Z", "event": "SessionStart", "session_id": "s1"}])
    eventcache_mod._cache = None
    first, _ = read_events_cached(p)
    eventcache_mod._cache = None  # tests reset the module state directly; there is no public API
    second, _ = read_events_cached(p)
    assert first == second
    assert first is not second   # reset, so this was a fresh read, not the cached object
