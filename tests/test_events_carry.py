"""Rotation keeps idle sessions known: `events.carry_forward` (2026-09-29)."""
import json

from pantheon import events as ev


def _write(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def test_idle_session_survives_rotation(tmp_path):
    p = tmp_path / "events.jsonl"
    _write(p, [
        {"ts": "2026-09-29T16:00:00Z", "event": "PostToolUse", "session_id": "A", "tmux_pane": "%13"},
        {"ts": "2026-09-29T16:12:00Z", "event": "Stop", "session_id": "A", "tmux_pane": "%13"},
        {"ts": "2026-09-29T16:20:00Z", "event": "Stop", "session_id": "B", "tmux_pane": "%11"},
        {"ts": "2026-09-29T16:30:00Z", "event": "SessionEnd", "session_id": "B", "tmux_pane": "%11"},
    ])
    archived = ev.rotate_if_large(p, max_bytes=10)
    assert archived is not None
    kept, _ = ev.read_events(p)
    assert [(e.session_id, e.event) for e in kept] == [("A", "Stop")]


def test_carry_skips_sessions_already_in_current(tmp_path):
    old = tmp_path / "events-old.jsonl"
    cur = tmp_path / "events.jsonl"
    _write(old, [{"ts": "2026-09-29T16:12:00Z", "event": "Stop", "session_id": "A"},
                 {"ts": "2026-09-29T16:13:00Z", "event": "Stop", "session_id": "C"}])
    _write(cur, [{"ts": "2026-09-29T21:00:00Z", "event": "PostToolUse", "session_id": "A"}])
    assert ev.carry_forward(old, cur) == 1
    got, _ = ev.read_events(cur)
    assert [e.session_id for e in got] == ["A", "C"]
